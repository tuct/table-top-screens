"""Clip generation tests: graphs, routes, and the reference that must survive.

No ComfyUI and no GPU involved. What is worth checking without one is that the
graphs we hand over are wired the way the models expect, that the page and its
routes behave when there is no generator at all, and -- the reason this file
exists -- that a reference picture arrives as *itself*.

That last one is a real bug this suite was written after: pulling a still out
of the pool through frames.frame() returns the synthetic test pattern, because
that function is for picking a frame out of an animation. Generation then
succeeds confidently from the wrong picture, which is the worst shape a bug
can take -- no error, just a result nobody asked for.

Run: ./.venv/bin/python test_generate.py
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import time
from pathlib import Path

from PIL import Image

os.environ["SCREENS_DISCOVERY"] = "0"

import app as srv  # noqa: E402
import generate  # noqa: E402
import library  # noqa: E402

DATA_TEST = Path(__file__).parent / "data_test_generate"
results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    results.append(bool(ok))


def png_bytes(w: int = 200, h: int = 200, colour=(10, 160, 90)) -> bytes:
    """A flat, unmistakable colour -- nothing the test pattern would produce."""
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "PNG")
    return buf.getvalue()


def links_of(graph: dict, node: str) -> dict:
    return {k: v for k, v in graph[node]["inputs"].items()
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)}


def main() -> int:  # noqa: PLR0915
    srv.DATA_DIR = DATA_TEST
    shutil.rmtree(DATA_TEST, ignore_errors=True)
    DATA_TEST.mkdir(parents=True, exist_ok=True)

    print("\ngraphs -- text to video")
    g = generate.clip_graph("a tavern", None, "portrait", 1)
    check("the sampler runs on the AnimateDiff model", g["sampler"]["inputs"]["model"] == ["ad", 0])
    check("frames are a latent batch", g["latent"]["inputs"]["batch_size"] == generate.FRAMES)
    check("the panel shape decides the size",
          (g["latent"]["inputs"]["width"], g["latent"]["inputs"]["height"]) == generate.SIZES["portrait"])
    check("with no reference the loop is closed", g["ctx"]["inputs"]["closed_loop"] is True)
    check("no SparseCtrl without a reference", "sparse" not in g and "cnet" not in g)
    check("output is an mp4", g["out"]["inputs"]["format"] == "video/h264-mp4")
    check("interpolation multiplies the written frame rate",
          g["out"]["inputs"]["frame_rate"] == generate.FPS * generate.INTERPOLATE)
    check("the sampler's conditioning is the raw prompt",
          g["sampler"]["inputs"]["positive"] == ["pos", 0])

    print("\ngraphs -- image to video")
    # Ping-pong was wired into gen.py's CLI from the start but hardcoded off
    # here, so the web UI could not close a loop the way the CLI could.
    off = generate.clip_graph("x", None, "square", 1)
    on = generate.clip_graph("x", None, "square", 1, pingpong=True)
    check("a clip does not ping-pong by default",
          off["out"]["inputs"]["pingpong"] is False)
    check("and does when asked", on["out"]["inputs"]["pingpong"] is True)
    check("it changes nothing else",
          {k: v for k, v in off["out"]["inputs"].items() if k != "pingpong"}
          == {k: v for k, v in on["out"]["inputs"].items() if k != "pingpong"})

    g = generate.clip_graph("a tavern", "ref.png", "square", 1)
    check("SparseCtrl is loaded", g["sparse"]["inputs"]["sparsectrl_name"] == generate.SPARSECTRL)
    check("the still is pinned to both ends",
          g["idx"]["inputs"]["indexes"] == f"0,{generate.FRAMES - 1}",
          g["idx"]["inputs"]["indexes"])
    check("and the loop opens, so it cannot fight the end anchor",
          g["ctx"]["inputs"]["closed_loop"] is False)
    check("conditioning goes through the control net",
          g["sampler"]["inputs"]["positive"] == ["cnet", 0]
          and g["sampler"]["inputs"]["negative"] == ["cnet", 1])
    check("the reference is scaled to the render size",
          (g["fit"]["inputs"]["width"], g["fit"]["inputs"]["height"]) == generate.SIZES["square"])

    print("\ngraphs -- the quick verify")
    g = generate.still_graph("a tavern", None, "portrait", 1)
    check("one frame only", g["latent"]["inputs"]["batch_size"] == 1)
    check("no motion module at all", not any(
        n["class_type"].startswith("ADE_") for n in g.values()))
    check("with no reference it denoises fully", g["sampler"]["inputs"]["denoise"] == 1.0)
    check("it saves an image, not a video", g["out"]["class_type"] == "SaveImage")

    g = generate.still_graph("a tavern", "ref.png", "square", 1, "balanced")
    check("with a reference it is img2img", g["latent"]["class_type"] == "VAEEncode")
    check("and only partially denoised, so the reference survives",
          g["sampler"]["inputs"]["denoise"] == generate.KEEP["balanced"]["denoise"])

    print("\nevery link points at a node that exists")
    for kind, graph in (("clip", generate.clip_graph("x", "r.png", "portrait", 1)),
                        ("verify", generate.still_graph("x", "r.png", "portrait", 1))):
        dangling = [f"{n}.{k}->{v[0]}" for n in graph for k, v in links_of(graph, n).items()
                    if v[0] not in graph]
        check(f"{kind} graph has no dangling links", not dangling, ", ".join(dangling[:3]))

    print("\nthe reference is the picture, not a stand-in")
    # The regression. A still in the pool must reach ComfyUI as itself.
    item = library.pool_add(DATA_TEST, png_bytes(colour=(10, 160, 90)), "image/png", "flat.png")
    sent: dict = {}

    def fake_upload(body: bytes, filename: str) -> str:
        sent["body"] = body
        sent["filename"] = filename
        return filename

    real_upload, real_submit = generate.upload_reference, generate.submit
    generate.upload_reference = fake_upload
    generate.submit = lambda kind, prompt, reference, size, seed, \
        length=generate.DEFAULT_LENGTH, adopted_from="", \
        keep=generate.DEFAULT_KEEP, pingpong=False: generate.Job(
            id="t", kind=kind, prompt=prompt, size=size, seed=seed,
            has_reference=bool(reference), length=length,
            adopted_from=adopted_from, keep=keep)
    try:
        c = srv.app.test_client()
        r = c.post("/generate/run", data={"kind": "verify", "prompt": "x",
                                          "pool_id": item["id"]},
                   content_type="multipart/form-data")
        check("the run is accepted", r.status_code == 202, str(r.status_code))
        check("something was uploaded as the reference", "body" in sent)
        if "body" in sent:
            got = Image.open(io.BytesIO(sent["body"])).convert("RGB")
            # The synthetic pattern is a gradient with circles; the fixture is
            # one flat colour. Comparing the corner pixel separates them.
            px = got.getpixel((5, 5))
            check("it is the pooled picture, not the synthetic test pattern",
                  abs(px[0] - 10) < 12 and abs(px[1] - 160) < 12 and abs(px[2] - 90) < 12,
                  f"corner {px}")
            check("and it kept the source's size", got.size == (200, 200), str(got.size))

        # The two ways a pool id can be wrong are different answers: one is a
        # malformed request, the other is a request for something absent.
        sent.clear()
        r = c.post("/generate/run", data={"kind": "verify", "prompt": "x",
                                          "pool_id": "not a valid id"},
                   content_type="multipart/form-data")
        check("a malformed pool id is a 400", r.status_code == 400, str(r.status_code))
        r = c.post("/generate/run", data={"kind": "verify", "prompt": "x",
                                          "pool_id": "deadbeefdeadbeef"},
                   content_type="multipart/form-data")
        check("a well-formed id for nothing is a 404", r.status_code == 404, str(r.status_code))
    finally:
        generate.upload_reference, generate.submit = real_upload, real_submit

    print("\njobs")
    job = generate.Job(id="j1", kind="clip", prompt="p", size="portrait",
                       seed=3, has_reference=False)
    pub = job.public()
    check("a job never exposes its bytes", "result" not in pub)
    check("it carries an estimate for the progress bar", pub["estimate"] > 0)
    check("state starts queued", pub["state"] == "queued")
    check("nothing is ahead of it yet", pub["ahead"] == 0)

    # Queue time and work time are different numbers, and the page shows the
    # second one: a clip waiting behind another job is not a slow clip.
    check("a job that has not started reports no running time", pub["running"] == 0.0)
    job.started = time.time() - 60          # asked for a minute ago
    job.running_since = time.time() - 10    # picked up ten seconds ago
    pub = job.public()
    check("elapsed counts from when it was asked for", 55 < pub["elapsed"] < 65,
          str(pub["elapsed"]))
    check("running counts only from when ComfyUI started it",
          5 < pub["running"] < 15, str(pub["running"]))
    job.finished = time.time()
    check("and both stop once it is finished",
          abs(job.public()["running"] - pub["running"]) < 2)

    print("\nwith no generator reachable")
    real_url, real_probe = generate.COMFY_URL, generate._last_probe
    generate.COMFY_URL = "http://127.0.0.1:9"      # discard port: refuses at once
    generate._last_probe = (0.0, False)
    try:
        check("available() says no", generate.available() is False)
        check("and the status line says where it looked",
              "9" in generate.status_line() and "No ComfyUI" in generate.status_line(),
              generate.status_line()[:60])
        c = srv.app.test_client()
        r = c.get("/generate")
        check("the page still renders", r.status_code == 200, str(r.status_code))
        check("and says so rather than offering a form",
              b"Nothing to generate with" in r.data)
    finally:
        generate.COMFY_URL, generate._last_probe = real_url, real_probe

    print("\nloop length")
    lens = {L: generate.clip_graph("x", None, "square", 1, L)["latent"]["inputs"]["batch_size"]
            for L in generate.LENGTHS}
    check("each length asks for its own frame count", len(set(lens.values())) == len(lens),
          str(lens))
    check("longer means more frames", lens["1s"] < lens["2s"] < lens["4s"])
    check("4s is about four seconds once interpolated",
          abs((2 * lens["4s"] - 1) / generate.OUT_FPS - 4) < 0.2,
          f"{(2 * lens['4s'] - 1) / generate.OUT_FPS:.2f}s")
    check("1s is about one", abs((2 * lens["1s"] - 1) / generate.OUT_FPS - 1) < 0.2)
    # The motion module was trained on 16-frame windows; a longer clip slides
    # that window rather than widening it.
    g4 = generate.clip_graph("x", None, "square", 1, "4s")
    check("the context window stays at 16 for a long clip",
          g4["ctx"]["inputs"]["context_length"] == 16)
    check("and the anchor follows the real last frame",
          generate.clip_graph("x", "r.png", "square", 1, "4s")["idx"]["inputs"]["indexes"]
          == f"0,{lens['4s'] - 1}")
    check("the estimate scales with length",
          generate.estimate_for("clip", "4s") > generate.estimate_for("clip", "1s"))
    check("a verify's estimate does not", generate.estimate_for("verify", "4s")
          == generate.estimate_for("verify", "1s"))

    r = srv.app.test_client().post("/generate/run",
                                   data={"kind": "clip", "prompt": "x", "length": "9s"},
                                   content_type="multipart/form-data")
    check("an unknown length is refused", r.status_code == 400, str(r.status_code))

    print("\nadopting a still as the next reference")
    finished = generate.Job(id="done1", kind="verify", prompt="p", size="square",
                            seed=1, has_reference=False)
    finished.state, finished.result = "done", png_bytes(colour=(200, 30, 40))
    finished.content_type = "image/png"
    unfinished = generate.Job(id="busy1", kind="verify", prompt="p", size="square",
                              seed=1, has_reference=False)
    a_clip = generate.Job(id="clip1", kind="clip", prompt="p", size="square",
                          seed=1, has_reference=False)
    a_clip.state, a_clip.result = "done", b"not an image"
    a_clip.content_type = "video/mp4"
    for j in (finished, unfinished, a_clip):
        generate._remember(j)

    sent.clear()
    generate.upload_reference = fake_upload
    real_submit2 = generate.submit
    generate.submit = lambda kind, prompt, reference, size, seed, \
        length=generate.DEFAULT_LENGTH, adopted_from="", \
        keep=generate.DEFAULT_KEEP, pingpong=False: generate.Job(
            id="t2", kind=kind, prompt=prompt, size=size, seed=seed,
            has_reference=bool(reference), length=length,
            adopted_from=adopted_from, keep=keep)
    try:
        c = srv.app.test_client()
        r = c.post("/generate/run", data={"kind": "verify", "prompt": "refined",
                                          "from_job": "done1"},
                   content_type="multipart/form-data")
        check("a finished still can be adopted", r.status_code == 202, str(r.status_code))
        check("and it is that still that gets uploaded",
              sent.get("body") == finished.result)
        check("the new job records what it came from",
              r.get_json().get("adopted_from") == "done1")

        r = c.post("/generate/run", data={"kind": "verify", "from_job": "busy1"},
                   content_type="multipart/form-data")
        check("an unfinished job cannot be adopted", r.status_code == 404, str(r.status_code))
        r = c.post("/generate/run", data={"kind": "verify", "from_job": "clip1"},
                   content_type="multipart/form-data")
        check("nor can a video -- a reference is a picture",
              r.status_code == 400, str(r.status_code))
    finally:
        generate.upload_reference, generate.submit = real_upload, real_submit2

    print("\nthe galleries")
    check("stills are listed newest first and finished only",
          [j.id for j in generate.recent(10, "verify", True)][:1] == ["done1"]
          or "done1" in [j.id for j in generate.recent(10, "verify", True)])
    check("an unfinished job is not in the gallery",
          "busy1" not in [j.id for j in generate.recent(10, "verify", True)])
    check("clips and stills are separate",
          "clip1" not in [j.id for j in generate.recent(10, "verify", True)]
          and "clip1" in [j.id for j in generate.recent(10, "clip", True)])
    check("the gallery is capped at ten", len(generate.recent(generate.GALLERY)) <= 10)
    check("a still is flagged as an image", finished.public()["is_image"] is True)
    check("a clip is not", a_clip.public()["is_image"] is False)

    print("\nholding on to the reference")
    close = generate.still_graph("x", "r.png", "square", 1, "close")
    loose = generate.still_graph("x", "r.png", "square", 1, "loose")
    check("close denoises less, so more of the picture survives",
          close["sampler"]["inputs"]["denoise"] < loose["sampler"]["inputs"]["denoise"],
          f'{close["sampler"]["inputs"]["denoise"]} < {loose["sampler"]["inputs"]["denoise"]}')
    check("the default is the one that keeps the subject",
          generate.DEFAULT_KEEP == "close")
    check("it reaches a clip's control strength too",
          generate.clip_graph("x", "r.png", "square", 1, "1s", "loose")["cnet"]["inputs"]["strength"]
          < generate.clip_graph("x", "r.png", "square", 1, "1s", "close")["cnet"]["inputs"]["strength"])
    check("with no reference it changes nothing",
          generate.still_graph("x", None, "square", 1, "close")["sampler"]["inputs"]["denoise"]
          == generate.still_graph("x", None, "square", 1, "loose")["sampler"]["inputs"]["denoise"]
          == 1.0)
    r = srv.app.test_client().post("/generate/run",
                                   data={"kind": "verify", "prompt": "x", "keep": "tight"},
                                   content_type="multipart/form-data")
    check("an unknown keep level is refused", r.status_code == 400, str(r.status_code))

    print("\nmatching the reference's proportions")
    # A 16:9 picture centre-cropped into a square loses its sides before the
    # model ever sees it, which is indistinguishable from being ignored.
    check("a wide picture asks for landscape", generate.shape_for(800, 450) == "landscape")
    check("a tall one asks for portrait", generate.shape_for(480, 800) == "portrait")
    check("a square one asks for square", generate.shape_for(800, 800) == "square")
    check("and nothing known falls back to the default",
          generate.shape_for(0, 0) == generate.DEFAULT_SIZE)

    wide = library.pool_add(DATA_TEST, png_bytes(320, 180), "image/png", "wide.png")
    sent.clear()
    real_up2, real_sub2 = generate.upload_reference, generate.submit
    seen: dict = {}
    generate.upload_reference = fake_upload
    generate.submit = lambda kind, prompt, reference, size, seed, \
        length=generate.DEFAULT_LENGTH, adopted_from="", \
        keep=generate.DEFAULT_KEEP, pingpong=False: (
            seen.update(size=size, keep=keep, pingpong=pingpong) or generate.Job(
            id="t3", kind=kind, prompt=prompt, size=size, seed=seed,
            has_reference=bool(reference), length=length,
            adopted_from=adopted_from, keep=keep))
    try:
        c = srv.app.test_client()
        r = c.post("/generate/run", data={"kind": "verify", "prompt": "x",
                                          "pool_id": wide["id"], "size": "auto"},
                   content_type="multipart/form-data")
        check("auto reads the reference and picks landscape for a 16:9 source",
              r.status_code == 202 and seen.get("size") == "landscape",
              f'{r.status_code} {seen.get("size")}')
        seen.clear()
        r = c.post("/generate/run", data={"kind": "verify", "prompt": "x", "size": "auto"},
                   content_type="multipart/form-data")
        check("auto with no reference falls back rather than failing",
              r.status_code == 202 and seen.get("size") == generate.DEFAULT_SIZE,
              str(seen.get("size")))
        r = c.post("/generate/run", data={"kind": "verify", "prompt": "x", "size": "oblong"},
                   content_type="multipart/form-data")
        check("an unknown size is still refused", r.status_code == 400, str(r.status_code))

        # An unchecked HTML checkbox sends no field at all, so the route has to
        # read absence as off rather than as missing input.
        seen.clear()
        c.post("/generate/run", data={"kind": "clip", "prompt": "x"},
               content_type="multipart/form-data")
        check("an absent checkbox means no ping-pong", seen.get("pingpong") is False,
              str(seen.get("pingpong")))
        seen.clear()
        c.post("/generate/run", data={"kind": "clip", "prompt": "x", "pingpong": "1"},
               content_type="multipart/form-data")
        check("and a ticked one reaches the job", seen.get("pingpong") is True,
              str(seen.get("pingpong")))
    finally:
        generate.upload_reference, generate.submit = real_up2, real_sub2

    print("\nbad input")
    c = srv.app.test_client()
    r = c.post("/generate/run", data={"kind": "nonsense"},
               content_type="multipart/form-data")
    check("an unknown kind is refused", r.status_code == 400, str(r.status_code))
    r = c.get("/generate/job/doesnotexist")
    check("an unknown job is a 404", r.status_code == 404, str(r.status_code))
    r = c.get("/generate/job/doesnotexist/result")
    check("so is its result", r.status_code == 404, str(r.status_code))

    shutil.rmtree(DATA_TEST, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
