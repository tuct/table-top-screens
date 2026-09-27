"""Image pool plus per-screen assignment.

Two levels, deliberately separate:

    data/_pool/
        index.json          every image ever uploaded, once
        items/<id>          the original uploaded bytes, verbatim
    data/_variants.json     source + framing config; what a screen shows
    data/<device>/
        used.json           which VARIANTS this screen uses, in order, and
                            which one is on it now, plus its panel shape
        prefs.json          this screen's DEFAULTS, used to seed a variant
                            the first time a source lands on it
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

A screen does not show a source, it shows a VARIANT: a source plus the
framing to apply to it. Framing used to be one setting per screen, which made
"this photo needs a tighter crop" impossible to express -- every picture on
that screen moved together.

Variants are keyed by source and PANEL SHAPE ("480x800", "240x240r"), because
that is the distinction that always matters: a portrait panel and a round one
want different crops of the same picture. Putting a source on a screen
auto-creates the variant for that screen's shape, seeded from the screen's
prefs, so nothing has to be framed before it can be shown. Extra variants of
the same source and shape are made by hand (`variant_duplicate`) and carry a
name, for when one picture wants two crops on one screen.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import time
from pathlib import Path

from PIL import Image

import frames

DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ITEM_RE = re.compile(r"^[0-9a-f]{16}$")
# Variant ids are shorter than item ids, so the two can never be confused --
# which matters because used.json held item ids before variants existed.
VARIANT_RE = re.compile(r"^[0-9a-f]{12}$")
# Used when a screen's panel shape is not known yet (it has never been seen
# over mDNS and nothing has rendered for it). Only ever seeds a variant that
# the real shape will replace.
DEFAULT_SHAPE = "800x480"
SHAPE_RE = re.compile(r"^[1-9][0-9]{0,3}x[1-9][0-9]{0,3}r?$")
# Everything a variant can say about how its source is rendered. More will
# join it (crop box, filters); each needs a default here.
CONFIG_KEYS = ("fit", "rot", "zoom", "bg", "q")
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


def _variants_path(root: Path) -> Path:
    return root / "_variants.json"


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
    # against the pool, so afterwards it would report nobody. Screens hold
    # VARIANT ids, so the lookup goes through the variants of this source.
    mine = {v["id"] for v in variants_of(root, item_id)}
    affected = [d for d in devices(root) if mine & set(used(root, d)["used"])]
    for d in affected:
        for variant_id in mine & set(used(root, d)["used"]):
            unassign(root, d, variant_id)
    for variant_id in mine:
        variant_remove(root, variant_id)

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


# --------------------------------------------------------------------------
# variants: a source plus the framing to show it with
# --------------------------------------------------------------------------
def _shape_ok(shape: str) -> str:
    shape = str(shape or "").lower()
    if not SHAPE_RE.match(shape):
        raise LibraryError(f"invalid panel shape {shape!r}, expected e.g. 480x800 or 240x240r")
    return shape


def variants(root: Path) -> list[dict]:
    got = _read_json(_variants_path(root), [])
    return got if isinstance(got, list) else []


def _save_variants(root: Path, value: list[dict]) -> None:
    _write_json(_variants_path(root), value)


def variant(root: Path, variant_id: str) -> dict | None:
    return next((v for v in variants(root) if v["id"] == variant_id), None)


def variants_of(root: Path, src_id: str, shape: str | None = None) -> list[dict]:
    """Every variant of one source, newest last; optionally one shape only."""
    return [
        v for v in variants(root)
        if v["src"] == src_id and (shape is None or v["shape"] == shape)
    ]


def _new_variant(root: Path, src_id: str, shape: str, config: dict, name: str,
                 auto: bool, desc: str = "") -> dict:
    if pool_get(root, src_id) is None:
        raise LibraryError("no such item")
    entry = {
        # Random rather than a hash of the contents: two variants of one
        # source in one shape are a legitimate thing to want, so identical
        # config must not collapse them into one.
        "id": hashlib.sha256(f"{src_id}{shape}{name}{time.time()}".encode()).hexdigest()[:12],
        "src": src_id,
        "shape": _shape_ok(shape),
        "name": str(name or "")[:60],
        # What this framing is for, in the owner's words -- "the face, for the
        # hallway screen". Filenames out of a camera say nothing.
        "desc": str(desc or "")[:200],
        "auto": bool(auto),
        "config": {k: v for k, v in (config or {}).items() if k in CONFIG_KEYS},
        "created_at": time.time(),
    }
    _save_variants(root, variants(root) + [entry])
    return entry


def ensure_variant(root: Path, src_id: str, shape: str, defaults: dict | None = None) -> dict:
    """The automatic variant of `src_id` for `shape`, created if it is new.

    Seeded from `defaults` (the screen's prefs) so a picture put on a screen
    looks the way that screen already looked, and can then be reframed on its
    own without moving anything else.
    """
    shape = _shape_ok(shape)
    for v in variants_of(root, src_id, shape):
        if v.get("auto"):
            return v
    return _new_variant(root, src_id, shape, defaults or {}, "", True)


def variant_duplicate(root: Path, variant_id: str, name: str = "") -> dict:
    """A second variant of the same source and shape, to frame differently."""
    src = variant(root, variant_id)
    if src is None:
        raise LibraryError("no such variant")
    copies = len(variants_of(root, src["src"], src["shape"]))
    return _new_variant(root, src["src"], src["shape"], dict(src["config"]),
                        name or f"copy {copies}", False, src.get("desc", ""))


def variant_set_config(root: Path, variant_id: str, config: dict) -> dict:
    """Merge framing into a variant. Only CONFIG_KEYS are kept."""
    all_of_them = variants(root)
    entry = next((v for v in all_of_them if v["id"] == variant_id), None)
    if entry is None:
        raise LibraryError("no such variant")
    entry["config"].update({k: v for k, v in (config or {}).items() if k in CONFIG_KEYS})
    _save_variants(root, all_of_them)
    return entry


def variant_set_config_exact(root: Path, variant_id: str, config: dict) -> dict:
    """Replace a variant's config outright. `{}` means "no framing of its
    own", which falls back to the screen's defaults."""
    all_of_them = variants(root)
    entry = next((v for v in all_of_them if v["id"] == variant_id), None)
    if entry is None:
        raise LibraryError("no such variant")
    entry["config"] = {k: v for k, v in (config or {}).items() if k in CONFIG_KEYS}
    _save_variants(root, all_of_them)
    return entry


def variant_set_labels(root: Path, variant_id: str, name=None, desc=None) -> dict:
    """Set a variant's name and/or description. None leaves one alone."""
    all_of_them = variants(root)
    entry = next((v for v in all_of_them if v["id"] == variant_id), None)
    if entry is None:
        raise LibraryError("no such variant")
    if name is not None:
        entry["name"] = str(name)[:60]
    if desc is not None:
        entry["desc"] = str(desc)[:200]
    _save_variants(root, all_of_them)
    return entry


def variant_remove(root: Path, variant_id: str) -> None:
    """Forget a variant, and drop it from every screen that used it."""
    keep = [v for v in variants(root) if v["id"] != variant_id]
    _save_variants(root, keep)
    for device in devices(root):
        state = _read_json(_used_path(root, device), {})
        if variant_id in state.get("used", []):
            try:
                unassign(root, device, variant_id)
            except LibraryError:
                pass


def prune_variants(root: Path) -> int:
    """Drop variants whose source has left the pool. Returns how many."""
    ids = {it["id"] for it in pool(root)}
    stale = [v["id"] for v in variants(root) if v["src"] not in ids]
    for variant_id in stale:
        variant_remove(root, variant_id)
    return len(stale)


def _seen_path(root: Path) -> Path:
    return root / "_screens.json"


def seen(root: Path) -> dict:
    """Every screen we have ever met, by name.

    Discovery is the only way a screen is found, and it lives in memory -- so
    without this a screen that is asleep when the server starts has no card at
    all, and one that was never given content disappears on every restart.
    What is kept is what a card needs while the screen is quiet (its size and
    shape) plus where to look for it (host and port).
    """
    got = _read_json(_seen_path(root), {})
    return got if isinstance(got, dict) else {}


def note_seen(root: Path, device: str, info: dict) -> dict:
    """Remember a screen we can see right now."""
    if not DEVICE_RE.match(device):
        raise LibraryError("invalid device name")
    known = seen(root)
    entry = {**known.get(device, {}),
             **{k: v for k, v in info.items() if v is not None}}
    entry["at"] = time.time()
    known[device] = entry
    _write_json(_seen_path(root), known)
    return entry


def forget_seen(root: Path, device: str) -> None:
    known = seen(root)
    if known.pop(device, None) is not None:
        _write_json(_seen_path(root), known)


def device_remove(root: Path, device: str) -> bool:
    """Forget a screen: its playlist, its defaults and its last report.

    The pictures are NOT touched -- they live in the pool, which is shared,
    and a screen being retired is no reason to lose them. Scenes lose the
    entry for this screen and keep the rest, for the same reason: a scene is
    a convenience, not a record that has to stay whole.
    """
    folder = device_dir(root, device)
    remembered = device in seen(root)
    if not folder.exists() and not remembered:
        return False
    if folder.exists():
        shutil.rmtree(folder)
    # Forgotten for good: without this the screen comes straight back as an
    # offline card, which is not what "remove" means.
    forget_seen(root, device)

    stored = scenes(root)
    trimmed = False
    for scene in stored:
        if device in (scene.get("entries") or {}):
            del scene["entries"][device]
            trimmed = True
    if trimmed:
        _write_json(_scenes_path(root), stored)

    prune_unused_variants(root)
    return True


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


def _capture(root: Path, device: str) -> dict:
    """What a screen is showing right now, as a scene entry."""
    state = used(root, device)
    entry = variant(root, state["current"]) if state["current"] else None
    return {
        "item": state["current"],
        "prefs": prefs(root, device),
        # The variant's framing as it is now: variants are edited in place, so
        # without this a restored scene would put the right picture back
        # wearing whatever framing it has since been given.
        "config": dict(entry["config"]) if entry else {},
    }


def _scene_entries(root: Path, only: set[str] | None, live: set[str] | None,
                   old: dict, keep_offline: bool) -> dict:
    """The entries a save or update should end up with.

    A screen is captured only while it is on the network -- there is no point
    putting a screen into a scene you cannot drive. One that is already in the
    scene and has since gone quiet keeps the entry it was saved with, rather
    than being dropped for being asleep.

    `keep_offline` is the difference between the two callers: saving keeps
    every offline entry, because the form that saves cannot offer offline
    screens at all; editing keeps only the offline entries still ticked, so
    one can be taken out on purpose.
    """
    entries = {}
    for device in devices(root):
        chosen = only is None or device in only
        if live is not None and device not in live:
            if device in old and (keep_offline or chosen):
                entries[device] = old[device]
            continue
        if not chosen:
            continue
        entries[device] = _capture(root, device)
    return entries


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise LibraryError("a scene needs a name")
    if len(name) > 60:
        raise LibraryError("name too long")
    return name


def scene_save(root: Path, name: str, only: set[str] | None = None,
               live: set[str] | None = None) -> dict:
    """Capture what the chosen screens are showing, under a name.

    `only` is which screens to take (None: every one we hold state for), and
    `live` which are on the network (None: treat them all as reachable, for
    callers that are not the page).
    """
    name = _check_name(name)
    # Replacing a scene of the same name is what you almost always mean.
    previous = next((s for s in scenes(root) if s.get("name") == name), None)
    old = (previous or {}).get("entries") or {}
    scene = {
        "id": hashlib.sha256(f"{name}{time.time()}".encode()).hexdigest()[:12],
        "name": name,
        # A description outlives a re-save: it says what the scene is for,
        # which does not change because the pictures did.
        "desc": str((previous or {}).get("desc", "")),
        "saved_at": time.time(),
        # Saving puts you in the scene you just saved: it is, by definition,
        # what the screens are showing.
        "applied_at": time.time(),
        "entries": _scene_entries(root, only, live, old, keep_offline=True),
    }
    keep = [s for s in scenes(root) if s.get("name") != name]
    _write_json(_scenes_path(root), keep + [scene])
    return scene


def scene_save_as(root: Path, source_id: str, name: str,
                  only: set[str] | None = None,
                  live: set[str] | None = None) -> dict:
    """Save what is up now as a NEW scene, leaving the one it came from alone.

    Never replaces by name -- that is the whole point of "save as" -- so a
    name already taken becomes "... copy". The new scene is the one you are
    then working in, because it is what is on the screens.
    """
    source = scene_get(root, source_id)
    if source is None:
        raise LibraryError("no such scene")
    name = _check_name(name)
    if any(s.get("name") == name for s in scenes(root)):
        name = _free_name(root, name)
    scene = {
        "id": hashlib.sha256(f"{name}{time.time()}".encode()).hexdigest()[:12],
        "name": name,
        "desc": str(source.get("desc", "")),
        "saved_at": time.time(),
        "applied_at": time.time(),
        "entries": _scene_entries(root, only, live,
                                  source.get("entries") or {}, keep_offline=True),
    }
    _write_json(_scenes_path(root), scenes(root) + [scene])
    return scene


def scene_set_labels(root: Path, scene_id: str, name: str | None = None,
                     desc: str | None = None) -> dict:
    """Rename or describe a scene. Touches nothing it recorded."""
    stored = scenes(root)
    scene = next((s for s in stored if s.get("id") == scene_id), None)
    if scene is None:
        raise LibraryError("no such scene")
    if name is not None:
        new = _check_name(name)
        if any(s.get("name") == new and s is not scene for s in stored):
            raise LibraryError("another scene already has that name")
        scene["name"] = new
    if desc is not None:
        scene["desc"] = str(desc).strip()[:200]
    _write_json(_scenes_path(root), stored)
    return scene


def scene_update(root: Path, scene_id: str, only: set[str] | None = None,
                 live: set[str] | None = None, name: str | None = None) -> dict:
    """Edit a scene in place: same id, same place in the list.

    Saving is one act, not two: the name, the membership AND what the live
    screens show are all taken as they are now. Keeping the old pictures
    while changing the name would be a second, quieter meaning of "save".
    """
    stored = scenes(root)
    scene = next((s for s in stored if s.get("id") == scene_id), None)
    if scene is None:
        raise LibraryError("no such scene")
    old = scene.get("entries") or {}
    if name is not None:
        scene["name"] = _check_name(name)
    scene["saved_at"] = time.time()
    scene["entries"] = _scene_entries(root, only, live, old, keep_offline=False)
    _write_json(_scenes_path(root), stored)
    return scene


def scene_duplicate(root: Path, scene_id: str, name: str = "") -> dict:
    """Copy a scene, entries and all, under a new name."""
    stored = scenes(root)
    scene = next((s for s in stored if s.get("id") == scene_id), None)
    if scene is None:
        raise LibraryError("no such scene")
    name = (name or "").strip() or _free_name(root, str(scene.get("name", "scene")))
    copy = {
        "id": hashlib.sha256(f"{name}{time.time()}".encode()).hexdigest()[:12],
        "name": _check_name(name),
        "desc": str(scene.get("desc", "")),
        "saved_at": time.time(),
        "entries": json.loads(json.dumps(scene.get("entries") or {})),
    }
    keep = [s for s in stored if s.get("name") != copy["name"]]
    _write_json(_scenes_path(root), keep + [copy])
    return copy


def _free_name(root: Path, base: str) -> str:
    """"Evening" -> "Evening copy" -> "Evening copy 2"."""
    taken = {s.get("name") for s in scenes(root)}
    candidate = f"{base} copy"[:60]
    n = 2
    while candidate in taken:
        candidate = f"{base} copy {n}"[:60]
        n += 1
    return candidate


def _framing_key(config: dict) -> dict:
    """The part of a config worth comparing, as plain strings.

    Only the keys that change what is rendered, and only those actually set --
    so an absent zoom and a zoom of None are the same thing, and a value that
    came back from a form as "150" matches one stored as 150. Values that do
    nothing are dropped as well: zoom 100 and no zoom at all render the same
    picture, so calling them different would star a scene nobody changed.
    """
    idle = {"zoom": "100", "rot": "0"}
    got = {k: str(config[k]) for k in CONFIG_KEYS if config.get(k) is not None}
    return {k: v for k, v in got.items() if idle.get(k) != v}


def scene_on_screen(root: Path, scene_id: str) -> bool:
    """True when every screen in the scene still shows what it recorded.

    This is what "loaded" means here: not that the scene was the last one
    applied, but that nothing has been changed since -- and framing counts as
    much as the picture does, because a scene restores both. An empty scene is
    never on screen: there is nothing for it to be true of.
    """
    scene = scene_get(root, scene_id)
    entries = (scene or {}).get("entries") or {}
    if not entries:
        return False
    known = set(devices(root))
    for device, entry in entries.items():
        if device not in known:
            return False
        if used(root, device)["current"] != entry.get("item"):
            return False
        # What applying the scene would put in effect, against what is in
        # effect now: prefs seed the variant, the variant's own config wins.
        want = {**(entry.get("prefs") or {}), **(entry.get("config") or {})}
        if _framing_key(want) != _framing_key(config_for(root, device)):
            return False
    return True


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
    known = {v["id"] for v in variants(root)}
    changed = []
    for device, entry in (scene.get("entries") or {}).items():
        if not DEVICE_RE.match(device):
            continue
        item = entry.get("item")
        if isinstance(entry.get("prefs"), dict):
            set_prefs(root, device, entry["prefs"])
        # Scenes recorded before variants hold pool ids; assign() takes both.
        if item in known or item in ids:
            assign(root, device, item, make_current=True)
            if item in known and isinstance(entry.get("config"), dict):
                variant_set_config_exact(root, item, entry["config"])
            changed.append(device)
        elif isinstance(entry.get("prefs"), dict):
            changed.append(device)
    # Noted so the page can say when a scene was last put up, even after the
    # screens have moved on from it.
    stored = scenes(root)
    for s in stored:
        if s.get("id") == scene_id:
            s["applied_at"] = time.time()
    _write_json(_scenes_path(root), stored)
    return changed


def scene_release(root: Path) -> None:
    """Stop working in a scene, without deleting anything.

    Only the "which scene am I in" mark is dropped -- every scene keeps its
    screens and its pictures. The shelf then shows every screen again, and
    nothing is starred, because no scene claims to be up.
    """
    stored = scenes(root)
    for scene in stored:
        scene.pop("applied_at", None)
    _write_json(_scenes_path(root), stored)


def scene_remove(root: Path, scene_id: str) -> None:
    remaining = [s for s in scenes(root) if s.get("id") != scene_id]
    if len(remaining) == len(scenes(root)):
        raise LibraryError("no such scene")
    _write_json(_scenes_path(root), remaining)


# --------------------------------------------------------------------------
# per-screen assignment
# --------------------------------------------------------------------------
def shape_of(root: Path, device: str) -> str:
    """The panel shape last recorded for this screen."""
    state = _read_json(_used_path(root, device), {})
    shape = state.get("shape")
    try:
        return _shape_ok(shape)
    except LibraryError:
        return DEFAULT_SHAPE


def set_shape(root: Path, device: str, shape: str) -> str:
    """Record the screen's panel shape, so variants are made for the right one.

    Called when a screen is discovered. Kept here rather than read from the
    registry each time so the server still knows the shape while the screen
    is offline.

    Changing it RE-KEYS what the screen shows: each variant is swapped for one
    of the same source at the new shape, carrying its framing over. Two things
    need that. A screen's lists can be migrated from the pre-variant layout
    before it has ever been seen, so they start at the default shape and would
    otherwise share variants with every other screen -- reframing a picture on
    one screen would move it on the others. And a panel really can change, if
    a screen is rebuilt on different hardware.
    """
    shape = _shape_ok(shape)
    state = _read_json(_used_path(root, device), {})
    old = state.get("shape")
    if old == shape:
        return shape
    state["shape"] = shape
    _write_json(_used_path(root, device), state)
    if old and state.get("used"):
        _rekey_shape(root, device, shape)
    return shape


def _rekey_shape(root: Path, device: str, shape: str) -> None:
    """Point a screen's list at variants of `shape`, keeping their framing."""
    state = _read_json(_used_path(root, device), {})
    by_id = {v["id"]: v for v in variants(root)}
    mapping: dict[str, str] = {}
    for variant_id in state.get("used", []):
        entry = by_id.get(variant_id)
        if entry is None or entry["shape"] == shape:
            continue
        if entry.get("auto"):
            match = next(
                (v for v in variants_of(root, entry["src"], shape) if v.get("auto")), None
            )
        else:
            # A named variant keeps its name at the new shape, so "face" stays
            # "face" rather than silently merging with the plain one.
            match = next(
                (v for v in variants_of(root, entry["src"], shape)
                 if v["name"] == entry["name"]), None
            )
        if match is None:
            match = _new_variant(root, entry["src"], shape, dict(entry["config"]),
                                 entry["name"], entry.get("auto", False))
        mapping[variant_id] = match["id"]

    if not mapping:
        return
    state["used"] = [mapping.get(i, i) for i in state.get("used", [])]
    if state.get("current") in mapping:
        state["current"] = mapping[state["current"]]
    _write_json(_used_path(root, device), state)
    prune_unused_variants(root)


def prune_unused_variants(root: Path) -> int:
    """Drop automatic variants no screen shows. Named ones are kept: someone
    made those on purpose and may be between screens with them."""
    shown: set[str] = set()
    for device in devices(root):
        shown.update(_read_json(_used_path(root, device), {}).get("used", []))
    keep = [v for v in variants(root) if v["id"] in shown or not v.get("auto")]
    dropped = len(variants(root)) - len(keep)
    if dropped:
        _save_variants(root, keep)
    return dropped


def used(root: Path, device: str) -> dict:
    """{"current": id|None, "used": [variant ids], "shape": str}.

    Also migrates: these lists held POOL item ids before variants existed, so
    any entry that names a source is converted to that source's automatic
    variant for this screen's shape, seeded from the screen's prefs. A screen
    therefore looks exactly as it did before variants, with each picture now
    free to be reframed on its own.
    """
    # Read the pool FIRST: pool() is what runs the migration, and the
    # migration is what creates used.json for a device coming from an older
    # layout. Reading used.json before that returned an empty state on the
    # very first call, so a just-migrated screen reported nothing on it.
    sources = {it["id"] for it in pool(root)}
    known = {v["id"] for v in variants(root)}
    state = _read_json(_used_path(root, device), {})
    shape = shape_of(root, device)

    order: list[str] = []
    changed = False
    for entry in state.get("used", []):
        if entry in known:
            order.append(entry)
        elif entry in sources:
            order.append(ensure_variant(root, entry, shape, prefs(root, device))["id"])
            changed = True
        else:
            changed = True  # the source or variant is gone

    current = state.get("current")
    if current not in order:
        if current in sources:
            current = next(
                (v["id"] for v in variants_of(root, current, shape) if v.get("auto")), None
            )
        if current not in order:
            current = order[0] if order else None
        changed = True

    if changed:
        _save_used(root, device, {**state, "used": order, "current": current})

    # Self-heal a shape mismatch: lists migrated before the screen was ever
    # seen point at variants made for the default shape, which other screens
    # would then share -- reframing on one would move the others.
    by_variant = {v["id"]: v for v in variants(root)}
    if any(by_variant[i]["shape"] != shape for i in order if i in by_variant):
        _rekey_shape(root, device, shape)
        state = _read_json(_used_path(root, device), {})
        order = list(state.get("used", []))
        current = state.get("current")

    return {"current": current, "used": order, "shape": shape}


def _save_used(root: Path, device: str, state: dict) -> dict:
    _write_json(_used_path(root, device), state)
    return state


def item_view(source: dict, entry: dict) -> dict:
    """One row for the UI: the source's facts under the variant's identity.

    `id` is the VARIANT id, because that is what a screen shows and what
    every assignment route takes. The source id stays available as `src_id`
    for the pool routes (thumbnails, raw bytes), which are per source.
    """
    return {
        **source,
        "id": entry["id"],
        "src_id": source["id"],
        "variant": entry["name"],
        "desc": entry.get("desc", ""),
        "shape": entry["shape"],
        "config": entry["config"],
        "auto": entry.get("auto", False),
    }


def used_items(root: Path, device: str) -> list[dict]:
    """The screen's variants, in its order, each with its source's facts."""
    by_id = {it["id"]: it for it in pool(root)}
    by_variant = {v["id"]: v for v in variants(root)}
    out = []
    for variant_id in used(root, device)["used"]:
        entry = by_variant.get(variant_id)
        source = by_id.get(entry["src"]) if entry else None
        if entry and source:
            out.append(item_view(source, entry))
    return out


def unused_items(root: Path, device: str) -> list[dict]:
    """Pool sources this screen shows no variant of.

    Sources, not variants: the point of the list is "what else could go on
    here", and putting one on makes the variant.
    """
    by_variant = {v["id"]: v for v in variants(root)}
    shown = {
        by_variant[i]["src"] for i in used(root, device)["used"] if i in by_variant
    }
    return [it for it in pool(root) if it["id"] not in shown]


def resolve(root: Path, device: str, ident: str) -> str:
    """A variant id from either a variant id or a source id.

    Assignment routes take both: a source id means "put this picture on that
    screen", which is the automatic variant for the screen's shape.
    """
    if variant(root, ident) is not None:
        return ident
    if pool_get(root, ident) is None:
        raise LibraryError("no such item")
    return ensure_variant(root, ident, shape_of(root, device), prefs(root, device))["id"]


def current(root: Path, device: str) -> dict | None:
    """The SOURCE on this screen -- its bytes and metadata.

    Still the source, not the variant, because every caller wants the picture
    itself: its hash, its frame count, the bytes to render. Ask
    current_variant() for the framing to render it with.
    """
    entry = current_variant(root, device)
    return pool_get(root, entry["src"]) if entry else None


def current_variant(root: Path, device: str) -> dict | None:
    """The variant on this screen: which source, framed how."""
    state = used(root, device)
    return variant(root, state["current"]) if state["current"] else None


def config_for(root: Path, device: str) -> dict:
    """Framing to render this screen's current content with.

    The variant's own config, over the screen's defaults. The fallback matters
    for a variant whose config was reset, and for a screen showing something
    that predates variants.
    """
    entry = current_variant(root, device)
    return {**prefs(root, device), **((entry or {}).get("config") or {})}


def assign(root: Path, device: str, item_id: str, make_current: bool = True) -> dict:
    item_id = resolve(root, device, item_id)
    state = used(root, device)
    if item_id not in state["used"]:
        state["used"].append(item_id)
    if make_current or state["current"] is None:
        state["current"] = item_id
    return _save_used(root, device, state)


def unassign(root: Path, device: str, item_id: str) -> dict:
    """Stop showing a variant on this screen. Pool and variant both stay.

    A source id works too and removes every variant of it this screen shows,
    which is what "take this picture off that screen" means.
    """
    state = used(root, device)
    if item_id not in state["used"]:
        by_variant = {v["id"]: v for v in variants(root)}
        mine = [i for i in state["used"] if by_variant.get(i, {}).get("src") == item_id]
        if not mine:
            raise LibraryError("no such item")
        for variant_id in mine[1:]:
            unassign(root, device, variant_id)
        return unassign(root, device, mine[0])
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
    """Put a variant on the screen, assigning it first if need be. Takes a
    source id too, which means its automatic variant for this shape."""
    return assign(root, device, item_id, make_current=True)


def reorder(root: Path, device: str, ids: list[str]) -> dict:
    """Apply a drag-and-drop order.

    Tolerant on purpose: ids the client did not mention keep their relative
    order at the end, and ids this screen does not use are ignored, so a stale
    browser tab cannot rewrite the assignment.
    """
    state = used(root, device)
    remaining = list(state["used"])
    by_variant = {v["id"]: v for v in variants(root)}
    ordered = []
    for i in ids:
        # Source ids are accepted as well as variant ids: a client that knows
        # only which pictures it dragged should not have to resolve them.
        if i not in remaining:
            i = next((u for u in remaining if by_variant.get(u, {}).get("src") == i), i)
        if i in remaining:
            remaining.remove(i)
            ordered.append(i)
    state["used"] = ordered + remaining
    return _save_used(root, device, state)


def clear(root: Path, device: str) -> dict:
    """Unassign everything from this screen. Pool and variants are untouched."""
    state = _read_json(_used_path(root, device), {})
    return _save_used(root, device, {**state, "current": None, "used": []})


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
