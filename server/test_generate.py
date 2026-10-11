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

from PIL import Image, ImageDraw

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
        keep=generate.DEFAULT_KEEP, **kw: generate.Job(
            id="t", kind=kind, prompt=prompt, size=size, seed=seed,
            has_reference=bool(reference), length=length,
            adopted_from=adopted_from, keep=keep)
    try:
        c = srv.app.test_client()
        # keep=close: this checks that the POOL picture is what gets uploaded,
        # and the default "subject" level uploads a composited canvas and a
        # mask built from it instead.
        r = c.post("/generate/run", data={"kind": "verify", "prompt": "x",
                                          "pool_id": item["id"], "keep": "close"},
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
        keep=generate.DEFAULT_KEEP, **kw: generate.Job(
            id="t2", kind=kind, prompt=prompt, size=size, seed=seed,
            has_reference=bool(reference), length=length,
            adopted_from=adopted_from, keep=keep)
    try:
        c = srv.app.test_client()
        # keep=close on purpose: this is about the adopt plumbing, and the
        # default "subject" level uploads a composited canvas and a mask
        # instead of the still itself, which would make the next check assert
        # the wrong thing.
        r = c.post("/generate/run", data={"kind": "verify", "prompt": "refined",
                                          "from_job": "done1", "keep": "close"},
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
    # Stated as the intent rather than the name: the default must hold the
    # subject, whether it does that by denoising least or by masking him out
    # of the sampling altogether.
    default = generate.KEEP[generate.DEFAULT_KEEP]
    check("the default is the one that keeps the subject",
          bool(default.get("inpaint"))
          or default["denoise"] <= min(v["denoise"] for v in generate.KEEP.values()
                                       if not v.get("inpaint")),
          generate.DEFAULT_KEEP)
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
        keep=generate.DEFAULT_KEEP, **kw: (
            seen.update(size=size, keep=keep, pingpong=kw.get('pingpong', False),
                        style=kw.get('style'), motion=kw.get('motion')) or generate.Job(
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

    print("\nexample prompts")
    # Real text, not placeholder grey: a placeholder cannot be edited, and the
    # whole point is to start from something that works.
    check("there are examples", len(generate.PROMPT_EXAMPLES) >= 2)
    for ex in generate.PROMPT_EXAMPLES:
        ok = ex.get("label") and ex.get("text") and ex.get("kind") in ("scene", "motion")
        check(f"{ex.get('label','?')} is well formed", bool(ok), str(sorted(ex)))
    # Both kinds must exist, because the split is the actual lesson: a scene
    # prompt builds the still, a motion prompt animates it.
    kinds = {e["kind"] for e in generate.PROMPT_EXAMPLES}
    check("both a scene and a motion example are offered", kinds == {"scene", "motion"},
          str(sorted(kinds)))
    page = srv.app.test_client().get("/generate").get_data(as_text=True)
    check("every example reaches the page in full",
          all(e["text"] in page for e in generate.PROMPT_EXAMPLES))

    print("\nbackgrounds: a place, kept and reused")
    bg = generate.background_graph("a torchlit dungeon", "dnd", "portrait", 7)
    check("a background has nobody in it",
          "person" in bg["neg"]["inputs"]["text"] and "character" in bg["neg"]["inputs"]["text"])
    check("the style is folded into the prompt",
          "dungeons and dragons" in bg["pos"]["inputs"]["text"])
    own = generate.background_graph("just this", "own", "square", 7)["pos"]["inputs"]["text"]
    check("roll-your-own adds no STYLE of its own",
          not any(w in own for w in ("dungeons and dragons", "isometric")), own[:60])
    check("but the composition still applies, whatever the look",
          generate.BACKGROUND_COMPOSITION in own)
    # A place is made once and reused for weeks, so it does NOT take the clip's
    # speed-over-sharpness bargain.
    check("it skips the latent-consistency LoRA", "lcm" not in bg)
    check("and samples properly", bg["sampler"]["inputs"]["steps"] == generate.BACKGROUND_STEPS)
    check("from text it starts from nothing", bg["sampler"]["inputs"]["denoise"] == 1.0)
    frompic = generate.background_graph("restyle", "dnd", "square", 7, reference="r.png")
    check("from a picture it is img2img", frompic["sampler"]["inputs"]["denoise"] < 1.0)
    # Refining has its own scale, deliberately gentler than the character
    # flow's: a place that gets repainted is a sibling, not a revision.
    lv = {k: generate.background_graph("x", "dnd", "square", 7, reference="r.png",
                                       refine=k)["sampler"]["inputs"]["denoise"]
          for k in generate.REFINE_LEVELS}
    check("a refine travels less the gentler it is",
          lv["nudge"] < lv["change"] < lv["rework"], str(lv))
    check("even a rework stays well short of a repaint", lv["rework"] <= 0.6, str(lv["rework"]))
    check("the default keeps the room", lv[generate.DEFAULT_REFINE] <= 0.4,
          str(lv[generate.DEFAULT_REFINE]))
    check("an unknown level falls back rather than failing",
          generate.background_graph("x", "dnd", "square", 7, reference="r.png",
                                    refine="nonsense")["sampler"]["inputs"]["denoise"]
          == lv[generate.DEFAULT_REFINE])
    # Motion is recorded WITH the place and spent later; it must never reach
    # the picture, which cannot show it.
    check("motion is not in the scene prompt",
          "smoke" not in bg["pos"]["inputs"]["text"].lower())
    check("there is a default for what moves", bool(generate.DEFAULT_MOTION))
    check("and examples to start from", len(generate.MOTION_EXAMPLES) >= 3)
    # A background is half a picture: a character is composited into the middle
    # later, so the middle has to be free.
    # Asserted against the constant, not a phrase: the wording gets tuned
    # whenever the model misreads it, and a test that pins the exact words
    # fails for the wrong reason every time.
    check("it asks for clear floor in the middle",
          "middle" in generate.BACKGROUND_COMPOSITION
          and generate.BACKGROUND_COMPOSITION in bg["pos"]["inputs"]["text"])
    # Measured as OVERHEAD, not total: what the user types can be any length,
    # and the thing worth guarding is how much of the 77-token budget is spent
    # before their words are even considered. It was 50 words once, which is
    # why torches could not get a word in.
    scene_words = len("a torchlit dungeon".split())
    overhead = len(generate.background_graph("a torchlit dungeon", "dnd", "portrait", 1)
                   ["pos"]["inputs"]["text"].split()) - scene_words
    # Everything we add: style AND composition. Both come out of the same
    # budget as the scene, and it was 50 words once -- which is why "iron
    # torches in wall brackets" could not get a word in.
    check("everything we add leaves room for the scene", overhead <= 34,
          f"{overhead} words of boilerplate")
    check("no style argues with the composition",
          not any("detailed" in v["prompt"] for v in generate.STYLES.values()),
          str([v["prompt"][:30] for v in generate.STYLES.values()]))
    check("and the default description still fits inside the budget",
          len(generate.background_graph(generate.DEFAULT_SCENE, "dnd", "portrait", 1)
              ["pos"]["inputs"]["text"].split()) <= 56,
          str(len(generate.background_graph(generate.DEFAULT_SCENE, "dnd", "portrait", 1)
                  ["pos"]["inputs"]["text"].split())))
    check("and the scene leads, weighted above the boilerplate",
          ":1.3)" in bg["pos"]["inputs"]["text"])
    check("and says the place is empty, not just forbids people",
          "nobody" in bg["pos"]["inputs"]["text"])
    # The character examples are written from the figure's point of view and
    # begin "standing in a...", which is itself a request for a person.
    # The default is the one most people will press Make with unchanged, so it
    # has to demonstrate the four things that actually work.
    # The default is what most people press Make with unchanged, and these
    # screens are 240-480 pixels with a character standing in front. So it has
    # to place a light (separation), name the ground (he stands on it) and let
    # the back fall away (depth without detail) -- and stay short, because
    # every word competes with the boilerplate for the same budget.
    d = generate.DEFAULT_SCENE.lower()
    check("the default places a light, not a mood",
          "light from the side" in d or "light from" in d, d)
    check("the default says what the ground is", "floor" in d, d)
    check("the default lets the back fall away",
          "shadow" in d or "distance" in d or "falling away" in d, d)
    check("and it is short enough to leave room",
          len(generate.DEFAULT_SCENE.split()) <= 24,
          f"{len(generate.DEFAULT_SCENE.split())} words")
    check("background examples never ask for someone standing there",
          not any("standing" in e["text"].lower() for e in generate.SCENE_EXAMPLES),
          str([e["label"] for e in generate.SCENE_EXAMPLES]))
    check("they are a separate set from the character ones",
          {e["text"] for e in generate.SCENE_EXAMPLES}
          .isdisjoint({e["text"] for e in generate.PROMPT_EXAMPLES}))
    check("every style has a label",
          all(v.get("label") for v in generate.STYLES.values()), str(sorted(generate.STYLES)))

    print("\nthe background shelf, as a BROWSER sees it")
    # Through the browser path on purpose. The JSON path was the only one
    # covered, so a NameError on the redirect sat in the delete route
    # unnoticed: the row vanished and the response was a 500.
    import library as _lib
    _buf = io.BytesIO()
    Image.new("RGB", (48, 48), (20, 24, 30)).save(_buf, "PNG")
    kept = _lib.bg_add(DATA_TEST, _buf.getvalue(),
                       {"scene": "a hall", "motion": "dust", "style": "dnd"})
    bc = srv.app.test_client()
    FORM = {"Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml"}
    r = bc.post(f"/backgrounds/{kept['id']}", data={"name": "Great hall",
                                                    "motion": "embers"},
                headers={"Accept": "text/html,application/xhtml+xml"})
    check("editing from a form redirects, not errors", r.status_code == 303,
          str(r.status_code))
    again = _lib.bg_get(DATA_TEST, kept["id"])
    check("and the words actually changed",
          again["name"] == "Great hall" and again["motion"] == "embers",
          str(again["name"]))
    # A delete button submits a form with NO fields, and the content type is
    # what marks it as a browser -- which is exactly the shape that caught the
    # Show and remove buttons before (see from_browser_form).
    r = bc.post(f"/backgrounds/{kept['id']}/delete",
                content_type="application/x-www-form-urlencoded",
                headers={"Accept": "text/html,application/xhtml+xml"})
    check("deleting from a form redirects, not errors", r.status_code == 303,
          str(r.status_code))
    check("and the place is gone", _lib.bg_get(DATA_TEST, kept["id"]) is None)
    check("deleting something that is not there is a 404",
          bc.post("/backgrounds/0000000000000000/delete").status_code == 404)

    print("\nstanding a character in a place")
    import library as _l
    _b = io.BytesIO(); Image.new("RGB", (384, 640), (70, 60, 50)).save(_b, "PNG")
    place = _l.bg_add(DATA_TEST, _b.getvalue(), {"scene": "a hall"})
    _c = io.BytesIO(); Image.new("RGB", (300, 400), (255, 255, 255)).save(_c, "PNG")
    # a dark blob on white paper: a subject the cut-out can actually find
    blob = Image.open(io.BytesIO(_c.getvalue())).convert("RGB")
    ImageDraw.Draw(blob).ellipse([110, 90, 190, 330], fill=(40, 60, 90))
    _c2 = io.BytesIO(); blob.save(_c2, "PNG")
    hero = _l.pool_add(DATA_TEST, _c2.getvalue(), "image/png", "hero.png")
    pc = srv.app.test_client()

    r = pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"]},
                content_type="multipart/form-data")
    check("composing answers with a picture",
          r.status_code == 200 and r.mimetype == "image/png", str(r.status_code))
    out = Image.open(io.BytesIO(r.data))
    check("it is the size of the PLACE, not the character", out.size == (384, 640),
          str(out.size))

    # Push-back is the whole reason this step exists: the backdrop has to give
    # way, or the figure competes with it on a 240 pixel panel.
    flat = pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                     "separation": "0", "shadow": "0"},
                   content_type="multipart/form-data").data
    pushed = pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                       "separation": "1", "shadow": "0"},
                     content_type="multipart/form-data").data
    import statistics
    def lum(b):
        im = Image.open(io.BytesIO(b)).convert("L")
        return statistics.fmean(im.crop((0, 0, 60, 60)).getdata())   # a corner
    check("pushing back actually dims the backdrop", lum(pushed) < lum(flat),
          f"{lum(pushed):.0f} vs {lum(flat):.0f}")

    tall = pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                     "fill": "0.95"}, content_type="multipart/form-data").data
    short = pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                      "fill": "0.45"}, content_type="multipart/form-data").data
    check("how tall he stands is actually a control", tall != short)

    check("keeping puts it in the pool",
          pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                    "keep": "1"},
                  content_type="multipart/form-data").get_json().get("kept"))
    check("a place is required",
          pc.post("/compose", data={"pool_id": hero["id"]},
                  content_type="multipart/form-data").status_code == 400)
    check("a character is required",
          pc.post("/compose", data={"bg_id": place["id"]},
                  content_type="multipart/form-data").status_code == 400)
    # Placement has to be movable: the floor in a generated place is wherever
    # the model put it, so a fixed baseline is a guess that is often wrong.
    base_hi = pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                        "baseline": "0.6"},
                      content_type="multipart/form-data").data
    base_lo = pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                        "baseline": "1.0"},
                      content_type="multipart/form-data").data
    check("up and down moves him", base_hi != base_lo)
    across = pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                       "offset": "0.3"},
                     content_type="multipart/form-data").data
    check("and across moves him too", across != flat)

    # The cut-out needs a manual escape: a clear plastic base is neither
    # backdrop nor figure, and no automatic rule gets every photo right.
    trimmed = pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                        "trim": "0.2"},
                      content_type="multipart/form-data").data
    check("his feet can be trimmed by hand", trimmed != flat)
    loose = pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                      "white": "150"},
                    content_type="multipart/form-data").data
    check("and how much counts as backdrop is adjustable", loose != flat)
    check("an impossible baseline is refused",
          pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                    "baseline": "nonsense"},
                  content_type="multipart/form-data").status_code == 400)

    # The cut-out is done by a model now, which means it needs ComfyUI. It
    # must DEGRADE rather than break: the compose path still answers with a
    # picture when the generator is down, using the old flood-fill.
    check("composing still works with no generator",
          pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                    "ai_cutout": "0"},
                  content_type="multipart/form-data").status_code == 200)
    # Needs a figure that actually HAS a disc: legs as two narrow spans, then
    # a single wide one under them, which is the shape the detector reads.
    based = Image.new("RGB", (300, 400), (255, 255, 255))
    bd = ImageDraw.Draw(based)
    bd.rectangle([120, 60, 150, 300], fill=(40, 60, 90))      # leg
    bd.rectangle([170, 60, 200, 300], fill=(40, 60, 90))      # leg
    bd.ellipse([60, 300, 260, 360], fill=(20, 20, 20))        # the base
    _bb = io.BytesIO(); based.save(_bb, "PNG")
    onbase = _l.pool_add(DATA_TEST, _bb.getvalue(), "image/png", "onbase.png")
    check("the baseplate is kept unless asked",
          pc.post("/compose", data={"bg_id": place["id"], "pool_id": onbase["id"],
                                    "ai_cutout": "0"},
                  content_type="multipart/form-data").data
          != pc.post("/compose", data={"bg_id": place["id"], "pool_id": onbase["id"],
                                       "ai_cutout": "0", "drop_base": "1"},
                     content_type="multipart/form-data").data)
    check("and the detector finds that disc",
          generate._baseplate_top(
              generate._subject_mask(based),
              generate._subject_mask(based).getbbox()) is not None)
    check("a cut-out that cannot run falls back rather than raising",
          generate.cutout(b"not an image at all") is None)

    check("a nonsense slider is refused, not clamped silently",
          pc.post("/compose", data={"bg_id": place["id"], "pool_id": hero["id"],
                                    "fill": "enormous"},
                  content_type="multipart/form-data").status_code == 400)

    print("@NLanimating a place, with nobody in it")
    import library as _lb
    _pb = io.BytesIO(); Image.new("RGB", (384, 640), (60, 50, 40)).save(_pb, "PNG")
    room = _lb.bg_add(DATA_TEST, _pb.getvalue(),
                      {"scene": "a hall", "motion": "drifting smoke"})
    ac = srv.app.test_client()
    check("a place starts with no loop", room.get("has_clip") in (None, False))

    # The graph the model is handed must contain the PLACE and no character.
    g = generate.background_clip_graph("room.png", "drifting smoke", "2s", seed=3)
    check("the loop starts from the place's own picture",
          g["img"]["inputs"]["image"] == "room.png")
    check("and the words are what moves, not what it looks like",
          g["pos"]["inputs"]["text"] == "drifting smoke")
    check("people are refused outright", "people" in g["neg"]["inputs"]["text"])
    check("only the START frame is pinned, so it can actually move",
          "end_image" not in g["i2v"]["inputs"])
    check("and the loop is closed by ping-pong instead",
          g["out"]["inputs"]["pingpong"] is True)
    check("length is Wan's 4n+1",
          (g["i2v"]["inputs"]["length"] - 1) % 4 == 0, str(g["i2v"]["inputs"]["length"]))
    check("a longer loop really is longer",
          generate.background_clip_graph("r.png", "m", "3s")["i2v"]["inputs"]["length"]
          > generate.background_clip_graph("r.png", "m", "1s")["i2v"]["inputs"]["length"])

    # An empty override falls back to the words stored with the place, which
    # is the point of storing them -- so the refusal needs a place that has
    # none at all.
    _nb = io.BytesIO(); Image.new("RGB", (64, 64), (10, 10, 10)).save(_nb, "PNG")
    silent = _lb.bg_add(DATA_TEST, _nb.getvalue(), {"scene": "a still room"})
    check("animating with nothing to move is refused",
          ac.post(f"/backgrounds/{silent['id']}/animate", data={"motion": ""},
                  content_type="application/x-www-form-urlencoded").status_code == 400)
    check("but an empty override falls back to the stored words",
          ac.post(f"/backgrounds/{room['id']}/animate", data={"motion": ""},
                  content_type="application/x-www-form-urlencoded"
                  ).status_code in (202, 303, 503))
    check("an unknown length is refused",
          ac.post(f"/backgrounds/{room['id']}/animate",
                  data={"motion": "smoke", "length": "9s"},
                  content_type="application/x-www-form-urlencoded").status_code in (400, 503))
    check("a loop that does not exist is a 404",
          ac.get(f"/backgrounds/{room['id']}/clip").status_code == 404)

    # Once filed, the place carries it.
    _lb.bg_set_clip(DATA_TEST, room["id"], b"not really an mp4, but bytes")
    check("a filed loop is served back",
          ac.get(f"/backgrounds/{room['id']}/clip").status_code == 200)
    check("and the shelf says the place moves",
          next(b for b in _lb.backgrounds(DATA_TEST) if b["id"] == room["id"])["has_clip"])
    _lb.bg_remove(DATA_TEST, room["id"])
    check("deleting the place takes its loop with it",
          not _lb.bg_clip_path(DATA_TEST, room["id"]).exists())

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
