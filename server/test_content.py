"""Content pipeline tests: store, render, cache, reject.

No network and no device involved -- this covers the parts a screen depends
on being correct: that it gets its panel's exact pixel dimensions, that the
JPEG is baseline (ESPHome's decoder handles baseline only), and that an
unchanged image really does cost a 304.

Run: ./.venv/Scripts/python.exe test_content.py
"""

from __future__ import annotations

import io
import os
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

# Before importing app: this suite uses real screen names (tabletop-01), and
# with discovery on it pushed test content to the real screen of that name.
os.environ["SCREENS_DISCOVERY"] = "0"

import app as srv  # noqa: E402

DATA_TEST = Path("./data_test")
results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    results.append(bool(ok))


def sample_png(w: int = 1200, h: int = 900) -> bytes:
    """A 4:3 source, so letterboxing into 800x480 is actually exercised."""
    img = Image.new("RGB", (w, h))
    for y in range(0, h, 3):
        for x in range(0, w, 40):
            img.paste((x % 256, y % 256, 128), (x, y, min(x + 40, w), min(y + 3, h)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def parse_clip(data: bytes) -> dict | None:
    """Read a TTMJ container the way the firmware does, bounds-checked."""
    head = srv.CLIP_HEADER
    if len(data) < head.size:
        return None
    magic, version, w, h, fps, count = head.unpack_from(data)
    if magic != b"TTMJ" or version != 1:
        return None
    pos, out = head.size, []
    for _ in range(count):
        if pos + 4 > len(data):
            return None
        (n,) = srv.CLIP_RECORD.unpack_from(data, pos)
        pos += 4
        if pos + n > len(data):
            return None
        out.append(data[pos:pos + n])
        pos += n
    if pos != len(data):
        return None
    return {"w": w, "h": h, "fps": fps, "frames": out}


def main() -> int:
    srv.DATA_DIR = DATA_TEST
    if DATA_TEST.exists():
        shutil.rmtree(DATA_TEST)
    DATA_TEST.mkdir(parents=True)
    srv.app.config["TESTING"] = True
    c = srv.app.test_client()
    png = sample_png()

    print("empty state")
    check("404 before anything is uploaded", c.get("/d/tabletop-01/image").status_code == 404)

    print("\nupload")
    r = c.post(
        "/d/tabletop-01/content",
        data={"file": (io.BytesIO(png), "test.png")},
        content_type="multipart/form-data",
    )
    check("multipart upload accepted", r.status_code == 201, str(r.status_code))
    meta = r.get_json() if r.status_code == 201 else {}
    check("source dimensions recorded", meta.get("source_size") == [1200, 900])
    r = c.post(
        "/d/raw-upload/content", data=png, content_type="image/png"
    )
    check("raw-body upload accepted", r.status_code == 201, str(r.status_code))

    print("\nrender")
    r = c.get("/d/tabletop-01/image?w=800&h=480&fmt=jpeg")
    check("200 for the panel size", r.status_code == 200, str(r.status_code))
    check("content type is jpeg", r.content_type == "image/jpeg", r.content_type)
    etag = r.headers.get("ETag")
    check("ETag present", bool(etag), str(etag))
    with Image.open(io.BytesIO(r.data)) as out:
        check("exact panel dimensions", out.size == (800, 480), str(out.size))
        # ESPHome's JPEG decoder handles baseline only.
        check("baseline, not progressive", "progression" not in out.info)

    print("\ncaching")
    r2 = c.get("/d/tabletop-01/image?w=800&h=480&fmt=jpeg", headers={"If-None-Match": etag})
    check("repeat fetch is 304", r2.status_code == 304, str(r2.status_code))
    check("304 body is empty", len(r2.data) == 0, f"{len(r2.data)} bytes")

    print("\nper-screen rendering")
    r3 = c.get("/d/tabletop-01/image?w=480&h=800&fmt=png&fit=cover")
    with Image.open(io.BytesIO(r3.data)) as out:
        check("a different panel gets its own size", out.size == (480, 800), str(out.size))
    check("and its own ETag", r3.headers.get("ETag") != etag)
    check("png content type", r3.content_type == "image/png", r3.content_type)

    print("\nrotation (rot is clockwise; output is always exactly w x h)")
    shots = {}
    for rot in (0, 90, 180, 270):
        r = c.get(f"/d/tabletop-01/image?w=800&h=480&fmt=png&rot={rot}")
        size = None
        ok = r.status_code == 200
        if ok:
            with Image.open(io.BytesIO(r.data)) as out:
                size = out.size
                ok = out.size == (800, 480)
            shots[rot] = r.data
        check(f"rot={rot} renders exactly 800x480", ok, str(size))
    check("each rotation gives distinct output", len({bytes(v) for v in shots.values()}) == 4)
    check(
        "rot=360 normalises to rot=0",
        c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&rot=360").data == shots[0],
    )

    print("\nfit modes")
    for fit in ("contain", "cover", "stretch", "width", "height", "x", "y"):
        r = c.get(f"/d/tabletop-01/image?w=800&h=480&fmt=png&fit={fit}")
        size = None
        ok = r.status_code == 200
        if ok:
            with Image.open(io.BytesIO(r.data)) as out:
                size = out.size
                ok = out.size == (800, 480)
        check(f"fit={fit} renders exactly 800x480", ok, str(size))
    check(
        "x aliases width",
        c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&fit=x").data
        == c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&fit=width").data,
    )
    check(
        "y aliases height",
        c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&fit=y").data
        == c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&fit=height").data,
    )
    # 1200x900 (4:3) into 800x480 (5:3): contain must leave side bars, cover must not
    r = c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&fit=contain&bg=red")
    with Image.open(io.BytesIO(r.data)) as out:
        px = out.getpixel((2, 240))
        check("contain letterboxes in the requested bg", px == (255, 0, 0), str(px))
    r = c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&fit=cover&bg=red")
    with Image.open(io.BytesIO(r.data)) as out:
        check("cover leaves no bars", out.getpixel((2, 240)) != (255, 0, 0))

    print("\nbg=auto samples the adjacent picture edge")
    # A tall source (so bars land left/right) with a distinctly coloured left
    # and right edge, to prove each bar is sampled independently.
    edged = Image.new("RGB", (400, 900), (30, 30, 30))
    edged.paste((200, 40, 40), (0, 0, 40, 900))        # red left edge
    edged.paste((40, 40, 200), (360, 0, 400, 900))     # blue right edge
    buf = io.BytesIO(); edged.save(buf, "PNG")
    c.post(
        "/d/edges/content",
        data={"file": (io.BytesIO(buf.getvalue()), "edges.png")},
        content_type="multipart/form-data",
    )

    r = c.get("/d/edges/image?w=800&h=480&fmt=png&fit=contain&bg=auto")
    check("bg=auto renders", r.status_code == 200, str(r.status_code))
    with Image.open(io.BytesIO(r.data)) as out:
        left, right = out.getpixel((3, 240)), out.getpixel((796, 240))
        check("left bar took the left edge colour (red)",
              left[0] > 150 and left[2] < 90, str(left))
        check("right bar took the right edge colour (blue)",
              right[2] > 150 and right[0] < 90, str(right))
        check("the two bars differ, so each is sampled separately", left != right)

    r = c.get("/d/edges/image?w=800&h=480&fmt=png&fit=contain&bg=black")
    with Image.open(io.BytesIO(r.data)) as out:
        check("bg=black still gives black bars", out.getpixel((3, 240)) == (0, 0, 0),
              str(out.getpixel((3, 240))))

    r = c.get("/d/edges/image?w=800&h=480&fmt=png&fit=contain&bg=%23112233")
    with Image.open(io.BytesIO(r.data)) as out:
        check("hex bg honoured", out.getpixel((3, 240)) == (17, 34, 51),
              str(out.getpixel((3, 240))))

    r = c.get("/d/edges/image?w=800&h=480&fmt=png&fit=contain&bg=notacolour")
    check("unparseable bg falls back rather than failing", r.status_code == 200)

    r = c.get("/d/edges/image?w=800&h=480&fmt=png&fit=cover&bg=auto")
    check("bg=auto is a no-op for cover (no bars)", r.status_code == 200)
    check(
        "auto and black differ only where there are bars",
        c.get("/d/edges/image?w=800&h=480&fmt=png&fit=cover&bg=auto").data
        == c.get("/d/edges/image?w=800&h=480&fmt=png&fit=cover&bg=black").data,
    )
    c.delete("/d/edges/content")

    print("\nzoom")
    base = c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&zoom=100").data
    for z in (50, 100, 150, 400, 25):
        r = c.get(f"/d/tabletop-01/image?w=800&h=480&fmt=png&zoom={z}")
        size = None
        ok = r.status_code == 200
        if ok:
            with Image.open(io.BytesIO(r.data)) as out:
                size = out.size
                ok = out.size == (800, 480)
        check(f"zoom={z} still renders exactly 800x480", ok, str(size))
    check("zoom=100 is the default",
          c.get("/d/tabletop-01/image?w=800&h=480&fmt=png").data == base)
    check("zooming in changes the picture",
          c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&zoom=200").data != base)
    # below 100% the picture shrinks, so the bg colour must appear at the edge
    r = c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&zoom=50&fit=cover&bg=red")
    with Image.open(io.BytesIO(r.data)) as out:
        px = out.getpixel((3, 3))
        check("zoom below 100 letterboxes in the bg colour", px == (255, 0, 0), str(px))
    check("out-of-range zoom rejected",
          c.get("/d/tabletop-01/image?w=800&h=480&zoom=500").status_code == 400)
    check("zoom below the minimum rejected",
          c.get("/d/tabletop-01/image?w=800&h=480&zoom=1").status_code == 400)
    check("non-integer zoom rejected",
          c.get("/d/tabletop-01/image?w=800&h=480&zoom=big").status_code == 400)

    print("\nzoom +/- buttons send a relative step")
    c.post("/d/tabletop-01/prefs", json={"reset": 1})
    r = c.post("/d/tabletop-01/prefs", json={"zoom_by": 10})
    check("zoom_by from the default", r.get_json()["prefs"]["zoom"] == 110, str(r.get_json()))
    r = c.post("/d/tabletop-01/prefs", json={"zoom_by": -30})
    check("and it accumulates", r.get_json()["prefs"]["zoom"] == 80, str(r.get_json()))
    for _ in range(50):
        r = c.post("/d/tabletop-01/prefs", json={"zoom_by": -10})
    check("clamped at the minimum", r.get_json()["prefs"]["zoom"] == srv.ZOOM_MIN,
          str(r.get_json()))
    for _ in range(60):
        r = c.post("/d/tabletop-01/prefs", json={"zoom_by": 10})
    check("clamped at the maximum", r.get_json()["prefs"]["zoom"] == srv.ZOOM_MAX,
          str(r.get_json()))
    check("bad zoom_by rejected",
          c.post("/d/tabletop-01/prefs", json={"zoom_by": "lots"}).status_code == 400)
    r = c.post("/d/tabletop-01/prefs", json={"zoom": 100})
    check("absolute zoom still works", r.get_json()["prefs"]["zoom"] == 100)
    check("the pref overrides the query string",
          c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&zoom=300").data
          == c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&zoom=100&prefs=0").data)
    c.post("/d/tabletop-01/prefs", json={"reset": 1})

    print("\nper-device prefs")
    r = c.post("/d/tabletop-01/prefs", json={"fit": "cover", "rot": 90})
    check("prefs accepted", r.status_code == 200, str(r.status_code))
    check("prefs stored", r.get_json()["prefs"] == {"fit": "cover", "rot": 90}, str(r.get_json()))
    check(
        "prefs override the query string",
        c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&fit=contain&rot=0").data
        == c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&fit=cover&rot=90").data,
    )
    check(
        "prefs accept the x alias",
        c.post("/d/tabletop-01/prefs", json={"fit": "x"}).get_json()["prefs"]["fit"] == "width",
    )
    check("bad fit pref rejected", c.post("/d/tabletop-01/prefs", json={"fit": "sideways"}).status_code == 400)
    check("bad rot pref rejected", c.post("/d/tabletop-01/prefs", json={"rot": 45}).status_code == 400)
    check("reset clears prefs", c.post("/d/tabletop-01/prefs", json={"reset": 1}).get_json()["prefs"] == {})

    print("\npreview before apply")
    c.post("/d/tabletop-01/prefs", json={"fit": "cover", "rot": 180})
    applied = c.get("/d/tabletop-01/image?w=800&h=480&fmt=png").data
    # prefs=0 must show the asked-for selection, not the stored one
    previewed = c.get(
        "/d/tabletop-01/image?w=800&h=480&fmt=png&fit=contain&rot=0&prefs=0"
    ).data
    plain_contain = c.get(
        "/d/tabletop-01/image?w=800&h=480&fmt=png&fit=contain&rot=0&prefs=0&x=1"
    ).data
    check("prefs=0 bypasses stored prefs", previewed != applied)
    check("prefs=0 is deterministic", previewed == plain_contain)
    check(
        "without prefs=0 the stored prefs still win",
        c.get("/d/tabletop-01/image?w=800&h=480&fmt=png&fit=contain&rot=0").data == applied,
    )
    for falsy in ("0", "false", "no"):
        check(
            f"prefs={falsy} disables overrides",
            c.get(f"/d/tabletop-01/image?w=800&h=480&fmt=png&fit=contain&rot=0&prefs={falsy}").data
            == previewed,
        )
    page = c.get("/d/tabletop-01/").get_data(as_text=True)
    check("page exposes a preview element the JS can drive", 'id="preview"' in page)
    check("page gives the JS a base URL", 'data-base="/d/tabletop-01/image' in page)
    check("page has the framing form", 'id="framing"' in page)
    check("page has an apply button", ">Apply</button>" in page)
    check("page offers bg=auto", 'value="auto"' in page)
    check("page offers the x/y fit modes", 'value="width"' in page and 'value="height"' in page)
    check("page loads the script that drives the preview",
          'static/app.js' in page)
    check("and that script previews the selection rather than what is stored",
          'p.set("prefs", "0")' in c.get("/static/app.js").get_data(as_text=True))
    c.post("/d/tabletop-01/prefs", json={"reset": 1})

    print("\nvideo frames")
    info = c.get("/d/tabletop-01/video").get_json()
    check("a still falls back to the synthetic clip", info["source"] == "synthetic", str(info))
    check("which has frames to play", info["frames"] > 1, str(info))

    seen = {}
    for n in (0, 1, 2, 5):
        r = c.get(f"/d/tabletop-01/frame?n={n}&w=240&h=240&fmt=jpeg")
        ok = r.status_code == 200
        size = None
        if ok:
            with Image.open(io.BytesIO(r.data)) as out:
                size = out.size
                ok = out.size == (240, 240)
            seen[n] = r.data
        check(f"frame n={n} renders exactly 240x240", ok, str(size))
    check("consecutive frames differ", len({bytes(v) for v in seen.values()}) == len(seen))
    r = c.get("/d/tabletop-01/frame?n=0&w=240&h=240")
    check("frame index is reported", r.headers.get("X-Frame-Index") == "0",
          str(r.headers.get("X-Frame-Index")))
    check("frame count is reported", int(r.headers.get("X-Frame-Count", 0)) > 1)
    check("frames are never cached", r.headers.get("Cache-Control") == "no-store",
          str(r.headers.get("Cache-Control")))
    check("n wraps rather than 404ing",
          c.get("/d/tabletop-01/frame?n=99999&w=240&h=240").status_code == 200)
    check("negative n is accepted too",
          c.get("/d/tabletop-01/frame?n=-1&w=240&h=240").status_code == 200)
    check("bad n rejected", c.get("/d/tabletop-01/frame?n=soon").status_code == 400)
    check("bad size rejected", c.get("/d/tabletop-01/frame?w=99999").status_code == 400)

    print("\nan uploaded GIF becomes the clip")
    # Asymmetric on purpose: a solid square is identical after a 90 degree
    # rotation, so a uniform frame could not detect whether rot was applied.
    gif = []
    for i in range(8):
        f = Image.new("RGB", (200, 200), (i * 30 % 256, 90, 200 - i * 22))
        ImageDraw.Draw(f).rectangle([0, 0, 199, 40], fill=(255, 255, 255))
        gif.append(f)
    buf = io.BytesIO()
    gif[0].save(buf, "GIF", save_all=True, append_images=gif[1:], duration=80, loop=0)
    c.post(
        "/d/vid/content",
        data={"file": (io.BytesIO(buf.getvalue()), "clip.gif")},
        content_type="multipart/form-data",
    )
    info = c.get("/d/vid/video").get_json()
    check("source is the upload now", info["source"] == "uploaded", str(info))
    check("with the right frame count", info["frames"] == 8, str(info))
    a = c.get("/d/vid/frame?n=0&w=240&h=240&fmt=png").data
    b = c.get("/d/vid/frame?n=4&w=240&h=240&fmt=png").data
    check("different frames of the GIF differ", a != b)
    check("and it wraps at the GIF length",
          c.get("/d/vid/frame?n=8&w=240&h=240&fmt=png").data == a)

    print("\nframes obey the screen's framing prefs")
    c.post("/d/vid/prefs", json={"rot": 90})
    check("rotation applied to frames",
          c.get("/d/vid/frame?n=0&w=240&h=240&fmt=png").data != a)
    c.post("/d/vid/prefs", json={"reset": 1})

    print("\nclip.mjpeg (whole clip, resampled to fps, for playback from memory)")
    # 8 frames x 80 ms = 640 ms of source.
    r = c.get("/d/vid/clip.mjpeg?w=240&h=240&fps=10")
    check("clip serves", r.status_code == 200, str(r.status_code))
    clip = parse_clip(r.data)
    check("TTMJ header parses", clip is not None)
    if clip is not None:
        check("header carries the panel size and fps",
              (clip["w"], clip["h"], clip["fps"]) == (240, 240, 10), str(clip["w"]))
        check("10 fps over 640 ms is 6 frames", len(clip["frames"]) == 6,
              str(len(clip["frames"])))
        check("X-Frame-Count agrees", r.headers.get("X-Frame-Count") == "6",
              str(r.headers.get("X-Frame-Count")))
        ok = True
        for jpeg in clip["frames"]:
            with Image.open(io.BytesIO(jpeg)) as out:
                ok = ok and out.format == "JPEG" and out.size == (240, 240) \
                    and "progression" not in out.info
        check("every frame is a baseline 240x240 JPEG", ok)
    fast = parse_clip(c.get("/d/vid/clip.mjpeg?w=240&h=240&fps=20").data)
    check("20 fps over 640 ms is 13 frames", fast is not None and len(fast["frames"]) == 13,
          str(fast and len(fast["frames"])))
    if fast is not None:
        # t=0,50 ms -> source 0; t=100 ms -> source 1. Repeats reuse the bytes.
        check("resampling repeats a frame that is still on screen",
              fast["frames"][0] == fast["frames"][1] != fast["frames"][2])
    etag = r.headers.get("ETag")
    r2 = c.get("/d/vid/clip.mjpeg?w=240&h=240&fps=10", headers={"If-None-Match": etag})
    check("unchanged clip is an empty 304", r2.status_code == 304 and not r2.data,
          str(r2.status_code))
    check("a different fps is a different clip",
          c.get("/d/vid/clip.mjpeg?w=240&h=240&fps=20").headers.get("ETag") != etag)
    one = len(clip["frames"][0]) if clip else 0
    budget = srv.CLIP_HEADER.size + 2 * (srv.CLIP_RECORD.size + one) + 8
    r = c.get(f"/d/vid/clip.mjpeg?w=240&h=240&fps=10&max={budget}")
    small = parse_clip(r.data)
    check("max truncates to whole frames", small is not None and 1 <= len(small["frames"]) < 6
          and len(r.data) <= budget, f"{len(r.data)} bytes")
    check("a budget below one frame is 413",
          c.get("/d/vid/clip.mjpeg?w=240&h=240&max=64").status_code == 413)
    check("bad fps rejected", c.get("/d/vid/clip.mjpeg?fps=0").status_code == 400)
    # The quality control on the device page has to reach clips, not just
    # /image -- it is folded into the content token either way, so if it did
    # not, changing it would force a re-download of identical bytes.
    plain = c.get("/d/vid/clip.mjpeg?w=240&h=240&fps=10&q=80")
    c.post("/d/vid/prefs", json={"q": 20})
    lowq = c.get("/d/vid/clip.mjpeg?w=240&h=240&fps=10&q=80")
    check("a stored quality pref overrides the screen's own q",
          len(lowq.data) < len(plain.data),
          f"{len(lowq.data)} vs {len(plain.data)} bytes")
    check("and it is a different clip to the screen", lowq.headers["ETag"] != plain.headers["ETag"])
    c.post("/d/vid/prefs", json={"reset": 1})
    check("clearing it restores the original bytes",
          c.get("/d/vid/clip.mjpeg?w=240&h=240&fps=10&q=80").data == plain.data)
    still = parse_clip(c.get("/d/tabletop-01/clip.mjpeg?w=64&h=64&fps=5").data)
    check("a still is a one-frame clip", still is not None and len(still["frames"]) == 1,
          str(still and len(still["frames"])))
    if still is not None:
        with Image.open(io.BytesIO(still["frames"][0])) as out:
            check("which is the still at the panel size", out.size == (64, 64), str(out.size))
    empty = parse_clip(c.get("/d/nobody-here/clip.mjpeg?w=64&h=64&fps=5").data)
    check("no content at all gets the synthetic test clip",
          empty is not None and len(empty["frames"]) > 1)
    c.delete("/d/vid/content")

    print("\nrejections")
    check("unknown format", c.get("/d/tabletop-01/image?fmt=tiff").status_code == 400)
    check("bad fit", c.get("/d/tabletop-01/image?fit=sideways").status_code == 400)
    check("bad rot", c.get("/d/tabletop-01/image?rot=45").status_code == 400)
    check("non-integer rot", c.get("/d/tabletop-01/image?rot=sideways").status_code == 400)
    check("non-integer size", c.get("/d/tabletop-01/image?w=wide").status_code == 400)
    check("oversized dimensions", c.get("/d/tabletop-01/image?w=99999").status_code == 400)
    check("invalid device name", c.get("/d/Bad_Name!/image").status_code == 400)
    check(
        "non-image upload rejected at upload time",
        c.post("/d/junk/content", data=b"not an image", content_type="application/octet-stream").status_code
        == 400,
    )
    check("empty upload rejected", c.post("/d/junk/content", data=b"").status_code == 400)
    check(
        "qoi reports unavailable rather than crashing",
        c.get("/d/tabletop-01/image?fmt=qoi").status_code in (200, 503),
    )

    print("\nrgb565 (raw frames for the SD path)")
    r = c.get("/d/tabletop-01/image?w=800&h=480&fmt=rgb565")
    check("rgb565 serves", r.status_code == 200, str(r.status_code))
    # Size is the whole point: fixed and predictable, so the device can read a
    # frame into a fixed buffer with no header parsing and no allocation.
    check("rgb565 is exactly w*h*2", len(r.data) == 800 * 480 * 2, str(len(r.data)))
    check(
        "rgb565 is served as raw bytes",
        r.headers["Content-Type"] == "application/octet-stream",
        r.headers["Content-Type"],
    )
    # The SD still path sends If-None-Match with the ETag it stored alongside
    # the file; an unchanged picture must not re-send 768 KB.
    etag = r.headers.get("ETag")
    check("rgb565 carries an ETag", bool(etag), str(etag))
    r2 = c.get("/d/tabletop-01/image?w=800&h=480&fmt=rgb565", headers={"If-None-Match": etag})
    check("rgb565 repeat with ETag is an empty 304",
          r2.status_code == 304 and not r2.data, str(r2.status_code))
    r = c.get("/d/tabletop-01/image?w=240&h=240&fmt=rgb565")
    check("rgb565 honours size", len(r.data) == 240 * 240 * 2, str(len(r.data)))
    check(
        "rgb565 rejects a misspelled format",
        c.get("/d/tabletop-01/image?w=8&h=8&fmt=rgb5650").status_code == 400,
    )
    # Solid red through the whole pipeline must come out as big-endian F800.
    # This is the check that would have caught the R->B swap during bring-up.
    solid = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(solid, "PNG")
    c.post(
        "/d/endian/content",
        data={"file": (io.BytesIO(solid.getvalue()), "red.png")},
        content_type="multipart/form-data",
    )
    r = c.get("/d/endian/image?w=4&h=4&fmt=rgb565&fit=cover")
    check("rgb565 red is big-endian F800", r.data[:2] == bytes((0xF8, 0x00)), r.data[:2].hex())

    print("\nscreen state (pushed by the screen)")
    items = c.get("/d/tabletop-01/items").get_json()
    # The cache report names the SOURCE first, then the variant: a screen
    # holds one entry per framing of a picture.
    item_id = items["current_src"]
    variant_id = items["current"]
    report = {
        "budget": 5000000, "used": 1300000, "max": 4000000,
        "psram_free": 2100000, "psram_total": 8000000, "heap_free": 120000,
        "shown": f"{item_id}.{variant_id}-f15-q80",
        "mem": [[f"{item_id}.{variant_id}-f15-q80", 900000, 1], ["file:intro.mjpeg", 400000, 240]],
        "card": [[f"{item_id}.{variant_id}-f15-q80", 900000],
                 [f"{item_id}.{variant_id}.abcd1234-f20-q80", 1200000]],
    }
    r = c.post("/d/tabletop-01/state", json=report)
    check("state accepted", r.status_code == 204, str(r.status_code))
    check("non-JSON state rejected",
          c.post("/d/tabletop-01/state", data="x", content_type="text/plain").status_code == 400)
    st = c.get("/d/tabletop-01/state").get_json()
    check("state stored with a timestamp", st.get("report") == report and st.get("at"))
    by_key = {e["key"]: e for e in st["entries"]}
    shown = by_key.get(f"{item_id}.{variant_id}-f15-q80", {})
    check("an item in memory and on card is one row",
          shown.get("mem") == 900000 and shown.get("card") == 900000 and shown.get("shown"),
          str(shown))
    check("keys parse back to item, fps and framing",
          shown.get("item_id") == item_id and shown.get("fps") == 15
          and by_key[f"{item_id}.{variant_id}.abcd1234-f20-q80"]["framed"], str(shown))
    check("hand-copied files are recognised", by_key["file:intro.mjpeg"]["file"] == "intro.mjpeg")
    check("the row on screen sorts first", st["entries"][0]["shown"])
    page = c.get("/d/tabletop-01/").get_data(as_text=True)
    check("device page shows memory in use", "Memory 1.2 MB of 4.8 MB" in page)
    check("and the cached items", "intro.mjpeg" in page and "on screen" in page)
    check("library row carries a cached badge", 'class="cached"' in page)
    # The overview says it as a bar and a line, not a sentence.
    idx = c.get("/").get_data(as_text=True)
    check("index card shows memory too", "1.2 MB / 4.8 MB" in idx)

    print("\npages")
    check("device page", c.get("/d/tabletop-01/").status_code == 200)
    check("index", c.get("/").status_code == 200)
    check("meta", c.get("/d/tabletop-01/meta").status_code == 200)
    check("healthz", c.get("/healthz").get_json().get("ok") is True)

    print("\ndelete")
    check("delete", c.delete("/d/tabletop-01/content").status_code == 204)
    check("404 after delete", c.get("/d/tabletop-01/image").status_code == 404)

    shutil.rmtree(DATA_TEST, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
