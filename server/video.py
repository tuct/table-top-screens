"""Video sources, read through ffmpeg.

An uploaded MP4, AVI, MOV or MKV is stored in the pool exactly as it arrived
-- originals only, like every other source -- and read back a frame at a time
when a screen asks for one. Nothing is transcoded at upload: framing, size and
frame rate are per screen and per variant, so a canonical intermediate would
be either wrong for someone or huge for everyone.

ffmpeg does the decoding, as a subprocess writing raw RGB to a pipe. A frame
at a time, so a two-hour file costs the same memory as a two-second one.

Which ffmpeg: whichever is on PATH, else the static build that ships with
`imageio-ffmpeg` (a pip dependency, no system install). The server works
without either -- videos are simply refused, with a line saying why.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache

from PIL import Image

# What we will take. The container is what ffmpeg is handed; the codecs inside
# are its problem, not ours.
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".mpg", ".mpeg", ".wmv"}
# Enough of a header to recognise a container without trusting the filename.
_MAGIC = (
    (4, b"ftyp"),          # MP4 / MOV, at offset 4
)
_LEADING = (
    b"\x1a\x45\xdf\xa3",   # Matroska / WebM
    b"OggS",
)


class NoFFmpeg(RuntimeError):
    """No ffmpeg anywhere. Videos cannot be read; everything else is fine."""


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    """The ffmpeg to use, preferring the system one.

    A system install is usually newer and built with more codecs than the
    bundled static one, and it is what the user chose.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise NoFFmpeg(
            "no ffmpeg on PATH and imageio-ffmpeg is not installed; "
            "run `pip install imageio-ffmpeg` or `brew install ffmpeg`"
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def have_ffmpeg() -> bool:
    try:
        return bool(ffmpeg_exe())
    except NoFFmpeg:
        return False


def looks_like_video(body: bytes, filename: str = "") -> bool:
    """Is this a video container? Magic bytes first, the name as a fallback."""
    head = body[:64]
    for offset, magic in _MAGIC:
        if head[offset:offset + len(magic)] == magic:
            return True
    if any(head.startswith(m) for m in _LEADING):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return True
    return os.path.splitext(filename.lower())[1] in VIDEO_EXTENSIONS


_DURATION = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d)\.(\d+)")
_VIDEO_LINE = re.compile(r"Stream #\d+:\d+.*?: Video: .*?(\d{2,5})x(\d{2,5})")
_RATE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")


def probe(body: bytes) -> dict:
    """Size, length and frame rate, from what ffmpeg says about the file.

    Read from ffmpeg's own banner rather than ffprobe, which the bundled
    build does not ship. Asking it to decode nothing (`-f null -` on no
    output) would cost a full pass; `-i` alone stops after the header.
    """
    with _as_file(body) as path:
        out = subprocess.run(
            [ffmpeg_exe(), "-hide_banner", "-i", path],
            capture_output=True, text=True, timeout=60,
        ).stderr

    duration_ms = 0
    if (m := _DURATION.search(out)) is not None:
        hours, minutes, seconds, frac = m.groups()
        duration_ms = ((int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000
                       + int(frac.ljust(3, "0")[:3]))
    width = height = 0
    if (m := _VIDEO_LINE.search(out)) is not None:
        width, height = int(m.group(1)), int(m.group(2))
    fps = 0.0
    if (m := _RATE.search(out)) is not None:
        fps = float(m.group(1))
    if not width or not height:
        raise ValueError("ffmpeg found no video stream in this file")
    return {
        "width": width,
        "height": height,
        "duration_ms": duration_ms,
        "fps": fps or 25.0,
        # An estimate: containers lie, and counting exactly costs a full
        # decode. Only ever used to say roughly how long a clip is.
        "frames": max(1, round((duration_ms / 1000.0) * (fps or 25.0))),
    }


def iter_video_frames(body: bytes, fps: float, width: int, height: int,
                      limit: int | None = None, info: dict | None = None):
    """Yield RGB frames at `fps`, each no larger than width x height.

    Scaled by ffmpeg rather than Pillow because it is the cheap place to do
    it: decoding 4K and handing back 4K costs 25 MB a frame down the pipe,
    and every screen here wants well under one. The framing (fit, crop,
    letterbox) is applied afterwards by the caller, exactly as for a GIF.

    The output size is computed here and passed as exact numbers rather than
    left to `force_original_aspect_ratio`, which quietly UPSCALES a source
    smaller than the box -- and a stride that disagrees with ffmpeg by one
    pixel turns the whole stream into garbage.
    """
    info = info or probe(body)
    size = fit_inside(info["width"], info["height"], width, height)
    stride = size[0] * size[1] * 3
    scale = f"scale={size[0]}:{size[1]},fps={fps:.6g}"
    with _as_file(body) as path:
        proc = subprocess.Popen(
            [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", path,
             "-vf", scale, "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            sent = 0
            while limit is None or sent < limit:
                raw = _read_exactly(proc.stdout, stride)
                if raw is None:
                    break
                yield Image.frombytes("RGB", size, raw)
                sent += 1
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait(timeout=10)


def fit_inside(src_w: int, src_h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """The biggest size with the source's shape that fits the box, never
    larger than the source. Even numbers, which is what rgb24 scaling wants."""
    factor = min(box_w / src_w, box_h / src_h, 1.0)
    return (max(2, int(src_w * factor) & ~1), max(2, int(src_h * factor) & ~1))


def first_frame(body: bytes, width: int = 0, height: int = 0) -> Image.Image:
    """One frame, for a thumbnail or for a video shown as a still."""
    info = probe(body)
    for img in iter_video_frames(body, info["fps"] or 25.0,
                                 width or info["width"], height or info["height"],
                                 limit=1, info=info):
        return img
    raise ValueError("ffmpeg produced no frames")


def _read_exactly(stream, count: int) -> bytes | None:
    """`count` bytes, or None at a clean end of stream."""
    chunks = []
    got = 0
    while got < count:
        chunk = stream.read(count - got)
        if not chunk:
            return None
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


class _as_file:
    """ffmpeg seeks, so it needs a real file rather than a pipe.

    MP4 in particular keeps its index at the end for a file written in one
    pass, and a non-seekable input makes ffmpeg read the whole thing before
    it can start.
    """

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.path = ""

    def __enter__(self) -> str:
        fd, self.path = tempfile.mkstemp(suffix=".video")
        with os.fdopen(fd, "wb") as fh:
            fh.write(self.body)
        return self.path

    def __exit__(self, *exc) -> None:
        if self.path:
            try:
                os.unlink(self.path)
            except OSError:
                pass
