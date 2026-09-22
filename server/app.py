#!/usr/bin/env python3
"""Content server for the tabletop mini screens.

One server, N screens, N resolutions, nothing configured on either side.

Screens are found over mDNS and told where to fetch from (see discovery.py),
so no address is typed anywhere -- being on the same network is the only
requirement. A screen states its own panel size in its mDNS TXT records and
receives a content URL carrying those dimensions, so this server never keeps
a device list of its own; adding a screen with a different panel needs no
change here.

Fetches are served with a strong ETag over (source bytes + render
parameters). New content is pushed (the screen's refresh button is pressed
over its REST API) so it lands in well under a second; the screen's slow
background poll is only a safety net, and costs one 304 with an empty body
when nothing has changed.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import struct
import time
from collections import OrderedDict
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request
from PIL import Image, ImageColor, ImageOps, ImageStat

import discovery
import frames
import library

# numpy is worth having on its own, independently of QOI: it makes RGB565
# packing a vectorised shift instead of a per-pixel Python loop, which at
# 800x480 is the difference between milliseconds and most of a second.
try:
    import numpy as np

    HAVE_NUMPY = True
except ImportError:  # pragma: no cover
    HAVE_NUMPY = False

try:  # optional: lossless and much faster to decode on-device than PNG
    import qoi as qoi_lib

    HAVE_QOI = HAVE_NUMPY
except ImportError:  # pragma: no cover
    HAVE_QOI = False

DATA_DIR = Path(__file__).parent / "data"
PORT = 8099
# The panels are RGB565, so they can only show ~65k colours. Past ~95 the extra
# bytes encode detail the hardware physically cannot display, so this is the
# point of diminishing returns rather than 100. Paired with subsampling=0 in
# render(), which matters far more than quality for sharp colour edges.
DEFAULT_QUALITY = 95
MAX_UPLOAD = 32 * 1024 * 1024
RENDER_CACHE_SIZE = 32
DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD

# Screens announce themselves over mDNS; this browses for them and pushes each
# one its content URL. Nothing here needs to be told an address.
#
# SCREENS_DISCOVERY=0 turns that off and SCREENS_ONLY=a,b limits it to named
# screens. Both exist for the tests: importing this module used to start real
# discovery, which pushed test content to -- and pressed Refresh on -- any real
# screen on the LAN sharing a test's device name.
registry, _zc = discovery.start(
    PORT,
    browse=os.environ.get("SCREENS_DISCOVERY", "1") != "0",
    only=(
        {n.strip() for n in os.environ["SCREENS_ONLY"].split(",") if n.strip()}
        if os.environ.get("SCREENS_ONLY")
        else None
    ),
)


def anim_format(item: dict) -> str | None:
    """The capability name for an animated item's source: gif, apng or webp.

    Pillow calls an APNG "PNG", so the name is mapped rather than lowercased.
    None for a still or a format no screen could advertise.
    """
    if not item.get("animated"):
        return None
    return {"GIF": "gif", "PNG": "apng", "WEBP": "webp"}.get(
        str(item.get("source_format", "")).upper()
    )


def plays_as_still(screen, item: dict | None) -> bool:
    """True when `item` is animated but `screen` cannot play it, so it shows
    the first frame instead."""
    if not item or not item.get("animated"):
        return False
    fmt = anim_format(item)
    return fmt is None or not screen.can_animate(fmt)


def _frames_for(screen) -> int:
    """Frame count of what `screen` is showing, as it should play it; 0 if
    nothing.

    Comes from the metadata recorded at upload, so a still is 1 and a clip is
    its real length -- no decoding here. The device caches and auto-plays when
    this is > 1, which is what makes animation need no button press. A screen
    that did not advertise the clip's format gets 1: `/image` already renders
    frame 0, so it simply shows a still.
    """
    try:
        item = library.current(DATA_DIR, screen.name)
    except library.LibraryError:
        return 0
    if not item:
        return 0
    if plays_as_still(screen, item):
        return 1
    return int(item.get("frames", 1) or 1)


def _version_for(device: str) -> str | None:
    """A token that names exactly what `device` is showing: item + framing.

    The pool stores items under a content hash, so the item id already names
    the picture. The screen's stored prefs (fit, rotation, zoom, background,
    quality) change what it receives just as much, so they are folded in: a
    screen that caches rendered content by this token must never be handed a
    token it already holds for different pixels. And the same item with the
    same prefs always gives the same token, which is what lets a screen switch
    back to something it has without pulling it again.
    """
    try:
        item = library.current(DATA_DIR, device)
    except library.LibraryError:
        return None
    item_id = (item or {}).get("id")
    if not item_id:
        return None
    try:
        prefs = library.prefs(DATA_DIR, device)
    except library.LibraryError:
        prefs = {}
    if not prefs:
        return item_id
    digest = hashlib.sha256(json.dumps(prefs, sort_keys=True).encode("utf-8")).hexdigest()
    return f"{item_id}.{digest[:8]}"


registry.frames_provider = _frames_for
registry.version_provider = _version_for

# (device, source_hash, params) -> (body, content_type)
_render_cache: OrderedDict[tuple, tuple[bytes, str]] = OrderedDict()
# (source_hash, params) -> (clip bytes, frame count). Few entries: a clip is
# megabytes, and only the screens' current clips are ever asked for.
_clip_cache: OrderedDict[tuple, tuple[bytes, int]] = OrderedDict()
CLIP_CACHE_SIZE = 4


# --------------------------------------------------------------------------
# storage (see library.py -- every image sent is kept, in playlist order)
# --------------------------------------------------------------------------
def device_dir(device: str) -> Path:
    try:
        return library.device_dir(DATA_DIR, device)
    except library.LibraryError as exc:
        abort(400, str(exc))


def read_prefs(device: str) -> dict:
    """Human-set render overrides for a device.

    Precedence: these win over the query string for `fit`, `rot`, `q` and
    `bg`, because they are a deliberate choice made after the device
    announced its defaults over mDNS. `w`/`h` always come from the request --
    those are hardware facts, not preferences. `prefs=0` opts out entirely.
    """
    return lib(library.prefs, device)


def write_prefs(device: str, prefs: dict) -> None:
    lib(library.set_prefs, device, prefs)


def read_meta(device: str) -> dict:
    """Metadata of the image currently on this screen, or {} if none."""
    try:
        return library.current(DATA_DIR, device) or {}
    except library.LibraryError as exc:
        abort(400, str(exc))


# Animate a list thumbnail from the original file up to this size. Beyond it,
# a static first frame plus a badge -- a 20 MB GIF in a list of twenty would
# otherwise be 400 MB of page weight.
RAW_THUMB_MAX = 2 * 1024 * 1024


def motion_of(item: dict) -> dict:
    """Motion facts for a pool item, computed at upload.

    Falls back to decoding for entries written before the fields existed, so
    an existing pool does not have to be re-uploaded to gain badges.
    """
    if "animated" in item:
        return item
    try:
        return {**item, **frames.describe_motion(lib(library.body_of, item["id"]))}
    except Exception:  # noqa: BLE001
        return {**item, "animated": False, "frames": 1, "duration_ms": 0,
                "browser_playable": False}


def thumb_src(item: dict) -> str:
    """Where a list thumbnail should come from.

    For a clip the browser can play, that is the original file, so the
    thumbnail animates with no server-side clip encoding at all.
    """
    m = motion_of(item)
    if (
        m.get("animated")
        and m.get("browser_playable")
        and item.get("bytes", 0) <= RAW_THUMB_MAX
    ):
        return f"/pool/{item['id']}/raw"
    return f"/pool/{item['id']}/thumb"


def motion_badge(item: dict) -> str:
    m = motion_of(item)
    if not m.get("animated"):
        return ""
    secs = (m.get("duration_ms") or 0) / 1000
    length = f", {secs:.1f}s" if secs else ""
    return (
        f'<span class="anim" title="Animated {item.get("source_format", "")}">'
        f'&#9658; {m.get("frames", "?")} frames{length}</span>'
    )


def caps_html(screen) -> str:
    """One line of what a screen can do, for its card and page."""
    if screen.anim is None:
        motion = "animation unknown"
    elif screen.anim:
        motion = "plays " + "/".join(screen.anim)
    else:
        motion = "stills only"
    parts = [f"{screen.width}x{screen.height}"]
    if screen.round:
        parts.append("round")
    parts += [motion, "img " + "/".join(screen.img)]
    if screen.clip:
        parts.append("clips " + screen.clip)
    html = '<small class="caps">' + " &middot; ".join(parts) + "</small>"
    if screen.sd:
        html += "<br>" + sd_html(screen.sd_state)
    return html


def sd_html(state: dict | None) -> str:
    """The screen's SD card: whether stills are on it, and free of total."""
    if not state:
        return '<small class="caps">SD card: not read yet</small>'
    if not state.get("mounted"):
        return '<small class="sdwarn">SD card: none mounted &mdash; stills over the network</small>'
    use = ('<span class="sdon">stills on card</span>' if state.get("in_use")
           else '<span class="sdwarn">mounted, not used</span>')
    total, free = state.get("total_mb"), state.get("free_mb")
    if total and free is not None:
        pct = 100 * (total - free) / total
        size = (f" &middot; {free / 1024:.1f} of {total / 1024:.1f} GB free"
                f' <meter min="0" max="100" value="{pct:.0f}" '
                f'title="{pct:.0f}% used"></meter>')
    else:
        size = ""
    return f'<small class="caps">SD card: {use}{size}</small>'


def parse_cache_key(key: str) -> dict:
    """What a screen's cache key names.

    Keys are `<item id>[.<prefs hash>]-f<fps>-q<quality>` for server content
    (see mjpeg-clip.yaml) and `file:<name>` for a file copied onto the card.
    """
    if key.startswith("file:"):
        return {"file": key[5:], "item_id": None, "fps": None, "framed": False}
    m = re.match(r"^([0-9A-Za-z]+)(\.[0-9a-f]+)?(?:-f(\d+))?", key)
    if not m:
        return {"file": None, "item_id": None, "fps": None, "framed": False}
    return {"file": None, "item_id": m.group(1), "fps": int(m.group(3)) if m.group(3) else None,
            "framed": bool(m.group(2))}


def cache_entries(state: dict | None) -> list[dict]:
    """A screen's report as one row per cached key: where it is, and its size."""
    report = (state or {}).get("report") or {}
    rows: dict[str, dict] = {}
    for entry in report.get("mem") or []:
        if isinstance(entry, list) and len(entry) >= 2:
            rows.setdefault(str(entry[0]), {})["mem"] = int(entry[1])
            rows[str(entry[0])]["frames"] = int(entry[2]) if len(entry) > 2 else None
    for entry in report.get("card") or []:
        if isinstance(entry, list) and len(entry) >= 2:
            rows.setdefault(str(entry[0]), {})["card"] = int(entry[1])
    shown = report.get("shown")
    out = []
    for key, row in rows.items():
        out.append({"key": key, "shown": key == shown, **parse_cache_key(key), **row})
    # On screen first, then memory, then card only.
    out.sort(key=lambda r: (not r["shown"], "mem" not in r, r["key"]))
    return out


def _kb(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, n // 1024)} kB"


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = max(0, int(time.time() - ts))
    if s < 60:
        return f"{s} s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def memory_html(device: str, detailed: bool = False) -> str:
    """What the screen last said it holds: memory in use, and each cached item."""
    state = lib(library.screen_state, device)
    report = (state or {}).get("report")
    if not report:
        return ""
    budget, used = report.get("budget") or 0, report.get("used") or 0
    pct = 100 * used / budget if budget else 0
    line = (f'<small class="caps">Memory cache: {_kb(used)} of {_kb(budget)} '
            f'<meter min="0" max="100" value="{pct:.0f}" title="{pct:.0f}% used"></meter>')
    if report.get("psram_total"):
        line += f' &middot; PSRAM {_kb(report.get("psram_free"))} free of {_kb(report["psram_total"])}'
    if report.get("heap_free") is not None:
        line += f' &middot; heap {_kb(report["heap_free"])} free'
    line += f' &middot; reported {_ago(state.get("at"))}</small>'

    rows = cache_entries(state)
    if not rows:
        return line + '<br><small class="caps">Nothing cached.</small>'
    names = {it["id"]: it["filename"] for it in lib(library.pool)}
    lis = []
    for r in rows:
        name = r["file"] if r["file"] else names.get(r["item_id"], r["item_id"] or r["key"])
        where = []
        if "mem" in r:
            frames = f', {r["frames"]} frames' if r.get("frames") and r["frames"] > 1 else ""
            where.append(f'memory {_kb(r["mem"])}{frames}')
        if "card" in r:
            where.append(f'card {_kb(r["card"])}')
        extra = []
        if r["fps"]:
            extra.append(f'{r["fps"]} fps')
        if r["framed"]:
            extra.append("custom framing")
        if r["file"]:
            extra.append("SD file")
        lis.append(
            f'<li>{"<b>" if r["shown"] else ""}{name}{"</b> &mdash; on screen" if r["shown"] else ""}'
            f' <small class="caps">{" &middot; ".join(where)}'
            f'{" (" + ", ".join(extra) + ")" if extra else ""}</small></li>'
        )
    in_mem = sum(1 for r in rows if "mem" in r)
    on_card = sum(1 for r in rows if "card" in r)
    summary = f"Cached: {in_mem} in memory" + (f", {on_card} on card" if report.get("card") is not None else "")
    body = f'<ul class="cache">{"".join(lis)}</ul>'
    if detailed:
        return f"{line}<br><b>{summary}</b>{body}"
    return f"{line}<details><summary><small>{summary}</small></summary>{body}</details>"


def cache_badge(rows: list[dict], item_id: str) -> str:
    """Where a library item is cached on the screen, for its row."""
    mem = sum(r.get("mem", 0) for r in rows if r["item_id"] == item_id)
    card = sum(r.get("card", 0) for r in rows if r["item_id"] == item_id)
    if not mem and not card:
        return ""
    parts = ([f"memory {_kb(mem)}"] if mem else []) + ([f"card {_kb(card)}"] if card else [])
    return f'<span class="cached" title="Held by the screen: switching to it needs no download">cached: {" &middot; ".join(parts)}</span>'


def still_badge(screen, item: dict | None) -> str:
    if screen is None or not plays_as_still(screen, item):
        return ""
    return ('<span class="still" title="This screen does not play '
            f'{anim_format(item) or "this format"}; it shows the first frame">'
            "still here</span>")


def redirect_target(default: str) -> str:
    """Where a browser form should land afterwards.

    Honours a `return_to` field so a control on the main page returns there
    instead of jumping to the device page. Restricted to a local path -- a
    bare "/..." that is not "//..." -- so this cannot be turned into an open
    redirect.
    """
    want = (request.form.get("return_to") or "").strip()
    if want.startswith("/") and not want.startswith("//"):
        return want
    return default


def from_browser_form() -> bool:
    """True when a browser submitted an HTML form, so we should redirect back
    to the page instead of answering with JSON.

    Two conditions, and both are needed:

    * A form content type. Not `request.form` being non-empty -- the "Show"
      and remove buttons submit forms with **no fields at all**, so a
      truthiness test on `request.form` hands the browser a JSON document
      instead of the page.
    * The client prefers HTML over JSON. `curl -F file=@x.jpg` also sends
      multipart, and it wants the documented JSON back; it sends `Accept: */*`,
      which scores HTML and JSON equally, whereas a browser ranks text/html
      above the `*/*` that matches JSON.
    """
    if request.mimetype not in (
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    ):
        return False
    accept = request.accept_mimetypes
    return accept["text/html"] > accept["application/json"]


def lib(fn, *args):
    """Call a library function, mapping its errors onto HTTP 4xx."""
    try:
        return fn(DATA_DIR, *args)
    except library.LibraryError as exc:
        msg = str(exc)
        abort(404 if "no such" in msg else 400, msg)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
FIT_MODES = ("contain", "cover", "stretch", "width", "height")
FIT_ALIASES = {"x": "width", "y": "height", "fit_x": "width", "fit_y": "height"}
ROTATIONS = (0, 90, 180, 270)
# Zoom is an integer percent, so URLs and ETags stay exact -- no float
# formatting to disagree about.
ZOOM_MIN, ZOOM_MAX = 25, 400


def _background(bg: str) -> tuple[int, int, int]:
    """Parse a colour, falling back to black rather than failing a render."""
    try:
        return ImageColor.getrgb(bg)
    except ValueError:
        return (0, 0, 0)


# How deep a strip to sample when matching a letterbox bar to the picture.
# A few pixels rather than one, so a single odd row cannot skew it; median
# rather than mean, so a thin bright edge or JPEG ringing cannot either.
_EDGE_SAMPLE_PX = 4


def _edge_colour(img: Image.Image, side: str) -> tuple[int, int, int]:
    w, h = img.size
    d = max(1, min(_EDGE_SAMPLE_PX, w if side in ("left", "right") else h))
    box = {
        "left": (0, 0, d, h),
        "right": (w - d, 0, w, h),
        "top": (0, 0, w, d),
        "bottom": (0, h - d, w, h),
    }[side]
    median = ImageStat.Stat(img.crop(box)).median
    return tuple(int(v) for v in median[:3])


def _centre_to_box(img: Image.Image, bw: int, bh: int, bg: str) -> Image.Image:
    """Pad and/or crop `img` about its centre until it is exactly bw x bh.

    bg="auto" fills each letterbox bar with the median colour of the picture
    edge it touches, so the bars read as part of the image instead of framing
    it. Because only one axis ever has slack after fitting, the two bars are
    independent -- a landscape with sky above and grass below gets a sky-
    coloured top bar and a grass-coloured bottom one.
    """
    if img.size == (bw, bh):
        return img

    auto = bg.lower() == "auto"
    canvas = Image.new("RGB", (bw, bh), (0, 0, 0) if auto else _background(bg))
    ox, oy = (bw - img.width) // 2, (bh - img.height) // 2

    if auto:
        if ox > 0:
            canvas.paste(_edge_colour(img, "left"), (0, 0, ox, bh))
            canvas.paste(_edge_colour(img, "right"), (ox + img.width, 0, bw, bh))
        if oy > 0:
            canvas.paste(_edge_colour(img, "top"), (0, 0, bw, oy))
            canvas.paste(_edge_colour(img, "bottom"), (0, oy + img.height, bw, bh))

    canvas.paste(img, (ox, oy))
    return canvas


def _fit_into(img: Image.Image, bw: int, bh: int, fit: str, bg: str) -> Image.Image:
    """Scale `img` to exactly bw x bh according to `fit`."""
    if fit == "stretch":
        return img.resize((bw, bh), Image.LANCZOS)
    if fit == "cover":
        # Scale to fill, then crop the overflow.
        return ImageOps.fit(img, (bw, bh), Image.LANCZOS, centering=(0.5, 0.5))
    if fit == "width":
        # Match the box width exactly; height then over- or underflows and is
        # cropped or padded about the centre.
        scale = bw / img.width
        img = img.resize((bw, max(1, round(img.height * scale))), Image.LANCZOS)
        if img.height > bh:
            top = (img.height - bh) // 2
            img = img.crop((0, top, bw, top + bh))
        return _centre_to_box(img, bw, bh, bg)
    if fit == "height":
        scale = bh / img.height
        img = img.resize((max(1, round(img.width * scale)), bh), Image.LANCZOS)
        if img.width > bw:
            left = (img.width - bw) // 2
            img = img.crop((left, 0, left + bw, bh))
        return _centre_to_box(img, bw, bh, bg)
    # contain: scale to fit entirely inside, letterbox the remainder
    img = ImageOps.contain(img, (bw, bh), Image.LANCZOS)
    return _centre_to_box(img, bw, bh, bg)


def _to_rgb565(img: Image.Image) -> bytes:
    """Pack RGB888 into big-endian RGB565.

    Big-endian because the device blits this straight to the panel with
    draw_pixels_at(), and both mipi_rgb and mipi_spi expect big-endian there.
    It is the same convention as `byte_order: BIG_ENDIAN` on online_image --
    see the long note in esphome/common/content-pull.yaml.

    This is the whole point of the SD path: the ESP has to do no decoding at
    all, just read bytes and hand them to the panel. Cost is size -- a frame
    is always width*height*2 bytes, 768 KB at 800x480, against ~33 KB as
    JPEG. That trade only makes sense because it removes a 1.8 s decode.
    """
    if HAVE_NUMPY:
        a = np.asarray(img, dtype=np.uint16)
        packed = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
        return packed.astype(">u2").tobytes()

    out = bytearray()
    for r, g, b in img.getdata():
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        out += bytes((v >> 8, v & 0xFF))
    return bytes(out)


def render_image(
    src: Image.Image,
    w: int,
    h: int,
    fmt: str,
    fit: str,
    quality: int,
    bg: str,
    rot: int = 0,
    zoom: int = 100,
    subsampling: int = 0,
) -> tuple[bytes, str]:
    """Fit, rotate and encode an already-open image. Shared by the still and
    the frame paths so a video frame is framed exactly like a photo."""
    img = ImageOps.exif_transpose(src).convert("RGB")

    box = (h, w) if rot in (90, 270) else (w, h)
    if zoom != 100:
        zw = max(1, round(box[0] * zoom / 100))
        zh = max(1, round(box[1] * zoom / 100))
        img = _fit_into(img, zw, zh, fit, bg)
        img = _centre_to_box(img, box[0], box[1], bg)
    else:
        img = _fit_into(img, box[0], box[1], fit, bg)

    if rot == 90:
        img = img.transpose(Image.ROTATE_270)   # PIL rotates CCW
    elif rot == 180:
        img = img.transpose(Image.ROTATE_180)
    elif rot == 270:
        img = img.transpose(Image.ROTATE_90)

    if fmt == "rgb565":
        return _to_rgb565(img), "application/octet-stream"

    out = io.BytesIO()
    if fmt == "png":
        img.save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    if fmt == "qoi":
        if not HAVE_QOI:
            abort(503, "QOI support not installed (pip install qoi numpy)")
        return qoi_lib.encode(np.asarray(img)), "image/qoi"
    # baseline JPEG -- progressive is NOT decodable by ESPHome's decoder.
    #
    # subsampling: 0 is 4:4:4, which a still wants -- it keeps colour edges
    # (text, line art) crisp, and a still is fetched once. Clip frames pass 2
    # (4:2:0) instead: chroma detail is what nobody sees at 15 fps, and bytes
    # per frame are exactly what limits how long a clip the screen can hold.
    img.save(
        out, "JPEG", quality=quality, optimize=True, progressive=False,
        subsampling=subsampling,
    )
    return out.getvalue(), "image/jpeg"


def render(
    body: bytes,
    w: int,
    h: int,
    fmt: str,
    fit: str,
    quality: int,
    bg: str,
    rot: int = 0,
    zoom: int = 100,
) -> tuple[bytes, str]:
    """Render stored bytes to exactly w x h for a device.

    `rot` is degrees CLOCKWISE, for a panel mounted turned. It is applied
    *after* fitting, and for 90/270 the source is fitted into a swapped box
    first, so the result is always exactly w x h and the content reads upright
    once the screen is physically rotated. Doing this here rather than with
    ESPHome's display `rotation:` keeps the device on its zero-copy draw path.
    """
    with Image.open(io.BytesIO(body)) as src:
        return render_image(src, w, h, fmt, fit, quality, bg, rot, zoom)


def cached_render(sha: str, body: bytes, params: tuple) -> tuple[bytes, str]:
    """Render once per (image, parameters); several screens share the result."""
    key = (sha, params)
    hit = _render_cache.get(key)
    if hit is not None:
        _render_cache.move_to_end(key)
        return hit

    body, ctype = render(body, *params)
    _render_cache[key] = (body, ctype)
    while len(_render_cache) > RENDER_CACHE_SIZE:
        _render_cache.popitem(last=False)
    return body, ctype


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------
@app.get("/d/<device>/image")
def get_image(device: str):
    meta = read_meta(device)
    if not meta:
        abort(404, "no content for this device yet")

    # `prefs=0` asks for exactly the query parameters given, ignoring stored
    # overrides. The device page uses it to preview a selection before it is
    # applied -- without it, stored prefs would mask whatever you picked.
    use_prefs = request.args.get("prefs", "1") not in ("0", "false", "no")
    prefs = read_prefs(device) if use_prefs else {}

    def arg(name: str, default):
        """Preference wins over query string; see read_prefs()."""
        if name in prefs:
            return prefs[name]
        return request.args.get(name, default)

    try:
        w = int(request.args.get("w", 800))
        h = int(request.args.get("h", 480))
        quality = int(arg("q", DEFAULT_QUALITY))
        rot = int(arg("rot", 0))
        zoom = int(arg("zoom", 100))
    except (TypeError, ValueError):
        abort(400, "w, h, q, rot and zoom must be integers")
    if not (0 < w <= 4096 and 0 < h <= 4096):
        abort(400, "w/h out of range")
    quality = max(1, min(100, quality))
    rot %= 360
    if rot not in ROTATIONS:
        abort(400, "rot must be one of 0, 90, 180, 270")
    if not ZOOM_MIN <= zoom <= ZOOM_MAX:
        abort(400, f"zoom must be {ZOOM_MIN}-{ZOOM_MAX} (percent)")

    fmt = str(arg("fmt", "jpeg")).lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ("jpeg", "png", "qoi", "rgb565"):
        abort(400, "fmt must be jpeg, png, qoi or rgb565")

    fit = str(arg("fit", "contain")).lower()
    fit = FIT_ALIASES.get(fit, fit)
    if fit not in FIT_MODES:
        abort(400, "fit must be one of " + ", ".join(FIT_MODES) + " (x/y alias width/height)")

    bg = str(arg("bg", "black"))
    params = (w, h, fmt, fit, quality, bg, rot, zoom)

    etag = '"%s"' % hashlib.sha256(
        (meta["sha256"] + repr(params)).encode("utf-8")
    ).hexdigest()[:32]

    # The whole point of the polling design: unchanged content costs this.
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

    body, ctype = cached_render(
        meta["sha256"], lib(library.body_of, meta["id"]), params
    )
    return Response(
        body,
        content_type=ctype,
        headers={
            "ETag": etag,
            "Cache-Control": "no-cache",
            "Content-Length": str(len(body)),
        },
    )


@app.post("/d/<device>/state")
def post_state(device: str):
    """A screen reporting what it holds. Sent by the screen on change."""
    device_dir(device)  # validates the name
    if request.content_length and request.content_length > 64 * 1024:
        abort(413, "state report too large")
    report = request.get_json(silent=True)
    if not isinstance(report, dict):
        abort(400, "expected a JSON object")
    lib(library.set_screen_state, device,
        {"report": report, "at": time.time(), "addr": request.remote_addr})
    return Response(status=204)


@app.get("/d/<device>/state")
def get_state(device: str):
    device_dir(device)
    state = lib(library.screen_state, device) or {}
    return jsonify({**state, "entries": cache_entries(state)})


@app.get("/d/<device>/video")
def video_info(device: str):
    """What this screen would play, and how many frames it has."""
    meta = read_meta(device)
    body = lib(library.body_of, meta["id"]) if meta else None
    info = frames.source_info(body)
    info["current"] = meta.get("filename") if meta else None
    return jsonify(info)


def _framing(device: str) -> tuple[str, int, int, str]:
    """(fit, rot, zoom, bg) from the screen's own preferences.

    Clips follow them so video is cropped and rotated the same way the screen's
    stills are. Bad stored values fall back rather than failing a clip.
    """
    prefs = read_prefs(device)
    fit = str(prefs.get("fit", "cover")).lower()
    fit = FIT_ALIASES.get(fit, fit)
    if fit not in FIT_MODES:
        fit = "cover"
    rot = int(prefs.get("rot", 0)) % 360
    if rot not in ROTATIONS:
        rot = 0
    zoom = max(ZOOM_MIN, min(ZOOM_MAX, int(prefs.get("zoom", 100))))
    bg = str(prefs.get("bg", "black"))
    return fit, rot, zoom, bg


def _quality(device: str, fallback) -> int:
    """JPEG quality for this screen: its stored pref, else `fallback`.

    Same precedence as everywhere else -- a stored pref beats the query
    string -- so the quality control on the device page reaches clips too, not
    only the `/image` path. Without this it changed the content token (which
    folds in every pref) and so forced a re-download of identical bytes.
    """
    prefs = read_prefs(device)
    try:
        quality = int(prefs["q"]) if "q" in prefs else int(fallback)
    except (TypeError, ValueError):
        quality = DEFAULT_QUALITY
    return max(1, min(100, quality))


# TTMJ: the clip container the screens load into PSRAM. Little-endian:
#   header   "TTMJ" u16 version, u16 w, u16 h, u16 fps, u32 count
#   record   u32 len, then len bytes of baseline JPEG      (count times)
# No per-frame delays: the clip is resampled to `fps` here, so the device
# plays at one fixed rate and "how long fits" is simply bytes / fps.
CLIP_MAGIC = b"TTMJ"
CLIP_VERSION = 1
CLIP_HEADER = struct.Struct("<4sHHHHI")
CLIP_RECORD = struct.Struct("<I")
CLIP_QUALITY = 80
CLIP_MAX_BYTES = 64 * 1024 * 1024


class ClipTooLarge(Exception):
    """Even the first frame does not fit the device's byte budget."""


def build_clip(body: bytes | None, w: int, h: int, fps: int, max_bytes: int,
               framing: tuple, still: bool = False) -> tuple[bytes, int]:
    """One cycle of the clip at `fps`, as TTMJ, truncated to `max_bytes`.

    Frames are encoded once per distinct SOURCE frame: resampling a slow GIF
    to a high rate repeats frames, and repeats reuse the same JPEG bytes.
    """
    fit, quality, bg, rot, zoom = framing
    # A clip is many frames in a fixed byte budget, so its frames are encoded
    # 4:2:0. A one-frame item is a still and keeps 4:4:4.
    subsampling = 0 if still else 2
    if still:
        # A still is a one-frame clip, so a screen can hold stills and clips in
        # the same cache and show both through one decoder.
        jpeg, _ = render(body, w, h, "jpeg", fit, quality, bg, rot, zoom)
        if CLIP_HEADER.size + CLIP_RECORD.size + len(jpeg) > max_bytes:
            raise ClipTooLarge
        return (CLIP_HEADER.pack(CLIP_MAGIC, CLIP_VERSION, w, h, fps, 1)
                + CLIP_RECORD.pack(len(jpeg)) + jpeg), 1
    indices = frames.resample(frames.durations(body), fps)
    encoded: dict[int, bytes] = {}
    for n, img in frames.iter_frames(body, indices, w, h):
        encoded[n], _ = render_image(img, w, h, "jpeg", fit, quality, bg, rot, zoom,
                                     subsampling)

    parts: list[bytes] = []
    used = CLIP_HEADER.size
    for n in indices:
        jpeg = encoded[n]
        cost = CLIP_RECORD.size + len(jpeg)
        if used + cost > max_bytes:
            break
        parts.append(CLIP_RECORD.pack(len(jpeg)))
        parts.append(jpeg)
        used += cost
    count = len(parts) // 2
    if count == 0:
        raise ClipTooLarge
    header = CLIP_HEADER.pack(CLIP_MAGIC, CLIP_VERSION, w, h, fps, count)
    return header + b"".join(parts), count


@app.get("/d/<device>/clip.mjpeg")
def get_clip(device: str):
    """The screen's whole clip in one download, for playback from memory.

    This replaces fetching /frame n, n+1, ... one request at a time: the device
    stores this file (on its SD card if it has one) and decodes each JPEG
    straight to the panel, so neither the network nor a decode buffer sits
    between frames.
    """
    try:
        w = int(request.args.get("w", 240))
        h = int(request.args.get("h", 240))
        fps = int(request.args.get("fps", 15))
        max_bytes = int(request.args.get("max", 4_000_000))
    except (TypeError, ValueError):
        abort(400, "w, h, q, fps and max must be integers")
    if not (0 < w <= 4096 and 0 < h <= 4096):
        abort(400, "w/h out of range")
    if not 1 <= fps <= 60:
        abort(400, "fps must be 1-60")
    if not CLIP_HEADER.size < max_bytes <= CLIP_MAX_BYTES:
        abort(400, f"max must be up to {CLIP_MAX_BYTES} bytes")
    # The screen sends the quality it wants; a stored pref overrides it.
    quality = _quality(device, request.args.get("q", CLIP_QUALITY))

    fit, rot, zoom, bg = _framing(device)
    meta = read_meta(device)
    body = lib(library.body_of, meta["id"]) if meta else None
    sha = meta["sha256"] if meta else "synthetic"
    # A still -- or a clip in a format this screen did not advertise -- is one
    # frame. Only a screen with no content at all gets the synthetic test clip.
    screens = registry.for_device(device)
    still = bool(meta) and (
        not meta.get("animated", frames.is_animated(body))
        or (bool(screens) and plays_as_still(screens[0], meta))
    )
    params = (w, h, fps, max_bytes, fit, quality, bg, rot, zoom, still)

    etag = '"%s"' % hashlib.sha256(
        (sha + "clip" + repr(params)).encode("utf-8")
    ).hexdigest()[:32]
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

    key = (sha, params)
    hit = _clip_cache.get(key)
    if hit is None:
        try:
            hit = build_clip(body, w, h, fps, max_bytes, (fit, quality, bg, rot, zoom), still)
        except ClipTooLarge:
            abort(413, "the first frame alone exceeds max")
        _clip_cache[key] = hit
        while len(_clip_cache) > CLIP_CACHE_SIZE:
            _clip_cache.popitem(last=False)
    else:
        _clip_cache.move_to_end(key)
    out, count = hit
    return Response(
        out,
        content_type="application/octet-stream",
        headers={
            "ETag": etag,
            "Cache-Control": "no-cache",
            "Content-Length": str(len(out)),
            "X-Frame-Count": str(count),
        },
    )


@app.get("/d/<device>/frame")
def get_frame(device: str):
    """One frame of the screen's clip, framed exactly like a still would be.

    The device asks for n, n+1, n+2... This costs an HTTP round trip per
    frame, which a real streaming component would not -- but it needs no new
    firmware component, so it measures the decode-and-draw cost honestly
    before committing to writing one.
    """
    try:
        n = int(request.args.get("n", 0))
        w = int(request.args.get("w", 240))
        h = int(request.args.get("h", 240))
    except (TypeError, ValueError):
        abort(400, "n, w and h must be integers")
    if not (0 < w <= 4096 and 0 < h <= 4096):
        abort(400, "w/h out of range")
    quality = _quality(device, request.args.get("q", DEFAULT_QUALITY))

    fmt = request.args.get("fmt", "jpeg").lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ("jpeg", "png", "qoi", "rgb565"):
        abort(400, "fmt must be jpeg, png, qoi or rgb565")

    fit, rot, zoom, bg = _framing(device)

    meta = read_meta(device)
    body = lib(library.body_of, meta["id"]) if meta else None
    total = frames.source_info(body)["frames"]
    n %= max(total, 1)

    img = frames.frame(body, n, w, h)
    out, ctype = render_image(img, w, h, fmt, fit, quality, bg, rot, zoom)
    return Response(
        out,
        content_type=ctype,
        headers={
            # Every frame is a different resource; never let anything cache
            # the sequence or the device will show a still.
            "Cache-Control": "no-store",
            "X-Frame-Index": str(n),
            "X-Frame-Count": str(total),
            "Content-Length": str(len(out)),
        },
    )


@app.post("/d/<device>/content")
def post_content(device: str):
    if "file" in request.files:
        upload = request.files["file"]
        body = upload.read()
        filename = upload.filename or "upload"
        ctype = upload.mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    else:
        body = request.get_data()
        filename = request.headers.get("X-Filename", "upload")
        ctype = request.content_type or "application/octet-stream"

    if not body:
        abort(400, "empty body -- POST a file as multipart 'file' or as the raw body")

    item = lib(library.pool_add, body, ctype, filename)
    lib(library.select, device, item["id"])
    meta = dict(item)
    # Push, rather than waiting for the next poll.
    meta["pushed_to"] = registry.notify(device)
    if from_browser_form():
        return Response(status=303, headers={"Location": f"/d/{device}/"})
    return jsonify(meta), 201


@app.delete("/d/<device>/content")
def delete_content(device: str):
    """Unassign everything from this screen. The pool keeps the images."""
    lib(library.clear, device)
    registry.notify(device)
    return "", 204


@app.get("/pool")
def get_pool():
    """Every uploaded image, newest first."""
    return jsonify(lib(library.pool))


@app.delete("/pool/<item_id>")
@app.post("/pool/<item_id>/delete")
def delete_pool_item(item_id: str):
    """Delete for good, from the pool and from every screen using it."""
    result = lib(library.pool_remove, item_id)
    for device in result["affected"]:
        registry.notify(device)
    if from_browser_form():
        return Response(status=303, headers={"Location": request.referrer or "/"})
    return jsonify(result)


# Content types we are willing to hand back verbatim. Anything else gets
# re-encoded through the thumbnail path instead of being echoed to a browser.
RAW_TYPES = {
    "GIF": "image/gif",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "JPEG": "image/jpeg",
}


@app.get("/pool/<item_id>/raw")
def pool_raw(item_id: str):
    """The original bytes, so a browser can animate a GIF/APNG/WebP itself.

    Rendering an animation server-side would mean re-encoding a clip per
    thumbnail; handing over the original and letting the browser play it costs
    nothing. Content type comes from what Pillow actually decoded, not from
    the upload header, which can be wrong or missing.
    """
    item = lib(library.pool_get, item_id)
    if item is None:
        abort(404, "no such item")
    ctype = RAW_TYPES.get(str(item.get("source_format", "")).upper())
    if ctype is None:
        abort(415, "not a format we serve verbatim")
    return Response(
        lib(library.body_of, item_id),
        content_type=ctype,
        headers={"Cache-Control": "max-age=3600"},
    )


@app.get("/pool/<item_id>/thumb")
def pool_thumb(item_id: str):
    """Thumbnail of a pool image. Device-independent, so one cache entry
    serves every screen's list."""
    item = lib(library.pool_get, item_id)
    if item is None:
        abort(404, "no such item")
    body = lib(library.body_of, item_id)
    png, ctype = cached_render(
        item["sha256"], body, (160, 96, "png", "contain", 90, "auto", 0, 100)
    )
    return Response(png, content_type=ctype, headers={"Cache-Control": "max-age=3600"})


@app.get("/d/<device>/items")
def list_items(device: str):
    """This screen's two lists, plus which item is on it."""
    return jsonify(
        {
            "current": lib(library.used, device)["current"],
            "used": lib(library.used_items, device),
            "unused": lib(library.unused_items, device),
        }
    )


@app.post("/d/<device>/items/<item_id>/select")
def select_item(device: str, item_id: str):
    """Show it now (assigning it to this screen if it was not already)."""
    lib(library.select, device, item_id)
    pushed = registry.notify(device)
    if from_browser_form():
        return Response(
            status=303, headers={"Location": redirect_target(f"/d/{device}/")}
        )
    return jsonify({"current": item_id, "pushed_to": pushed})


@app.post("/d/<device>/items/<item_id>/add")
def add_item(device: str, item_id: str):
    """Start using a pool image on this screen, without switching to it."""
    lib(library.assign, device, item_id, False)
    if from_browser_form():
        return Response(
            status=303, headers={"Location": redirect_target(f"/d/{device}/")}
        )
    return jsonify(lib(library.used, device))


@app.delete("/d/<device>/items/<item_id>")
@app.post("/d/<device>/items/<item_id>/remove")
def remove_item(device: str, item_id: str):
    """Stop using it on this screen. It stays in the pool."""
    state = lib(library.unassign, device, item_id)
    pushed = registry.notify(device)
    if from_browser_form():
        return Response(status=303, headers={"Location": f"/d/{device}/"})
    return jsonify({**state, "pushed_to": pushed})


@app.post("/d/<device>/order")
def reorder_items(device: str):
    """Apply a drag-and-drop order. Body: {"ids": [...]} or form ids=a,b,c."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    if ids is None and (raw := request.form.get("ids")):
        ids = [i for i in raw.split(",") if i]
    if not isinstance(ids, list):
        abort(400, 'expected {"ids": [...]}')
    state = lib(library.reorder, device, [str(i) for i in ids])
    return jsonify({"order": state["used"]})


@app.post("/d/<device>/prefs")
def post_prefs(device: str):
    """Set render overrides, then push a refresh so the change is visible."""
    device_dir(device)  # validates the name
    prefs = read_prefs(device)
    form = request.form if request.form else (request.get_json(silent=True) or {})

    if (fit := form.get("fit")) is not None:
        fit = FIT_ALIASES.get(str(fit).lower(), str(fit).lower())
        if fit not in FIT_MODES:
            abort(400, "fit must be one of " + ", ".join(FIT_MODES))
        prefs["fit"] = fit

    if (rot := form.get("rot")) is not None:
        try:
            rot = int(rot) % 360
        except (TypeError, ValueError):
            abort(400, "rot must be an integer")
        if rot not in ROTATIONS:
            abort(400, "rot must be one of 0, 90, 180, 270")
        prefs["rot"] = rot

    if (q := form.get("q")) is not None:
        try:
            prefs["q"] = max(1, min(100, int(q)))
        except (TypeError, ValueError):
            abort(400, "q must be an integer")

    if (bg := form.get("bg")) is not None:
        prefs["bg"] = str(bg)

    # zoom_by is what the +/- buttons send: a relative step, so the browser
    # never has to know the current value.
    if (step := form.get("zoom_by")) is not None:
        try:
            prefs["zoom"] = int(prefs.get("zoom", 100)) + int(step)
        except (TypeError, ValueError):
            abort(400, "zoom_by must be an integer")
    elif (zoom := form.get("zoom")) is not None:
        try:
            prefs["zoom"] = int(zoom)
        except (TypeError, ValueError):
            abort(400, "zoom must be an integer")
    if "zoom" in prefs:
        prefs["zoom"] = max(ZOOM_MIN, min(ZOOM_MAX, prefs["zoom"]))

    # "reset" clears everything and falls back to what the device asked for.
    if form.get("reset"):
        prefs = {}

    write_prefs(device, prefs)
    pushed = registry.notify(device)

    if from_browser_form():
        return Response(status=303, headers={"Location": f"/d/{device}/"})
    return jsonify({"prefs": prefs, "pushed_to": pushed})


@app.get("/d/<device>/meta")
def get_meta(device: str):
    meta = read_meta(device)
    if not meta:
        abort(404, "no content for this device yet")
    return jsonify(meta)


LIBRARY_JS = """
<script>
(function () {
  var list = document.getElementById("library");
  if (!list) return;
  var dragging = null;

  list.addEventListener("dragstart", function (e) {
    dragging = e.target.closest("li");
    if (dragging) dragging.classList.add("dragging");
  });

  list.addEventListener("dragend", function () {
    if (dragging) dragging.classList.remove("dragging");
    dragging = null;
    persist();
  });

  list.addEventListener("dragover", function (e) {
    e.preventDefault();
    var over = e.target.closest("li");
    if (!over || !dragging || over === dragging) return;
    var rows = Array.prototype.slice.call(list.children);
    // Insert before or after depending on which half of the row we are over.
    var box = over.getBoundingClientRect();
    var after = (e.clientY - box.top) > box.height / 2;
    list.insertBefore(dragging, after ? over.nextSibling : over);
  });

  function persist() {
    var ids = Array.prototype.map.call(list.children, function (li) {
      return li.dataset.id;
    });
    fetch(list.dataset.orderUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: ids })
    }).then(function (r) {
      var note = document.getElementById("ordernote");
      if (note) note.textContent = r.ok ? "Order saved." : "Could not save order.";
    });
  }
})();
</script>
"""


INDEX_JS = """
<script>
(function () {
  var grid = document.querySelector(".grid");
  if (!grid) return;

  // Switch in place: no navigation, no reload. Posting JSON (rather than a
  // form body) is what makes the server answer with JSON instead of a 303.
  grid.addEventListener("submit", function (e) {
    var form = e.target.closest("form.pick");
    if (!form) return;
    e.preventDefault();

    var card = form.closest(".card");
    var busy = form.classList.contains("busy");
    if (busy) return;
    form.classList.add("busy");

    fetch(form.action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.json();
      })
      .then(function () {
        form.classList.remove("busy");

        var picks = card.querySelectorAll("form.pick");
        for (var i = 0; i < picks.length; i++) picks[i].classList.remove("now");
        form.classList.add("now");

        var caption = card.querySelector(".caption");
        if (caption && form.dataset.name) caption.textContent = form.dataset.name;

        var img = card.querySelector("img.preview");
        if (img) {
          // Cache-bust: the URL is unchanged, only what it renders to.
          img.src = img.dataset.base + "&t=" + Date.now();
        } else {
          // The card had nothing on screen and so has no preview element yet.
          location.reload();
        }
      })
      .catch(function () {
        form.classList.remove("busy");
        form.submit();   // last resort: let the browser do the normal post
      });
  });
})();
</script>
"""


PREVIEW_JS = """
<script>
(function () {
  var form = document.getElementById("framing");
  var img = document.getElementById("preview");
  if (!form || !img) return;
  var base = img.dataset.base;          // /d/<device>/image?w=..&h=..&fmt=png
  var status = document.getElementById("pstatus");

  function refresh() {
    var p = new URLSearchParams(new FormData(form));
    p.delete("reset");
    p.set("prefs", "0");                // preview the selection, not the stored prefs
    p.set("t", Date.now());             // defeat the browser cache
    img.src = base + "&" + p.toString();
    if (status) status.textContent = "preview — not yet on the screen";
  }

  form.addEventListener("change", refresh);
})();
</script>
"""


@app.get("/d/<device>/")
def device_page(device: str):
    device_dir(device)  # validates the name
    meta = read_meta(device)
    prefs = read_prefs(device)

    # Preview at the real panel aspect ratio so what you see matches the
    # screen. Prefs override query params, so the preview shows the same fit
    # and rotation the device will get.
    screens = registry.for_device(device)
    screen = screens[0] if screens else None
    pw, ph = (screen.width, screen.height) if screen else (800, 480)
    shape = "border-radius:50%;" if screen and screen.round else ""
    scale = min(1.0, 520 / max(pw, 1))
    vw, vh = max(1, round(pw * scale)), max(1, round(ph * scale))

    if meta:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta["uploaded_at"]))
        current = (
            f'<p>Source: <b>{meta["filename"]}</b> &mdash; '
            f'{meta["source_format"]} {meta["source_size"][0]}x{meta["source_size"][1]}, '
            f'{meta["bytes"] // 1024} kB, updated {ts}</p>'
            f'<p><img id="preview" '
            f'data-base="/d/{device}/image?w={vw}&h={vh}&fmt=png" '
            f'src="/d/{device}/image?w={vw}&h={vh}&fmt=png&t={int(meta["uploaded_at"])}" '
            f'width="{vw}" height="{vh}" '
            f'style="max-width:100%;border:1px solid #ccc;background:#eee;{shape}"></p>'
            f'<p style="color:#666" id="pstatus">Showing what is on the screen '
            f'now, at the panel aspect ratio {pw}x{ph}.</p>'
        )
    else:
        current = "<p><i>No content yet.</i></p>"

    cache_rows = cache_entries(lib(library.screen_state, device))
    used_state = lib(library.used, device)
    used = lib(library.used_items, device)
    unused = lib(library.unused_items, device)

    def row(it, n=None, draggable=False):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(it["uploaded_at"]))
        is_current = it["id"] == used_state["current"]
        klass = "current" if is_current else ""
        num = f"{n}. " if n else ""
        grip = '<span class="grip" title="Drag to reorder">&#9776;</span>' if draggable else ""
        if draggable:
            acts = (
                f'<form method="post" action="/d/{device}/items/{it["id"]}/select">'
                f'<button type="submit"{" disabled" if is_current else ""}>Show</button></form>'
                f'<form method="post" action="/d/{device}/items/{it["id"]}/remove">'
                f'<button type="submit" title="Stop using on this screen">&minus;</button></form>'
            )
        else:
            acts = (
                f'<form method="post" action="/d/{device}/items/{it["id"]}/add">'
                f'<button type="submit" title="Use on this screen">+</button></form>'
                f'<form method="post" action="/d/{device}/items/{it["id"]}/select">'
                f'<button type="submit">Show</button></form>'
                f'<form method="post" action="/pool/{it["id"]}/delete">'
                f'<button type="submit" title="Delete from the pool entirely">&times;</button></form>'
            )
        attrs = ' draggable="true"' if draggable else ""
        if klass:
            attrs += f' class="{klass}"'
        return (
            f'<li{attrs} data-id="{it["id"]}">'
            f'{grip}'
            f'<img src="{thumb_src(it)}" width="80" height="48" alt="" loading="lazy">'
            f'<span class="who"><b>{num}{it["filename"]}</b> {motion_badge(it)} '
            f'{still_badge(screen, it)} {cache_badge(cache_rows, it["id"])}<br>'
            f'<small>{it["source_size"][0]}x{it["source_size"][1]}, '
            f'{it["bytes"] // 1024} kB, {when}'
            f'{" &mdash; <b>on screen</b>" if is_current else ""}</small></span>'
            f'<span class="acts">{acts}</span></li>'
        )

    if used:
        used_html = (
            f'<ol id="library" data-order-url="/d/{device}/order">'
            + "".join(row(it, n, True) for n, it in enumerate(used, 1))
            + "</ol>"
            '<p style="color:#666" id="ordernote">Drag to reorder &mdash; saved '
            "as you drop, and the order a slideshow will follow.</p>"
        )
    else:
        used_html = (
            "<p><i>This screen is not using anything yet. Upload below, or "
            "add from the pool.</i></p>"
        )

    unused_html = (
        '<ul id="unused">' + "".join(row(it) for it in unused) + "</ul>"
        if unused
        else "<p><i>Nothing else in the pool.</i></p>"
    )

    def sel(name: str, value, options) -> str:
        current_value = prefs.get(name, value)
        opts = "".join(
            f'<option value="{v}"{" selected" if str(v) == str(current_value) else ""}>{label}</option>'
            for v, label in options
        )
        return f'<select name="{name}">{opts}</select>'

    fit_sel = sel("fit", "contain", [
        ("contain", "contain — fit all, letterbox"),
        ("cover", "cover — fill, crop overflow"),
        ("width", "width (x) — match width"),
        ("height", "height (y) — match height"),
        ("stretch", "stretch — distort to fill"),
    ])
    rot_sel = sel("rot", 0, [(0, "0°"), (90, "90° CW"), (180, "180°"), (270, "270° CW")])
    # Lower values are there for clips: quality sets bytes per frame, and a
    # screen caches a fixed number of BYTES, so dropping it is what buys a
    # longer clip (and a faster download).
    q_sel = sel("q", DEFAULT_QUALITY, [
        (35, "35 — longest clips"),
        (50, "50"),
        (65, "65"),
        (75, "75"),
        (85, "85"),
        (95, "95 (default)"),
        (100, "100"),
    ])
    bg_sel = sel("bg", "black", [
        ("auto", "auto — match the picture edge"),
        ("black", "black"),
        ("white", "white"),
        ("#808080", "grey"),
    ])
    zoom_now = int(prefs.get("zoom", 100))
    # Separate little forms rather than part of the framing form: these are
    # relative steps applied server-side, so the page never has to know or
    # round-trip the current value.
    zoom_html = (
        f'<form method="post" action="/d/{device}/prefs">'
        f'<button type="submit" name="zoom_by" value="-10" title="Zoom out"'
        f'{" disabled" if zoom_now <= ZOOM_MIN else ""}>&minus;</button></form> '
        f"<b>{zoom_now}%</b> "
        f'<form method="post" action="/d/{device}/prefs">'
        f'<button type="submit" name="zoom_by" value="10" title="Zoom in"'
        f'{" disabled" if zoom_now >= ZOOM_MAX else ""}>+</button></form> '
        f'<form method="post" action="/d/{device}/prefs">'
        f'<button type="submit" name="zoom" value="100"'
        f'{" disabled" if zoom_now == 100 else ""}>Reset zoom</button></form>'
    )

    override = (
        f'<p style="color:#666">Overriding: <code>{prefs}</code></p>' if prefs else
        '<p style="color:#666">No overrides — using what the device asked for.</p>'
    )

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{device} — mini screen</title>
<style>
  body {{ font: 16px system-ui, sans-serif; margin: 0 auto; padding: 1.5rem; max-width: 40rem; }}
  input[type=file] {{ display: block; margin: 1rem 0; }}
  button {{ font-size: 1rem; padding: .6rem 1.2rem; }}
  label {{ display: inline-block; margin: 0 1rem .6rem 0; }}
  select {{ font-size: 1rem; padding: .3rem; }}
  fieldset {{ border: 1px solid #ddd; margin: 1.5rem 0; padding: 1rem; }}
  ol#library {{ list-style: none; margin: 0; padding: 0; }}
  ul#unused {{ list-style: none; margin: 0; padding: 0; }}
  ul#unused li {{ display: flex; align-items: center; gap: .6rem; padding: .4rem;
                  border: 1px solid #eee; border-radius: 4px; margin-bottom: .4rem;
                  background: #fafafa; }}
  ul#unused img {{ border: 1px solid #ddd; flex: none; }}
  ol#library li {{ display: flex; align-items: center; gap: .6rem; padding: .4rem;
                   border: 1px solid #eee; border-radius: 4px; margin-bottom: .4rem;
                   background: #fff; cursor: grab; }}
  ol#library li.current {{ border-color: #1a7f37; background: #f2fbf4; }}
  ol#library li.dragging {{ opacity: .4; }}
  ol#library img {{ border: 1px solid #ddd; flex: none; }}
  .grip {{ color: #999; flex: none; }}
  .who {{ flex: 1; min-width: 0; overflow: hidden; }}
  .acts {{ display: flex; gap: .3rem; flex: none; }}
  .acts button {{ font-size: .85rem; padding: .3rem .6rem; }}
  .anim {{ font-size: .75rem; color: #0a5; border: 1px solid #0a5;
           border-radius: 3px; padding: 0 .25rem; white-space: nowrap; }}
  .still {{ font-size: .75rem; color: #a60; border: 1px solid #a60;
            border-radius: 3px; padding: 0 .25rem; white-space: nowrap; }}
  .caps {{ color: #666; }}
  .cached {{ font-size: .75rem; color: #2458b3; border: 1px solid #2458b3;
             border-radius: 3px; padding: 0 .25rem; white-space: nowrap; }}
  ul.cache {{ margin: .3rem 0; padding-left: 1.2rem; font-size: .9rem; }}
  .sdon {{ color: #1a7f37; }}
  .sdwarn {{ color: #a60; }}
  meter {{ width: 4rem; height: .7rem; vertical-align: middle; }}
</style>
<h1>{device}</h1>
<p>{caps_html(screen) if screen else '<small class="caps">offline &mdash; capabilities unknown</small>'}</p>
<div>{memory_html(device, detailed=True)}</div>
{current}

<fieldset>
  <legend>Send an image</legend>
  <form method="post" action="/d/{device}/content" enctype="multipart/form-data">
    <input type="file" name="file" accept="image/*" required>
    <button type="submit">Upload</button>
  </form>
  <p style="color:#666">Pushed to the screen immediately. On a phone the file
  picker offers the camera.</p>
</fieldset>

<fieldset>
  <legend>Currently used ({len(used)})</legend>
  {used_html}
</fieldset>

<fieldset>
  <legend>Not used &mdash; in the pool ({len(unused)})</legend>
  {unused_html}
  <p style="color:#666">The pool is shared by every screen, and each image is
  stored once. <b>+</b> starts using it here, <b>Show</b> uses it and puts it
  on now, <b>&times;</b> deletes it from the pool for every screen.</p>
</fieldset>

<fieldset>
  <legend>Framing</legend>
  <form method="post" action="/d/{device}/prefs" id="framing">
    <label>Fit {fit_sel}</label>
    <label>Rotate {rot_sel}</label>
    <label>Quality {q_sel}</label>
    <label>Letterbox {bg_sel}</label>
    <div style="margin-top:.8rem">
      <button type="submit">Apply to screen</button>
      <button type="submit" name="reset" value="1">Reset</button>
    </div>
  </form>
  <p style="margin-top:1rem">Zoom {zoom_html}</p>
  {override}
  <p style="color:#666">Zoom crops in above 100% and letterboxes below it, on
  top of whatever fit mode is selected. Rotate is for a panel mounted turned — it is applied
  after fitting, so content stays upright and fills the screen. Doing it here
  keeps the device on its fast draw path. Letterbox only shows with
  <code>contain</code> (or <code>width</code>/<code>height</code> when the
  picture underflows); <b>auto</b> fills each bar with the median colour of
  the picture edge it touches. Any CSS colour name or <code>#rrggbb</code>
  also works via the <code>bg</code> URL parameter.</p>
</fieldset>

<p><a href="/">All devices</a></p>
{PREVIEW_JS}
{LIBRARY_JS}
"""


@app.get("/")
def index():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # A screen is worth a card if it is on the network OR we hold state for
    # it. Live ones first: those are the ones you can actually send to.
    online = {sc.name: sc for sc in registry.all()}
    known = set(lib(library.devices))

    cards = []
    for name in sorted(online.keys() | known):
        sc = online.get(name)
        pw, ph = (sc.width, sc.height) if sc else (800, 480)
        # Preview at the panel's aspect ratio, and through the same prefs the
        # screen gets, so the card shows what is actually on it.
        tw = 260
        th = max(1, round(tw * ph / max(pw, 1)))
        state = lib(library.used, name)
        items = lib(library.used_items, name) or lib(library.pool)
        cur = next((i for i in items if i["id"] == state["current"]), None)

        # A round panel shows only the inscribed circle, so preview exactly that.
        shape = "border-radius:50%;" if sc and sc.round else ""
        if state["current"]:
            preview = (
                f'<img class="preview" data-base="/d/{name}/image?w={tw}&h={th}&fmt=png"'
                f' src="/d/{name}/image?w={tw}&h={th}&fmt=png"'
                f' width="{tw}" height="{th}" alt=""'
                f' style="border:1px solid #ccc;background:#eee;{shape}">'
            )
        else:
            preview = (
                f'<a href="/d/{name}/" class="empty" style="width:{tw}px;height:{th}px;{shape}">'
                "nothing on screen</a>"
            )

        if sc:
            status = (
                f'<span class="on">&#9679; online</span> '
                f"<small>{sc.host}</small><br>{caps_html(sc)}"
            )
            if sc.last_error:
                status += f' <small class="err">({sc.last_error})</small>'
        else:
            status = '<span class="off">&#9675; offline</span>'
        # Last report, even while offline: it is what the screen held then.
        if mem := memory_html(name):
            status += f"<br>{mem}"

        # "Dropdown with preview": a details/summary holding a strip of
        # thumbnails. A <select> cannot show pictures, and picking an image by
        # filename is exactly the thing you cannot do from memory.
        if items:
            choices = "".join(
                f'<form method="post" action="/d/{name}/items/{it["id"]}/select" '
                f'class="pick{" now" if it["id"] == state["current"] else ""}" '
                f'data-name="{it["filename"]}">'
                '<input type="hidden" name="return_to" value="/">'
                f'<button type="submit" title="{it["filename"]}">'
                f'<img src="{thumb_src(it)}" width="72" height="43" alt="" '
                f'loading="lazy"></button></form>'
                for it in items
            )
            switcher = (
                f"<details><summary>Switch picture "
                f"({len(items)})</summary><div class=\"strip\">{choices}</div></details>"
            )
        else:
            switcher = '<p><small>Nothing in the pool yet.</small></p>'

        cards.append(
            f'<div class="card"><h2><a href="/d/{name}/">{name}</a></h2>'
            f"<p>{status}</p>{preview}"
            f'<p><small class="caption">{cur["filename"] if cur else "&mdash;"}</small> '
            f'{motion_badge(cur) if cur else ""} {still_badge(sc, cur)}</p>'
            f"{switcher}</div>"
        )

    grid = (
        f'<div class="grid">{"".join(cards)}</div>'
        if cards
        else "<p><i>Nothing found yet. Screens appear here automatically once "
             "they are on the network.</i></p>"
    )

    scenes = lib(library.scenes)
    scene_rows = "".join(
        f'<li><b>{sc["name"]}</b> '
        f'<small>{len(sc.get("entries") or {})} screen(s), '
        f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(sc.get("saved_at", 0)))}</small>'
        f'<span class="acts">'
        f'<form method="post" action="/scenes/{sc["id"]}/apply">'
        f'<button type="submit">Apply</button></form>'
        f'<form method="post" action="/scenes/{sc["id"]}/delete">'
        f'<button type="submit" title="Delete scene">&times;</button></form>'
        f"</span></li>"
        for sc in reversed(scenes)
    )
    scenes_html = (
        f'<ul id="scenes">{scene_rows}</ul>' if scene_rows
        else "<p><i>No scenes saved yet.</i></p>"
    )

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mini screens</title>
<style>
  body {{ font: 16px system-ui, sans-serif; margin: 0 auto; padding: 1.5rem; max-width: 64rem; }}
  h1 {{ margin-top: 0; }}
  h2 {{ font-size: 1.1rem; margin: 0 0 .3rem; }}
  a {{ color: inherit; }}
  .grid {{ display: grid; gap: 1rem;
           grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); }}
  .card {{ border: 1px solid #ddd; border-radius: 6px; padding: .9rem; }}
  .card p {{ margin: .3rem 0; }}
  .on {{ color: #1a7f37; }}
  .off {{ color: #999; }}
  .err {{ color: #b00; }}
  .empty {{ display: flex; align-items: center; justify-content: center;
            border: 1px dashed #ccc; color: #999; text-decoration: none; }}
  details summary {{ cursor: pointer; margin-top: .5rem; font-size: .9rem; }}
  .strip {{ display: flex; flex-wrap: wrap; gap: .3rem; margin-top: .5rem; }}
  .pick button {{ padding: 0; border: 2px solid transparent; background: none;
                  cursor: pointer; line-height: 0; }}
  .pick.now button {{ border-color: #1a7f37; }}
  .pick.busy button {{ opacity: .4; }}
  .anim {{ font-size: .75rem; color: #0a5; border: 1px solid #0a5;
           border-radius: 3px; padding: 0 .25rem; white-space: nowrap; }}
  .still {{ font-size: .75rem; color: #a60; border: 1px solid #a60;
            border-radius: 3px; padding: 0 .25rem; white-space: nowrap; }}
  .caps {{ color: #666; }}
  .cached {{ font-size: .75rem; color: #2458b3; border: 1px solid #2458b3;
             border-radius: 3px; padding: 0 .25rem; white-space: nowrap; }}
  ul.cache {{ margin: .3rem 0; padding-left: 1.2rem; font-size: .9rem; }}
  .sdon {{ color: #1a7f37; }}
  .sdwarn {{ color: #a60; }}
  meter {{ width: 4rem; height: .7rem; vertical-align: middle; }}
  form {{ display: inline; }}
  fieldset {{ border: 1px solid #ddd; margin: 1.5rem 0; padding: 1rem; }}
  ul#scenes {{ list-style: none; margin: 0; padding: 0; }}
  ul#scenes li {{ display: flex; align-items: center; gap: .6rem; padding: .4rem;
                  border: 1px solid #eee; border-radius: 4px; margin-bottom: .4rem; }}
  ul#scenes li b {{ flex: none; }}
  ul#scenes li small {{ flex: 1; color: #666; }}
  .acts {{ display: flex; gap: .3rem; }}
  button {{ font-size: .9rem; padding: .35rem .7rem; }}
</style>
<h1>Mini screens</h1>
{grid}

<fieldset>
  <legend>Scenes</legend>
  {scenes_html}
  <form method="post" action="/scenes" style="margin-top:.8rem">
    <input name="name" placeholder="Scene name" maxlength="60" required
           style="font-size:1rem;padding:.35rem">
    <button type="submit">Save current state</button>
  </form>
  <p style="color:#666">A scene records which picture each screen is showing
  and its framing (fit, rotation, zoom, letterbox, quality). Applying one puts
  every screen back and pushes them all. Screens whose picture has since left
  the pool are skipped rather than failing the whole scene.</p>
</fieldset>

<p style="color:#666">Screens are discovered over mDNS and told where to fetch
from &mdash; no addresses configured anywhere.
<a href="/devices">/devices</a> shows the raw registry,
<a href="/pool">/pool</a> every image.</p>
{INDEX_JS}
"""


@app.get("/devices")
def devices():
    """Everything discovered on the network, and what it was told to fetch."""
    return jsonify([s.as_dict() for s in registry.all()])


@app.get("/scenes")
def list_scenes():
    return jsonify(lib(library.scenes))


@app.post("/scenes")
def save_scene():
    """Snapshot what every screen is showing, and how, under a name."""
    form = request.form if request.form else (request.get_json(silent=True) or {})
    scene = lib(library.scene_save, str(form.get("name", "")))
    if from_browser_form():
        return Response(status=303, headers={"Location": "/"})
    return jsonify(scene), 201


@app.post("/scenes/<scene_id>/apply")
def apply_scene(scene_id: str):
    changed = lib(library.scene_apply, scene_id)
    pushed = sum(registry.notify(d) for d in changed)
    if from_browser_form():
        return Response(status=303, headers={"Location": "/"})
    return jsonify({"changed": changed, "pushed_to": pushed})


@app.delete("/scenes/<scene_id>")
@app.post("/scenes/<scene_id>/delete")
def delete_scene(scene_id: str):
    lib(library.scene_remove, scene_id)
    if from_browser_form():
        return Response(status=303, headers={"Location": "/"})
    return "", 204


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "qoi": HAVE_QOI})


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # use_reloader off: a second process would double the mDNS browser.
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
