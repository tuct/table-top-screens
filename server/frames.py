"""Frame sources for the MJPEG experiment.

A "video" here is just a sequence of frames the device fetches one at a time.
Two sources, neither needing ffmpeg:

* **A multi-frame image already in the pool** -- GIF, animated WebP, APNG.
  Pillow reads these natively, so uploading a GIF is uploading a video.
* **A synthetic test animation**, used when the current item is a still. It
  is deterministic and cheap, which is what you want when the thing being
  measured is the *device*, not the content.

Why frames rather than a real MJPEG stream: ESPHome's `online_image` fetches
one URL at a time, so the cheap experiment -- no new C++ component -- is to
serve frame N on request and let the device ask for N+1. That measures the
decode-and-draw cost honestly; it just pays an HTTP round trip per frame,
which a real streaming component would not. Measure first, then decide
whether the component is worth writing.
"""

from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw, ImageSequence

# Keep the synthetic clip short enough to loop visibly, long enough that a
# stuck frame counter is obvious.
SYNTHETIC_FRAMES = 60


# Formats Pillow can read multiple frames from, and a browser can animate
# from the original bytes. APNG is reported by Pillow as "PNG" with
# n_frames > 1 -- there is no separate format name for it, which is why
# sniffing the format string alone would miss it.
ANIMATED_FORMATS = {"GIF", "PNG", "WEBP"}


def frame_count(body: bytes) -> int:
    """How many frames the uploaded image has. 1 for a still."""
    try:
        with Image.open(io.BytesIO(body)) as im:
            return getattr(im, "n_frames", 1) or 1
    except Exception:  # noqa: BLE001 - Pillow raises many types
        return 1


def is_animated(body: bytes) -> bool:
    return frame_count(body) > 1


def describe_motion(body: bytes) -> dict:
    """Whether this is a clip, how long, and whether a browser can play it.

    Covers APNG as well as GIF and animated WebP. Pillow calls an APNG
    "PNG" with n_frames > 1, so this asks `is_animated` / `n_frames` rather
    than trusting the format name.
    """
    try:
        with Image.open(io.BytesIO(body)) as im:
            fmt = im.format or ""
            count = getattr(im, "n_frames", 1) or 1
            if count <= 1:
                return {"animated": False, "frames": 1, "duration_ms": 0,
                        "browser_playable": False}
            total = 0
            for f in ImageSequence.Iterator(im):
                # Per-frame duration; GIF and APNG both report it here, and a
                # missing or zero value means "as fast as you can", for which
                # browsers substitute ~100ms.
                total += int(f.info.get("duration") or 0) or 100
            return {
                "animated": True,
                "frames": count,
                "duration_ms": total,
                "browser_playable": fmt.upper() in ANIMATED_FORMATS,
                "format": fmt,
            }
    except Exception:  # noqa: BLE001 - Pillow raises many types
        return {"animated": False, "frames": 1, "duration_ms": 0,
                "browser_playable": False}


def extract(body: bytes, n: int) -> Image.Image:
    """Frame `n` (wrapping) of a multi-frame image, flattened to RGB.

    GIF frames are often partial updates on a shared canvas, so each frame is
    composited by seeking rather than read in isolation -- otherwise later
    frames come out as fragments on transparent black.
    """
    with Image.open(io.BytesIO(body)) as im:
        total = getattr(im, "n_frames", 1) or 1
        im.seek(n % total)
        return im.convert("RGB")


def synthetic(n: int, w: int, h: int) -> Image.Image:
    """A deterministic test clip: a dot orbiting a moving hue, plus a frame
    counter. Every element is there to make a problem visible --

    * the orbit shows dropped or reordered frames as a stutter or jump,
    * the sweeping background shows tearing as a visible seam,
    * the printed number tells you which frame you are actually looking at.
    """
    t = (n % SYNTHETIC_FRAMES) / SYNTHETIC_FRAMES
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)

    # Background: a vertical sweep whose phase advances each frame.
    for y in range(h):
        v = (y / max(h - 1, 1) + t) % 1.0
        d.line(
            [(0, y), (w, y)],
            fill=(int(40 + 60 * v), int(20 + 40 * (1 - v)), int(70 + 90 * v)),
        )

    # Orbiting dot.
    cx, cy = w / 2, h / 2
    r = min(w, h) * 0.34
    a = 2 * math.pi * t
    x, y = cx + r * math.cos(a), cy + r * math.sin(a)
    rad = max(4, min(w, h) // 12)
    d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=(255, 230, 60))

    # A second dot at half speed, so a doubled or halved rate is obvious.
    a2 = math.pi * t
    x2, y2 = cx + r * 0.55 * math.cos(a2), cy + r * 0.55 * math.sin(a2)
    rad2 = max(3, rad // 2)
    d.ellipse([x2 - rad2, y2 - rad2, x2 + rad2, y2 + rad2], fill=(80, 230, 255))

    d.text((4, 4), f"{n % SYNTHETIC_FRAMES:03d}", fill=(255, 255, 255))
    return img


def source_info(body: bytes | None) -> dict:
    """What the device is about to play, for the UI and the API."""
    if body is not None and is_animated(body):
        return {"source": "uploaded", "frames": frame_count(body)}
    return {"source": "synthetic", "frames": SYNTHETIC_FRAMES}


def frame(body: bytes | None, n: int, w: int, h: int) -> Image.Image:
    """Frame `n` from the best available source, as an RGB image."""
    if body is not None and is_animated(body):
        return extract(body, n)
    return synthetic(n, w, h)
