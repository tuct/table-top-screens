"""Image pool plus per-screen assignment.

Two levels, deliberately separate:

    data/_pool/
        index.json          every image ever uploaded, once
        items/<id>          the original uploaded bytes, verbatim
    data/<device>/
        used.json           which pool items this screen uses, in order,
                            and which one is on it now
        prefs.json          render overrides (fit / rot / zoom / q / bg)
    data/_scenes.json       named snapshots of what every screen shows

The pool is shared, so an image uploaded for one screen can be picked for
another without re-uploading, and it is stored once no matter how many
screens use it. Each screen then has two lists: the pool items it **uses**
(ordered, drag-and-drop, one of them current) and the rest of the pool, which
it does not.

Pool ids are content hashes, so uploading the same file twice does not
duplicate it -- a shared pool is a set of pictures, not a log of uploads.
(The earlier per-device list used random ids on purpose, because there a
repeat upload was a meaningful second entry.)

Only originals are stored here -- never rendered output. Rendering per screen
is `app.py`'s job, cached by (image hash, render parameters), so two screens
of different sizes share one pool entry and get their own versions.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from pathlib import Path

from PIL import Image

import frames

DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ITEM_RE = re.compile(r"^[0-9a-f]{16}$")
# Leading underscore, so DEVICE_RE can never match it and a device cannot
# collide with the pool directory.
POOL = "_pool"


class LibraryError(Exception):
    """Bad request-level problem; callers map this to a 4xx."""


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------
def device_dir(root: Path, device: str) -> Path:
    if not DEVICE_RE.match(device):
        raise LibraryError("invalid device name")
    return root / device


def pool_dir(root: Path) -> Path:
    return root / POOL


def item_path(root: Path, item_id: str) -> Path:
    if not ITEM_RE.match(item_id):
        raise LibraryError("invalid item id")
    return pool_dir(root) / "items" / item_id


def _pool_index_path(root: Path) -> Path:
    return pool_dir(root) / "index.json"


def _used_path(root: Path, device: str) -> Path:
    return device_dir(root, device) / "used.json"


def _prefs_path(root: Path, device: str) -> Path:
    return device_dir(root, device) / "prefs.json"


def _state_path(root: Path, device: str) -> Path:
    return device_dir(root, device) / "state.json"


# A file, not a directory, so it cannot be mistaken for a screen by anything
# that lists data/ looking for devices.
def _scenes_path(root: Path) -> Path:
    return root / "_scenes.json"


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# the pool
# --------------------------------------------------------------------------
def pool(root: Path) -> list[dict]:
    """Every uploaded image, newest first. Entries with no file are dropped."""
    _migrate(root)
    items = _read_json(_pool_index_path(root), [])
    if not isinstance(items, list):
        return []
    keep = [
        it
        for it in items
        if isinstance(it, dict)
        and ITEM_RE.match(str(it.get("id", "")))
        and item_path(root, it["id"]).exists()
    ]
    keep.sort(key=lambda it: it.get("uploaded_at", 0), reverse=True)
    return keep


def pool_get(root: Path, item_id: str) -> dict | None:
    if not ITEM_RE.match(item_id):
        raise LibraryError("invalid item id")
    return next((it for it in pool(root) if it["id"] == item_id), None)


def body_of(root: Path, item_id: str) -> bytes:
    p = item_path(root, item_id)
    if not p.exists():
        raise LibraryError("no such item")
    return p.read_bytes()


def _describe(body: bytes, content_type: str, filename: str) -> dict:
    try:
        with Image.open(io.BytesIO(body)) as probe:
            probe.load()
            size, fmt = probe.size, probe.format
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types
        raise LibraryError(f"not a decodable image: {exc}") from exc
    sha = hashlib.sha256(body).hexdigest()
    # Detect motion once, at upload, so lists and pages never re-decode to
    # find out. Covers GIF, APNG and animated WebP -- Pillow reports an APNG
    # as format "PNG" with n_frames > 1, so the format name alone is not
    # enough to tell.
    motion = frames.describe_motion(body)
    return {
        "id": sha[:16],  # content-addressed: re-uploading is a no-op
        "sha256": sha,
        "bytes": len(body),
        "content_type": content_type,
        "filename": filename,
        "source_format": fmt,
        "source_size": list(size),
        "uploaded_at": time.time(),
        "animated": motion["animated"],
        "frames": motion["frames"],
        "duration_ms": motion["duration_ms"],
        # Whether a browser can play the original bytes as-is, which is what
        # lets a list thumbnail animate without the server rendering a clip.
        "browser_playable": motion["browser_playable"],
    }


def pool_add(root: Path, body: bytes, content_type: str, filename: str) -> dict:
    """Put an image in the pool. Identical bytes reuse the existing entry."""
    if not body:
        raise LibraryError("empty body")
    item = _describe(body, content_type, filename)
    items = pool(root)
    if (existing := next((i for i in items if i["id"] == item["id"]), None)) is not None:
        return existing
    item_path(root, item["id"]).parent.mkdir(parents=True, exist_ok=True)
    item_path(root, item["id"]).write_bytes(body)
    _write_json(_pool_index_path(root), [item] + items)
    return item


def pool_remove(root: Path, item_id: str) -> dict:
    """Delete from the pool, and from every screen that was using it."""
    items = pool(root)
    if not any(it["id"] == item_id for it in items):
        raise LibraryError("no such item")

    # Find the users BEFORE removing it from the index: used() validates
    # against the pool, so afterwards it would report nobody.
    affected = [d for d in devices(root) if item_id in used(root, d)["used"]]
    for d in affected:
        unassign(root, d, item_id)

    _write_json(_pool_index_path(root), [it for it in items if it["id"] != item_id])
    item_path(root, item_id).unlink(missing_ok=True)
    return {"removed": item_id, "affected": affected}


# --------------------------------------------------------------------------
# per-screen render preferences
# --------------------------------------------------------------------------
def prefs(root: Path, device: str) -> dict:
    got = _read_json(_prefs_path(root, device), {})
    return got if isinstance(got, dict) else {}


def set_prefs(root: Path, device: str, value: dict) -> dict:
    _write_json(_prefs_path(root, device), value)
    return value


# --------------------------------------------------------------------------
# what a screen last reported holding (pushed by the screen, never polled)
# --------------------------------------------------------------------------
def screen_state(root: Path, device: str) -> dict | None:
    got = _read_json(_state_path(root, device), None)
    return got if isinstance(got, dict) else None


def set_screen_state(root: Path, device: str, value: dict) -> dict:
    _write_json(_state_path(root, device), value)
    return value


def devices(root: Path) -> list[str]:
    """Every screen we hold state for."""
    if not root.exists():
        return []
    _migrate(root)
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and p.name != POOL and DEVICE_RE.match(p.name)
    )


# --------------------------------------------------------------------------
# scenes: a named snapshot of what every screen is showing, and how
# --------------------------------------------------------------------------
def scenes(root: Path) -> list[dict]:
    got = _read_json(_scenes_path(root), [])
    return got if isinstance(got, list) else []


def scene_save(root: Path, name: str) -> dict:
    """Capture the current item and render prefs of every known screen."""
    name = (name or "").strip()
    if not name:
        raise LibraryError("a scene needs a name")
    if len(name) > 60:
        raise LibraryError("name too long")
    entries = {}
    for d in devices(root):
        state = used(root, d)
        entries[d] = {"item": state["current"], "prefs": prefs(root, d)}
    scene = {
        "id": hashlib.sha256(f"{name}{time.time()}".encode()).hexdigest()[:12],
        "name": name,
        "saved_at": time.time(),
        "entries": entries,
    }
    # Replacing a scene of the same name is what you almost always mean.
    keep = [s for s in scenes(root) if s.get("name") != name]
    _write_json(_scenes_path(root), keep + [scene])
    return scene


def scene_get(root: Path, scene_id: str) -> dict | None:
    return next((s for s in scenes(root) if s.get("id") == scene_id), None)


def scene_apply(root: Path, scene_id: str) -> list[str]:
    """Restore a scene. Returns the screens actually changed.

    Skips entries whose image has since left the pool, rather than failing the
    whole scene -- a scene is a convenience, not a transaction.
    """
    scene = scene_get(root, scene_id)
    if scene is None:
        raise LibraryError("no such scene")
    ids = {it["id"] for it in pool(root)}
    changed = []
    for device, entry in (scene.get("entries") or {}).items():
        if not DEVICE_RE.match(device):
            continue
        item = entry.get("item")
        if isinstance(entry.get("prefs"), dict):
            set_prefs(root, device, entry["prefs"])
        if item in ids:
            assign(root, device, item, make_current=True)
            changed.append(device)
        elif isinstance(entry.get("prefs"), dict):
            changed.append(device)
    return changed


def scene_remove(root: Path, scene_id: str) -> None:
    remaining = [s for s in scenes(root) if s.get("id") != scene_id]
    if len(remaining) == len(scenes(root)):
        raise LibraryError("no such scene")
    _write_json(_scenes_path(root), remaining)


# --------------------------------------------------------------------------
# per-screen assignment
# --------------------------------------------------------------------------
def used(root: Path, device: str) -> dict:
    """{"current": id|None, "used": [ids]} -- validated against the pool."""
    # Read the pool FIRST: pool() is what runs the migration, and the
    # migration is what creates used.json for a device coming from an older
    # layout. Reading used.json before that returned an empty state on the
    # very first call, so a just-migrated screen reported nothing on it.
    ids = {it["id"] for it in pool(root)}
    state = _read_json(_used_path(root, device), {})
    order = [i for i in state.get("used", []) if i in ids]
    current = state.get("current")
    if current not in order:
        current = order[0] if order else None
    return {"current": current, "used": order}


def _save_used(root: Path, device: str, state: dict) -> dict:
    _write_json(_used_path(root, device), state)
    return state


def used_items(root: Path, device: str) -> list[dict]:
    """The screen's items, in its order."""
    by_id = {it["id"]: it for it in pool(root)}
    return [by_id[i] for i in used(root, device)["used"] if i in by_id]


def unused_items(root: Path, device: str) -> list[dict]:
    """Everything in the pool this screen is not using."""
    assigned = set(used(root, device)["used"])
    return [it for it in pool(root) if it["id"] not in assigned]


def current(root: Path, device: str) -> dict | None:
    state = used(root, device)
    return pool_get(root, state["current"]) if state["current"] else None


def assign(root: Path, device: str, item_id: str, make_current: bool = True) -> dict:
    if pool_get(root, item_id) is None:
        raise LibraryError("no such item")
    state = used(root, device)
    if item_id not in state["used"]:
        state["used"].append(item_id)
    if make_current or state["current"] is None:
        state["current"] = item_id
    return _save_used(root, device, state)


def unassign(root: Path, device: str, item_id: str) -> dict:
    """Stop using an item on this screen. The pool keeps it."""
    state = used(root, device)
    if item_id not in state["used"]:
        raise LibraryError("no such item")
    position = state["used"].index(item_id)
    state["used"].remove(item_id)
    if state["current"] == item_id:
        # Promote whatever took its place, else the last one, else nothing.
        state["current"] = (
            state["used"][min(position, len(state["used"]) - 1)]
            if state["used"]
            else None
        )
    return _save_used(root, device, state)


def select(root: Path, device: str, item_id: str) -> dict:
    """Put an item on the screen, assigning it first if need be."""
    if pool_get(root, item_id) is None:
        raise LibraryError("no such item")
    return assign(root, device, item_id, make_current=True)


def reorder(root: Path, device: str, ids: list[str]) -> dict:
    """Apply a drag-and-drop order.

    Tolerant on purpose: ids the client did not mention keep their relative
    order at the end, and ids this screen does not use are ignored, so a stale
    browser tab cannot rewrite the assignment.
    """
    state = used(root, device)
    remaining = list(state["used"])
    ordered = []
    for i in ids:
        if i in remaining:
            remaining.remove(i)
            ordered.append(i)
    state["used"] = ordered + remaining
    return _save_used(root, device, state)


def clear(root: Path, device: str) -> dict:
    """Unassign everything from this screen. The pool is untouched."""
    return _save_used(root, device, {"current": None, "used": []})


# --------------------------------------------------------------------------
# migration from the two earlier layouts
# --------------------------------------------------------------------------
def _migrate(root: Path) -> None:
    """Adopt per-device libraries, and the original single-`source`, into the
    pool. Runs at most once per layout: both leave no trace behind."""
    if not root.exists():
        return
    pool_items_dir = pool_dir(root) / "items"
    index = _read_json(_pool_index_path(root), [])
    if not isinstance(index, list):
        index = []
    known = {it.get("id") for it in index}
    changed = False

    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name != POOL):
        if not DEVICE_RE.match(d.name):
            continue

        # Layout A: per-device library (items/ + index.json with random ids).
        legacy_index = d / "index.json"
        if legacy_index.exists():
            old = _read_json(legacy_index, {})
            order = []
            for it in old.get("items", []):
                src = d / "items" / str(it.get("id", ""))
                if not src.exists():
                    continue
                body = src.read_bytes()
                item = _describe(
                    body, it.get("content_type", ""), it.get("filename", "image")
                )
                item["uploaded_at"] = it.get("uploaded_at", time.time())
                pool_items_dir.mkdir(parents=True, exist_ok=True)
                if item["id"] not in known:
                    (pool_items_dir / item["id"]).write_bytes(body)
                    index.append(item)
                    known.add(item["id"])
                    changed = True
                if item["id"] not in order:
                    order.append(item["id"])
                # remember the mapping so `current` survives
                if old.get("current") == it.get("id"):
                    old["current"] = item["id"]
                src.unlink(missing_ok=True)
            _write_json(
                _used_path(root, d.name),
                {
                    "current": old.get("current") if old.get("current") in order else (order[0] if order else None),
                    "used": order,
                },
            )
            legacy_index.unlink(missing_ok=True)
            items_subdir = d / "items"
            if items_subdir.exists() and not any(items_subdir.iterdir()):
                items_subdir.rmdir()

        # Layout B: the original single `source` + `meta.json`.
        legacy_source, legacy_meta = d / "source", d / "meta.json"
        if legacy_source.exists():
            meta = _read_json(legacy_meta, {})
            body = legacy_source.read_bytes()
            item = _describe(
                body, meta.get("content_type", ""), meta.get("filename", "image")
            )
            item["uploaded_at"] = meta.get("updated_at", time.time())
            pool_items_dir.mkdir(parents=True, exist_ok=True)
            if item["id"] not in known:
                (pool_items_dir / item["id"]).write_bytes(body)
                index.append(item)
                known.add(item["id"])
                changed = True
            state = _read_json(_used_path(root, d.name), {"current": None, "used": []})
            if item["id"] not in state["used"]:
                state["used"].append(item["id"])
            state["current"] = state["current"] or item["id"]
            _write_json(_used_path(root, d.name), state)
            legacy_source.unlink(missing_ok=True)
            legacy_meta.unlink(missing_ok=True)

    if changed or not _pool_index_path(root).exists():
        _write_json(_pool_index_path(root), index)
