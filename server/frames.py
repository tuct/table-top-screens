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

import video

# Keep the synthetic clip short enough to loop visibly, long enough that a
# stuck frame counter is obvious.
SYNTHETIC_FRAMES = 60


# Formats Pillow can read multiple frames from, and a browser can animate
# from the original bytes. APNG is reported by Pillow as "PNG" with
# n_frames > 1 -- there is no separate format name for it, which is why
# sniffing the format string alone would miss it.
ANIMATED_FORMATS = {"GIF", "PNG", "WEBP"}


def is_video(body: bytes | None, filename: str = "") -> bool:
    """A video container, which Pillow cannot open and ffmpeg can."""
    return bool(body) and video.looks_like_video(body, filename)


def frame_count(body: bytes) -> int:
    """How many frames the uploaded image has. 1 for a still."""
    if is_video(body):
        return video.probe(body)["frames"]
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
    than trusting the format name. A video is read by ffmpeg instead, and is
    never "browser playable" here: the lists show a rendered thumbnail, not
    the file.
    """
    if is_video(body):
        info = video.probe(body)
        return {"animated": True, "frames": info["frames"],
                "duration_ms": info["duration_ms"], "browser_playable": False,
                "format": "VIDEO", "fps": info["fps"],
                "size": [info["width"], info["height"]]}
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


def durations(body: bytes | None) -> list[int]:
    """Per-frame display time in ms, from the best available source.

    Same rule as describe_motion(): a missing or zero duration means "as fast
    as you can", which browsers treat as ~100 ms, so that is what we use too.
    """
    if is_video(body):
        # Every frame the same length, which is what a constant frame rate
        # means -- and a variable-rate file is resampled by ffmpeg anyway.
        info = video.probe(body)
        step = max(1, round(1000 / (info["fps"] or 25.0)))
        return [step] * max(1, info["frames"])
    if body is not None and is_animated(body):
        with Image.open(io.BytesIO(body)) as im:
            return [int(f.info.get("duration") or 0) or 100
                    for f in ImageSequence.Iterator(im)]
    return [100] * SYNTHETIC_FRAMES


def resample(durs: list[int], fps: int) -> list[int]:
    """Source frame index for each output frame of one cycle played at `fps`.

    Output frame k shows whichever source frame is on screen at t = k/fps, so
    a clip keeps its real speed whatever rate the device plays at: a slow GIF
    repeats frames, a fast one drops them. At least one frame, always.
    """
    total = sum(durs)
    count = max(1, round(total * fps / 1000))
    out: list[int] = []
    src, ends = 0, durs[0]
    for k in range(count):
        t = k * 1000 / fps
        while t >= ends and src < len(durs) - 1:
            src += 1
            ends += durs[src]
        out.append(src)
    return out


def iter_frames(body: bytes | None, indices: list[int], w: int, h: int):
    """Yield (index, RGB image) for each DISTINCT index, in ascending order.

    One open and forward seeks only. extract() reopens and seeks per call,
    which for a long GIF is quadratic -- fine for one frame, not for a clip.
    """
    wanted = sorted(set(indices))
    if is_video(body):
        # ffmpeg is already resampling to the rate the caller asked for, so
        # what comes out of the pipe IS the output sequence -- numbered as
        # the caller numbered it, however many source frames it took.
        info = video.probe(body)
        fps = (len(indices) * 1000.0) / max(1, sum(durations(body))) if indices else info["fps"]
        n = 0
        for img in video.iter_video_frames(body, fps or info["fps"], w, h,
                                           limit=len(wanted), info=info):
            yield wanted[n], img
            n += 1
            if n >= len(wanted):
                break
        # A container that ends early still has to fill the sequence, or the
        # caller is left with a hole it cannot render.
        while n < len(wanted):
            yield wanted[n], Image.new("RGB", (w, h), (0, 0, 0))
            n += 1
        return
    if body is not None and is_animated(body):
        with Image.open(io.BytesIO(body)) as im:
            for n in wanted:
                im.seek(n)
                yield n, im.convert("RGB")
        return
    for n in wanted:
        yield n, synthetic(n, w, h)


def source_info(body: bytes | None) -> dict:
    """What the device is about to play, for the UI and the API."""
    if is_video(body):
        return {"source": "video", "frames": video.probe(body)["frames"]}
    if body is not None and is_animated(body):
        return {"source": "uploaded", "frames": frame_count(body)}
    return {"source": "synthetic", "frames": SYNTHETIC_FRAMES}


def frame(body: bytes | None, n: int, w: int, h: int) -> Image.Image:
    """Frame `n` from the best available source, as an RGB image."""
    if is_video(body):
        # Seeking to an arbitrary frame costs a decode from the last keyframe;
        # the one caller that wants a single frame wants the first.
        return video.first_frame(body, w, h)
    if body is not None and is_animated(body):
        return extract(body, n)
    return synthetic(n, w, h)
