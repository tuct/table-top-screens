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

from flask import (Flask, Response, abort, jsonify, make_response,
                   render_template, request)
from markupsafe import Markup
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


def screen_shape(screen) -> str:
    """A screen's panel shape, as variants are keyed: "480x800", "240x240r"."""
    return f"{screen.width}x{screen.height}" + ("r" if screen.round else "")


def note_shape(screen) -> None:
    """Record the shape a screen actually has, so its variants are made for
    it. Recorded rather than looked up per render, so framing still works
    while the screen is offline."""
    try:
        library.set_shape(DATA_DIR, screen.name, screen_shape(screen))
    except library.LibraryError:
        pass  # an odd size is not worth failing a push over


def _frames_for(screen) -> int:
    """Frame count of what `screen` is showing, as it should play it; 0 if
    nothing.

    Comes from the metadata recorded at upload, so a still is 1 and a clip is
    its real length -- no decoding here. The device caches and auto-plays when
    this is > 1, which is what makes animation need no button press. A screen
    that did not advertise the clip's format gets 1: `/image` already renders
    frame 0, so it simply shows a still.
    """
    # Every push comes through here, which makes it the one place that always
    # sees a live Screen and can keep its panel shape current.
    note_shape(screen)
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
    """A token that names exactly what `device` is showing: variant + framing.

    The pool stores items under a content hash, so the item id already names
    the picture. The screen's stored prefs (fit, rotation, zoom, background,
    quality) change what it receives just as much, so they are folded in: a
    screen that caches rendered content by this token must never be handed a
    token it already holds for different pixels. And the same item with the
    same prefs always gives the same token, which is what lets a screen switch
    back to something it has without pulling it again.
    """
    try:
        entry = library.current_variant(DATA_DIR, device)
        config = library.config_for(DATA_DIR, device)
    except library.LibraryError:
        return None
    if not entry:
        return None
    # The variant id alone would not change when its framing is edited, and
    # the source id alone cannot tell two variants of one picture apart.
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()
    return f"{entry['src']}.{entry['id']}.{digest[:8]}" if config else f"{entry['src']}.{entry['id']}"


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
    # Pool routes are per SOURCE; a row's `id` is its variant.
    item = {**item, "id": item.get("src_id", item["id"])}
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

    Keys are `<source>.<variant>[.<framing hash>]-f<fps>-q<quality>` for
    server content (the token from _version_for, plus what the screen adds in
    mjpeg-clip.yaml) and `file:<name>` for a file copied onto the card.
    """
    if key.startswith("file:"):
        return {"file": key[5:], "item_id": None, "variant": None, "fps": None,
                "framed": False}
    m = re.match(r"^([0-9A-Za-z]+)((?:\.[0-9a-f]+)*)(?:-f(\d+))?", key)
    if not m:
        return {"file": None, "item_id": None, "variant": None, "fps": None,
                "framed": False}
    parts = [p for p in m.group(2).split(".") if p]
    return {
        "file": None,
        "item_id": m.group(1),
        "variant": parts[0] if parts else None,
        "fps": int(m.group(3)) if m.group(3) else None,
        # A framing hash rides along only when the variant has framing of its
        # own, so two parts means "not just the plain picture".
        "framed": len(parts) > 1,
    }


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
    # The current variant's framing, over the screen's defaults -- the same
    # thing /clip.mjpeg renders with, so the preview matches the panel.
    prefs = lib(library.config_for, device) if use_prefs else {}

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
    """(fit, rot, zoom, bg) for what this screen is showing.

    From the current VARIANT's config, over the screen's defaults -- so two
    pictures on one screen can be framed differently. Clips and stills read
    the same thing, so video is cropped and rotated like the stills are. Bad
    stored values fall back rather than failing a render.
    """
    prefs = lib(library.config_for, device)
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
    """JPEG quality for what this screen shows: variant config, else `fallback`.

    Same precedence as everywhere else -- a stored pref beats the query
    string -- so the quality control on the device page reaches clips too, not
    only the `/image` path. Without this it changed the content token (which
    folds in every pref) and so forced a re-download of identical bytes.
    """
    prefs = lib(library.config_for, device)
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
    state = lib(library.used, device)
    current = lib(library.variant, state["current"]) if state["current"] else None
    return jsonify(
        {
            # The variant on the screen, and the source it came from: callers
            # that care about "which picture" want the second.
            "current": state["current"],
            "current_src": (current or {}).get("src"),
            "shape": state["shape"],
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


@app.post("/d/<device>/variants/<variant_id>/duplicate")
def duplicate_variant(device: str, variant_id: str):
    """A second framing of the same picture on this screen.

    The copy starts from the original's framing, is put on the screen, and is
    what the framing controls then edit -- so "keep this crop, try another"
    is two clicks rather than a re-upload.
    """
    entry = lib(library.variant_duplicate, variant_id, request.form.get("name", ""))
    lib(library.select, device, entry["id"])
    pushed = registry.notify(device)
    if from_browser_form():
        return Response(status=303, headers={"Location": f"/d/{device}/"})
    return jsonify({"variant": entry, "pushed_to": pushed})


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
    by_variant = {v["id"]: v for v in lib(library.variants)}
    return jsonify({
        "order": state["used"],
        # The same order as sources, for callers that dragged pictures rather
        # than variants.
        "order_src": [by_variant[i]["src"] for i in state["used"] if i in by_variant],
    })


@app.post("/d/<device>/prefs")
def post_prefs(device: str):
    """Frame what this screen is showing, then push so it is visible.

    Writes to the CURRENT VARIANT -- framing belongs to the picture, not the
    screen, so reframing one does not move the rest. The same values are kept
    as the screen's defaults, which seed the next picture put on it; that is
    what makes "set this screen to cover" still a single action.
    """
    device_dir(device)  # validates the name
    # Start from what is actually in effect, so a relative zoom step adds to
    # the variant's zoom rather than the screen default's.
    prefs = lib(library.config_for, device)
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
    # The swatches cover four colours; anything else is typed, and wins.
    if (hex_bg := str(form.get("bg_hex", "")).strip()):
        prefs["bg"] = hex_bg

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
    entry = lib(library.current_variant, device)
    if entry is not None:
        # Applied with the framing, not only when duplicating: naming a
        # framing after the fact is the normal case ("this is the face crop").
        # Reset puts the variant back to plain, so the name goes with it.
        if form.get("reset"):
            lib(library.variant_set_labels, entry["id"], "", "")
        else:
            name, desc = form.get("name"), form.get("desc")
            if name is not None or desc is not None:
                lib(library.variant_set_labels, entry["id"], name, desc)
    if entry is not None:
        lib(library.variant_set_config, entry["id"],
            prefs if prefs else dict.fromkeys(library.CONFIG_KEYS))
        if not prefs:
            # Reset means "no framing of its own" -- an empty config, not keys
            # set to None, which would render as garbage.
            lib(library.variant_set_config_exact, entry["id"], {})
    pushed = registry.notify(device)

    if from_browser_form():
        return Response(status=303, headers={"Location": f"/d/{device}/"})
    return jsonify({"prefs": prefs, "variant": (entry or {}).get("id"), "pushed_to": pushed})


@app.get("/d/<device>/meta")
def get_meta(device: str):
    meta = read_meta(device)
    if not meta:
        abort(404, "no content for this device yet")
    return jsonify(meta)








# Small inline SVGs, as Markup so the template can place them directly.
# Icons live here rather than in the template so a row's markup stays legible.
ICONS = {
    "grip": Markup(
        '<svg class="grip" width="16" height="16" viewBox="0 0 24 24" fill="none"'
        ' stroke="#9a988f" stroke-width="2" stroke-linecap="round" aria-hidden="true">'
        '<circle cx="9" cy="6" r="1"/><circle cx="15" cy="6" r="1"/>'
        '<circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/>'
        '<circle cx="9" cy="18" r="1"/><circle cx="15" cy="18" r="1"/></svg>'
    ),
    "plus": Markup(
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
        ' stroke-width="2" stroke-linecap="round" aria-hidden="true">'
        '<path d="M12 5v14M5 12h14"/></svg>'
    ),
    "minus": Markup(
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
        ' stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M5 12h14"/></svg>'
    ),
    "trash": Markup(
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
        ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>'
    ),
    "up": Markup(
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
        ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M12 16V4"/><path d="m6 10 6-6 6 6"/><path d="M4 20h16"/></svg>'
    ),
}

# Quality: low values exist for clips, where quality sets bytes per frame and
# a screen caches a fixed number of BYTES -- so dropping it is what buys a
# longer clip and a faster download.
QUALITIES = [
    (35, "35 · longest"), (50, "50"), (65, "65"), (75, "75"),
    (85, "85"), (95, "95 · default"), (100, "100 · sharpest"),
]
FIT_CHOICES = [("contain", "Contain"), ("cover", "Cover"),
               ("width", "Width"), ("height", "Height")]
ROT_CHOICES = [(0, "0°"), (90, "90°"), (180, "180°"), (270, "270°")]
BG_CHOICES = [("auto", "Auto", None), ("black", "Black", "#000000"),
              ("white", "White", "#ffffff"), ("#808080", "Grey", "#6b6a65")]

# How the shelf is ordered. Live screens first by default: those are the ones
# you can actually send to, so they are the ones worth reaching first.
SORTS = [("status", "Online first"), ("name", "Name"), ("seen", "Last seen")]
SORTS_BY = {
    "status": lambda c: (0 if c["online"] else 1, c["name"].lower()),
    "name": lambda c: c["name"].lower(),
    "seen": lambda c: (-c["seen"], c["name"].lower()),
}
DEFAULT_SORT = "status"


def geometry(width: int, height: int, is_round: bool) -> str:
    """How the panel is shaped, in one phrase: "480×800 portrait".

    Orientation is the panel's own, not the framing's: it says which way the
    hardware stands, which is what decides how a picture has to be cropped. A
    round panel has no orientation worth the word.
    """
    shape = "round" if is_round else (
        "portrait" if height > width else "landscape" if width > height else "square"
    )
    return f"{width}×{height} {shape}"


def panel_of(device: str, screen) -> tuple[int, int, bool]:
    """(width, height, round) for a screen that may be offline.

    From the registry when it is there, else from the shape recorded for it --
    a screen does not change size or grow corners while it is away, and its
    pictures should keep being drawn for the panel they are going to.
    """
    if screen is not None:
        return screen.width, screen.height, bool(screen.round)
    # What it looked like when we last saw it beats a shape guessed from
    # nothing -- a screen with no content of its own has no recorded shape.
    was = lib(library.seen).get(device) or {}
    if was.get("width") and was.get("height"):
        return int(was["width"]), int(was["height"]), bool(was.get("round"))
    shape = lib(library.shape_of, device)
    body, is_round = (shape[:-1], True) if shape.endswith("r") else (shape, False)
    try:
        width, height = (int(n) for n in body.split("x"))
    except ValueError:
        return 800, 480, False
    return width, height, is_round


def geometry_of(device: str, screen) -> str:
    """The shape phrase for a screen that may be offline."""
    return geometry(*panel_of(device, screen))


def caps_line(screen) -> str:
    """The screen in its own words: size, what it animates, what it takes."""
    if screen is None:
        return "not seen on the network"
    bits = [geometry(screen.width, screen.height, screen.round)]
    if screen.anim:
        bits.append(" ".join(screen.anim))
    elif screen.anim is not None:
        bits.append("stills only")
    bits.append(" ".join(screen.img))
    if screen.clip:
        bits.append(screen.clip)
    if screen.sd:
        bits.append("sd")
    return " · ".join(bits)


def sd_line(screen) -> str:
    """The card, in one line: whether it is in, how full, what it is for.

    Separate from the cache report -- a screen can have a card and never
    report memory, and can have a card it only keeps clips on.
    """
    if screen is None or not screen.sd:
        return ""
    state = screen.sd_state or {}
    if not state:
        return "SD: not read yet"
    if not state.get("mounted"):
        return "SD: no card in the slot"
    use = "stills and clips" if state.get("in_use") else "clips only"
    total, free = state.get("total_mb"), state.get("free_mb")
    line = f"SD: card in, {use}"
    if total and free is not None:
        line += f" · {free / 1024:.1f} of {total / 1024:.1f} GB free"
    return line


def cache_view(device: str) -> dict | None:
    """What the screen last reported holding, as rows for the page."""
    state = lib(library.screen_state, device)
    report = (state or {}).get("report")
    if not report:
        return None
    names = {it["id"]: it["filename"] for it in lib(library.pool)}
    budget, used = report.get("budget") or 0, report.get("used") or 0
    entries = []
    for r in cache_entries(state):
        where = []
        if "mem" in r:
            where.append(f"memory {_kb(r['mem'])}")
        if "card" in r:
            where.append(f"card {_kb(r['card'])}")
        extra = []
        if r.get("frames") and r["frames"] > 1:
            extra.append(f"{r['frames']} frames")
        if r["fps"]:
            extra.append(f"{r['fps']} fps")
        if r["framed"]:
            extra.append("custom framing")
        if r["file"]:
            extra.append("SD file")
        entries.append({
            "name": r["file"] or names.get(r["item_id"], r["item_id"] or r["key"]),
            "where": " · ".join(where),
            "extra": ", ".join(extra),
            "shown": r["shown"],
        })
    return {
        "used": _kb(used), "budget": _kb(budget),
        "pct": round(100 * used / budget) if budget else 0,
        "psram_free": _kb(report.get("psram_free")) if report.get("psram_total") else "",
        "psram_total": _kb(report["psram_total"]) if report.get("psram_total") else "",
        "heap_free": _kb(report["heap_free"]) if report.get("heap_free") is not None else "",
        "ago": _ago(state.get("at")),
        "entries": entries,
    }


@app.get("/d/<device>/")
def device_page(device: str):
    device_dir(device)  # validates the name
    meta = read_meta(device)
    # The framing OF THE PICTURE ON THE SCREEN, not the screen's defaults: the
    # controls edit this variant, so they have to show its values. Selecting
    # another picture reloads the page and brings up that picture's framing.
    config = lib(library.config_for, device)

    screens = registry.for_device(device)
    screen = screens[0] if screens else None
    pw, ph, is_round = panel_of(device, screen)
    # A preview, not the exhibit: big enough to judge a crop, small enough to
    # sit beside the settings rather than push them off the page.
    scale = min(1.0, 300 / max(pw, ph, 1))
    vw, vh = max(1, round(pw * scale)), max(1, round(ph * scale))

    used_state = lib(library.used, device)
    used_items = lib(library.used_items, device)
    unused_items = lib(library.unused_items, device)

    # "cached" belongs in the row you choose from: whether the screen already
    # holds a picture is the difference between switching instantly and
    # waiting for a download.
    held: dict[str, int] = {}
    for r in cache_entries(lib(library.screen_state, device)):
        if r["item_id"]:
            held[r["item_id"]] = held.get(r["item_id"], 0) + (r.get("mem", 0) or r.get("card", 0))

    def row(it: dict) -> dict:
        source_id = it.get("src_id", it["id"])
        motion = motion_of(it)
        # The given name leads, because "face" says more than
        # PXL_20260909_133347043.MP.jpg. The filename is still the truth about
        # which file this is, so it shows on the one you have selected.
        return {
            "id": it["id"],
            "title": it.get("variant") or it["filename"],
            "desc": it.get("desc", ""),
            "filename": it["filename"],
            # NOT "copy": Jinja resolves a dict's attributes before its keys,
            # so `it.copy` would render dict.copy, the built-in method.
            "badge": "copy" if not it.get("auto", True) and not it.get("variant") else "",
            "variant": it.get("variant", ""),
            "thumb": thumb_src(it),
            "facts": f'{it["source_size"][0]}×{it["source_size"][1]} · {_kb(it["bytes"])}',
            "cached": _kb(held[source_id]) if held.get(source_id) else "",
            "current": it["id"] == used_state["current"],
            "still_here": plays_as_still(screen, it) if screen else False,
            "source_format": it.get("source_format", ""),
            "anim": (f'{motion["frames"]} · {(motion.get("duration_ms") or 0) / 1000:.1f}s'
                     if motion.get("animated") else ""),
        }

    entry = lib(library.current_variant, device)
    settings = None
    if entry is not None:
        bg_now = str(config.get("bg", "black"))
        known_bg = {value for value, _, _ in BG_CHOICES}
        settings = {
            "variant": entry["id"],
            "name": entry["name"],
            "desc": entry.get("desc", ""),
            "fit": FIT_ALIASES.get(str(config.get("fit", "contain")), str(config.get("fit", "contain"))),
            "fits": FIT_CHOICES,
            "rot": int(config.get("rot", 0)),
            "rots": ROT_CHOICES,
            "zoom": int(config.get("zoom", 100)),
            "zoom_min": ZOOM_MIN,
            "zoom_max": ZOOM_MAX,
            "q": config.get("q", DEFAULT_QUALITY),
            "qualities": QUALITIES,
            "bg": bg_now,
            "bgs": [(value, label, colour, "bg-" + re.sub(r"[^a-z0-9]", "", value))
                    for value, label, colour in BG_CHOICES],
            "bg_custom": "" if bg_now in known_bg else bg_now,
        }

    # The bezel's glass takes the letterbox colour, so the preview shows what
    # the panel will show around the picture.
    letterbox = {"auto": "#ffffff", "black": "#000000", "white": "#ffffff"}.get(
        str(config.get("bg", "black")),
        str(config.get("bg")) if str(config.get("bg", "")).startswith("#") else "#6b6a65",
    )

    return render_template(
        "device.html",
        device=device,
        online=screen is not None,
        last_error=screen.last_error if screen else "",
        caps_line=(caps_line(screen) if screen
                   else " · ".join(x for x in [geometry_of(device, None),
                                               "not seen on the network"] if x)),
        round=is_round,
        icon=ICONS,
        meta={
            "filename": meta["filename"],
            "format": meta["source_format"],
            "width": meta["source_size"][0],
            "height": meta["source_size"][1],
            "size": _kb(meta["bytes"]),
            "when": time.strftime("%d %b %H:%M", time.localtime(meta["uploaded_at"])),
        } if meta else None,
        preview={"w": vw, "h": vh, "letterbox": letterbox,
                 "stamp": int(meta["uploaded_at"]) if meta else 0},
        settings=settings,
        used=[row(it) for it in used_items],
        unused=[row(it) for it in unused_items],
        cache=cache_view(device),
        sd=sd_line(screen),
    )


IDLE_NOTE = ("A scene saves the picture and settings of the screens you pick, "
             "and puts them all back at once.")
EMPTY_NOTE = ("None yet. A scene saves the picture and settings of the screens "
              "you pick, and restores them all at once.")


def scene_marks() -> dict:
    """Which scene is up, and which has been changed out from under it.

    "On screen" is not "was applied last": it means the screens still show
    what the scene recorded. The one that was applied and no longer matches
    gets the star -- the way an edited document does. Only that one: a scene
    nobody has applied is not changed, it is simply not up.
    """
    stored = lib(library.scenes)
    latest = max((s.get("applied_at") or 0 for s in stored), default=0)
    by_id = {}
    for s in stored:
        # Both marks belong to the scene you are IN. Another scene that
        # happens to match is not "on screen" -- nothing put it there -- and
        # after leaving a scene nothing claims to be up at all.
        here = bool(latest and (s.get("applied_at") or 0) == latest)
        on = here and lib(library.scene_on_screen, s["id"])
        by_id[s["id"]] = {
            "id": s["id"],
            "name": s["name"],
            "on_screen": on,
            "dirty": here and not on,
        }
    order = [by_id[s["id"]] for s in reversed(stored)]
    # The scene you are working in: the last one put up, whether or not the
    # screens have since moved away from it.
    active = None
    if latest:
        s = next(x for x in stored if (x.get("applied_at") or 0) == latest)
        active = {"id": s["id"], "name": s["name"],
                  "members": sorted((s.get("entries") or {}).keys())}
    return {
        "by_id": by_id,
        "scenes": order,
        "active": active,
        "on_screen": next((m["name"] for m in order if m["on_screen"]), ""),
        "changed": next((m["name"] for m in order if m["dirty"]), ""),
    }


def scene_note(marks: dict, any_scenes: bool) -> dict:
    """The one-line summary above the scene list, in parts the page can swap.

    A scene that matches outranks one that has been changed since: what is
    actually on the screens is worth more than what was last clicked.
    """
    if marks["on_screen"]:
        return {"who": marks["on_screen"], "star": False,
                "tail": "is on the screens now."}
    if marks["changed"]:
        return {"who": marks["changed"], "star": True,
                "tail": "was put up, and a screen has been changed since."}
    return {"who": "", "star": False, "tail": IDLE_NOTE if any_scenes else EMPTY_NOTE}


@app.get("/scenes/state")
def scenes_state():
    """Just the marks, for a page that switched a picture without reloading."""
    marks = scene_marks()
    return jsonify({"scenes": marks["scenes"],
                    "note": scene_note(marks, bool(marks["scenes"]))})


@app.get("/")
def index():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # A screen is worth a card if it is on the network OR we hold state for
    # it. Live ones first: those are the ones you can actually send to.
    online = {sc.name: sc for sc in registry.all()}
    # Remembered as we go, so a screen still has a card when it is asleep --
    # and so a restart does not lose one that was never given content.
    for sc in online.values():
        lib(library.note_seen, sc.name, {
            "key": registry.key_of(sc), "host": sc.host, "port": sc.port,
            "width": sc.width, "height": sc.height, "round": sc.round,
            "sd": sc.sd,
        })
    remembered = lib(library.seen)
    known = set(lib(library.devices)) | set(remembered)

    order = request.args.get("sort") or request.cookies.get("sort") or DEFAULT_SORT
    if order not in {key for key, _ in SORTS}:
        order = DEFAULT_SORT

    cards = []
    for name in sorted(online.keys() | known):
        sc = online.get(name)
        pw, ph, is_round = panel_of(name, sc)
        state = lib(library.used, name)
        # A screen with nothing assigned still gets a strip to pick from.
        items = lib(library.used_items, name) or lib(library.pool)
        current = next((i for i in items if i["id"] == state["current"]), None)

        # The panel in the tray, at its own proportions: a portrait screen
        # stands, a landscape one lies down, and both fit the same card.
        box_w, box_h = 250, 186
        scale = min(box_w / max(pw, 1), box_h / max(ph, 1))
        frame_w, frame_h = max(40, round(pw * scale)), max(40, round(ph * scale))
        config = lib(library.config_for, name)
        bg = str(config.get("bg", "black"))
        letterbox = {"auto": "#ffffff", "black": "#000000", "white": "#ffffff"}.get(
            bg, bg if bg.startswith("#") else "#6b6a65"
        )

        # What the screen last reported holding, as one bar and one line.
        cache = None
        view = cache_view(name)
        if view:
            detail = []
            if view["psram_total"]:
                detail.append(f'PSRAM {view["psram_free"]} of {view["psram_total"]} free')
            if view["heap_free"]:
                detail.append(f'heap {view["heap_free"]}')
            detail.append(view["ago"])
            cache = {"used": view["used"], "budget": view["budget"],
                     "pct": view["pct"], "detail": " · ".join(detail)}

        geo = geometry_of(name, sc)
        if sc:
            sub = f"{sc.host} · {geo}"
            sub_full = caps_line(sc)
        else:
            sub = geo or "offline"
            sub_full = "not seen on the network"

        sd = sd_line(sc)

        def card_item(it):
            motion = motion_of(it)
            return {
                "id": it["id"],
                "title": it.get("variant") or it["filename"],
                "filename": it["filename"],
                "thumb": thumb_src(it),
                "current": it["id"] == state["current"],
                "still_here": plays_as_still(sc, it) if sc else False,
                "anim": (f'{motion["frames"]} · {(motion.get("duration_ms") or 0) / 1000:.1f}s'
                         if motion.get("animated") else ""),
            }

        cards.append({
            "name": name,
            "online": sc is not None,
            "seen": sc.last_seen if sc else 0.0,
            "sub": sub,
            "sub_full": sub_full,
            "round": is_round,
            "frame_w": frame_w,
            "frame_h": frame_h,
            "letterbox": letterbox,
            # Rendered through the screen's own framing, so a card shows what
            # is actually on the panel rather than the original picture.
            "preview": f"/d/{name}/image?w={frame_w * 2}&h={frame_h * 2}&fmt=png",
            "stamp": int(time.time()),
            "current": card_item(current) if current else None,
            "thumbs": [card_item(it) for it in items],
            "cache": cache,
            "sd": sd,
        })

    cards.sort(key=SORTS_BY[order])

    # Which screens a scene may be built from: live ones can be added, ones
    # that are not answering can only be kept where a scene already has them.
    # Ticked by default when live: saving a scene means "these, as they are".
    choices = [{"name": c["name"], "online": c["online"], "member": c["online"]}
               for c in cards]

    def scene_shots(entries: dict) -> list[dict]:
        """A row of small pictures: what this scene puts on each screen.

        The pool thumbnail, not a render of the scene's framing -- it is the
        picture you are identifying at 34 pixels wide, and a screen that is
        round shows it round so the row reads like the shelf does.
        """
        shots = []
        for device in sorted(entries):
            variant_id = entries[device].get("item")
            entry = lib(library.variant, variant_id) if variant_id else None
            # "src" is what a stored variant calls it; "src_id" is the name it
            # takes in list rows. Scenes hold stored ids -- and pool ids
            # directly, for scenes saved before variants existed.
            src = (entry or {}).get("src") or variant_id
            item = (lib(library.pool_get, src)
                    if src and library.ITEM_RE.match(str(src)) else None)
            live = device in online
            shots.append({
                "device": device,
                "thumb": thumb_src(item) if item else "",
                "round": panel_of(device, online.get(device))[2],
                "online": live,
                "title": (f'{device} · {item["filename"]}' if item else
                          f"{device} · picture no longer in the library")
                         + ("" if live else " · offline"),
            })
        return shots

    marks = scene_marks()
    scenes = []
    for s in reversed(lib(library.scenes)):
        members = set((s.get("entries") or {}).keys())
        mark = marks["by_id"][s["id"]]
        scenes.append({
            "id": s["id"],
            "name": s["name"],
            "desc": s.get("desc", ""),
            "shots": scene_shots(s.get("entries") or {}),
            "on_screen": mark["on_screen"],
            "dirty": mark["dirty"],
            # A screen a scene remembers but we no longer hold state for.
            "lost": sorted(members - {c["name"] for c in choices}),
        })
    note = scene_note(marks, bool(scenes))

    # One save form, always at the top, always with the screens to tick. While
    # you are in a scene it saves back into that scene, and offers to save
    # beside it instead -- the name starts as the one you are in, so "save as"
    # takes a word changed rather than a name invented.
    here = marks["active"]
    in_scene = set(here["members"]) if here else set()
    save_form = {
        "scene_id": here["id"] if here else "",
        "name": here["name"] if here else "",
        "picks": [{"name": c["name"], "online": c["online"],
                   "member": (c["name"] in in_scene) if here else c["online"]}
                  for c in choices],
    }

    # Working in a scene means working on its screens: the rest are out of the
    # way until you ask for them, or add one to the scene. Never hidden to the
    # point of an empty shelf, and never hidden from the scene's own picker --
    # that is where a screen is added, so it has to list them all.
    # Hidden, not dropped: every card is still sent, so ticking a screen into
    # the scene can show it at once -- you tick it because you want to set
    # what it shows, and waiting for a round trip to see it is no good.
    active = marks["active"]
    narrowed = bool(active and request.args.get("all") != "1"
                    and any(c["name"] in active["members"] for c in cards))
    for c in cards:
        c["in_scene"] = bool(active and c["name"] in active["members"])
    scene_filter = {
        "name": active["name"] if active else "",
        "narrowed": narrowed,
        "showing_all": bool(active and request.args.get("all") == "1"),
        "shown": sum(1 for c in cards if c["in_scene"]) if narrowed else len(cards),
        "total": len(cards),
    }

    page = make_response(render_template(
        "index.html",
        screens=cards,
        online_count=len(online),
        upload_to=(sorted(online)[0] if online else (sorted(known)[0] if known else "")),
        scenes=scenes,
        save_form=save_form,
        note=note,
        scene_filter=scene_filter,
        # Set only just after a refresh, to say what the knocking found.
        checked=request.args.get("checked", type=int),
        gone=request.args.get("gone", type=int) or 0,
        back=request.args.get("back", type=int) or 0,
        sort=order,
        sorts=SORTS,
        icon=ICONS,
    ))
    # Asked for once, kept: the order you read the shelf in is a preference,
    # not a step in a journey, so it should survive the next visit.
    if request.args.get("sort"):
        page.set_cookie("sort", order, max_age=365 * 24 * 3600, samesite="Lax")
    return page


@app.post("/screens/refresh")
def refresh_screens():
    """Knock on every screen's door, and believe the answer.

    mDNS only reports a screen leaving when it says goodbye, so one that lost
    power stays on the shelf until its record expires -- possibly an hour.
    This checks now: anything that does not answer is marked offline, and any
    screen we remember that DOES answer comes back.
    """
    result = registry.recheck(lib(library.seen))
    if from_browser_form():
        back = redirect_target("/")
        joiner = "&" if "?" in back else "?"
        return Response(status=303, headers={
            "Location": f'{back}{joiner}checked={result["checked"]}'
                        f'&gone={len(result["gone"])}&back={len(result["back"])}'})
    return jsonify(result)


@app.get("/devices")
def devices():
    """Everything discovered on the network, and what it was told to fetch."""
    return jsonify([s.as_dict() for s in registry.all()])


@app.get("/scenes")
def list_scenes():
    return jsonify(lib(library.scenes))


def scene_selection() -> tuple[set[str] | None, set[str] | None]:
    """Which screens a scene form picked, and which of them are reachable.

    The page always sends the selection -- with a `pick` marker, so ticking
    nothing is telling the difference from a caller that never offered the
    choice. Those callers (the JSON API) keep the old behaviour: every screen,
    online or not.
    """
    data = request.form if request.form else (request.get_json(silent=True) or {})
    if hasattr(data, "getlist"):
        picked, marked = data.getlist("screens"), "pick" in data
    else:
        raw = data.get("screens")
        marked = isinstance(raw, list)
        picked = [str(x) for x in raw] if marked else []
    if not marked:
        return None, None
    return set(picked), {sc.name for sc in registry.all()}


@app.post("/scenes")
def save_scene():
    """Snapshot what the chosen screens are showing, and how, under a name."""
    form = request.form if request.form else (request.get_json(silent=True) or {})
    only, live = scene_selection()
    scene = lib(library.scene_save, str(form.get("name", "")), only, live)
    if from_browser_form():
        return Response(status=303, headers={"Location": "/"})
    return jsonify(scene), 201


@app.post("/scenes/<scene_id>/update")
def update_scene(scene_id: str):
    """Edit a scene in place: its name, its screens, and what they show.

    Saving is one act: the scene ends up meaning the screens as they are now.
    """
    form = request.form if request.form else (request.get_json(silent=True) or {})
    only, live = scene_selection()
    name = form.get("name")
    scene = lib(library.scene_update, scene_id, only, live,
                str(name) if name is not None else None)
    if from_browser_form():
        return Response(status=303, headers={"Location": "/"})
    return jsonify(scene)


@app.post("/scenes/<scene_id>/saveas")
def save_scene_as(scene_id: str):
    """Save what is up now as a new scene, leaving the original alone."""
    form = request.form if request.form else (request.get_json(silent=True) or {})
    only, live = scene_selection()
    scene = lib(library.scene_save_as, scene_id, str(form.get("name", "")),
                only, live)
    if from_browser_form():
        return Response(status=303, headers={"Location": "/"})
    return jsonify(scene), 201


@app.post("/scenes/<scene_id>/labels")
def label_scene(scene_id: str):
    """Rename or describe a scene, without touching what it recorded."""
    form = request.form if request.form else (request.get_json(silent=True) or {})
    name, desc = form.get("name"), form.get("desc")
    scene = lib(library.scene_set_labels, scene_id,
                str(name) if name is not None else None,
                str(desc) if desc is not None else None)
    if from_browser_form():
        return Response(status=303, headers={"Location": "/"})
    return jsonify(scene)


@app.post("/scenes/release")
def release_scene():
    """Leave the scene you are in. Deletes nothing -- only the mark."""
    lib(library.scene_release)
    if from_browser_form():
        return Response(status=303, headers={"Location": redirect_target("/")})
    return "", 204


@app.post("/scenes/<scene_id>/duplicate")
def duplicate_scene(scene_id: str):
    """A copy to change freely, leaving the original as it was."""
    form = request.form if request.form else (request.get_json(silent=True) or {})
    scene = lib(library.scene_duplicate, scene_id, str(form.get("name", "")))
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


@app.post("/d/<device>/remove")
def remove_device(device: str):
    """Forget a screen that is no longer on the shelf.

    Only one that is off the network: a live screen would be rediscovered
    within seconds and come back empty, which looks like a bug rather than a
    removal. Its pictures stay in the pool.
    """
    device_dir(device)  # validates the name
    if registry.for_device(device):
        abort(409, "that screen is on the network")
    if not lib(library.device_remove, device):
        abort(404)
    if from_browser_form():
        return Response(status=303, headers={"Location": redirect_target("/")})
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
