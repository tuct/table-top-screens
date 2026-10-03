"""Clip generation, driven by a local ComfyUI.

The server does not generate anything itself. It hands a graph to a ComfyUI
running on this machine (or any machine named by COMFYUI_URL) and collects the
file that comes back. Which means the whole feature is optional in exactly the
way video decoding is: with no ComfyUI reachable, generation is refused with a
line saying why, and everything else works.

Two shapes of job, because generating a loop costs minutes and most of those
minutes are wasted on a prompt that was never going to look right:

    verify   one still, no motion. Seconds, not minutes. Answers "is this the
             picture I meant?" before committing to the clip.
    clip     the real thing: an animated, looping MP4, which lands in the pool.

Jobs run on background threads and are polled, because a Flask request cannot
sit open for four minutes and a browser would not wait if it could.

The graphs here are deliberately a separate copy from tools/ai-video/gen.py.
That script is for experimenting at a terminal and changes freely; this one is
a product surface, and the two having different reasons to change is worth
more than sharing the code would save.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

COMFY_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")

# What bootstrap.py installs. A missing one surfaces as a plain ComfyUI error
# naming the file, which is more useful than anything we could guess here.
CHECKPOINT = "DreamShaper_8_pruned.safetensors"
VAE = "vae-ft-mse-840000-ema-pruned.safetensors"
MOTION = "v3_sd15_mm.ckpt"
ADAPTER = "v3_sd15_adapter.ckpt"
LCM = "lcm_lora_sd15.safetensors"
SPARSECTRL = "v3_sd15_sparsectrl_rgb.ckpt"

NEGATIVE = ("worst quality, low quality, blurry, jpeg artifacts, watermark, "
            "text, signature, deformed, extra limbs, flicker")

# Render sizes. SD1.5 wants to work near 512x512; the panel gets its own
# aspect ratio and the content server rescales to the actual pixels later.
SIZES = {
    "portrait":  (384, 640),
    "landscape": (640, 384),
    "square":    (512, 512),
}
DEFAULT_SIZE = "portrait"
AUTO_SIZE = "auto"      # match the reference's own proportions

# How much of the reference to hold on to. For a still this is the denoise --
# lower keeps more -- and for a clip it is how hard SparseCtrl pulls. Named for
# the intent rather than the number, because "denoise 0.35" does not say
# "looks like the picture I gave you", which is the thing being asked for.
KEEP = {
    "close":    {"denoise": 0.32, "control": 1.0},
    "balanced": {"denoise": 0.55, "control": 1.0},
    "loose":    {"denoise": 0.75, "control": 0.8},
}
DEFAULT_KEEP = "close"


def shape_for(width: int, height: int) -> str:
    """The named shape closest to these proportions.

    A 16:9 picture squeezed into a square loses its sides to a centre crop,
    which reads as the reference having been ignored when in fact most of it
    was thrown away before the model ever saw it.
    """
    if not width or not height:
        return DEFAULT_SIZE
    want = width / height
    return min(SIZES, key=lambda k: abs(SIZES[k][0] / SIZES[k][1] - want))

# Interpolation doubles the frames after sampling, so what the sampler is
# asked for is half of what gets written. Cost scales with the generated count,
# which is why a 4-second loop is four times a 1-second one and worth choosing
# deliberately rather than defaulting into.
INTERPOLATE = 2
FPS = 8
OUT_FPS = FPS * INTERPOLATE

# label -> frames the sampler generates. RIFE turns N into 2N-1, so the written
# length is (2N-1)/16 seconds -- a shade under the label, near enough to it.
LENGTHS = {
    "1s": 8,
    "2s": 16,
    "4s": 32,
}
DEFAULT_LENGTH = "4s"
FRAMES = LENGTHS[DEFAULT_LENGTH]


class NoComfyUI(RuntimeError):
    """No ComfyUI reachable. Generation is refused; the rest of the server is fine."""


# ------------------------------------------------------------------ transport

def _get(path: str, timeout: float = 5.0) -> Any:
    try:
        with urllib.request.urlopen(f"{COMFY_URL}{path}", timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise NoComfyUI(f"cannot reach ComfyUI at {COMFY_URL}: {exc}") from exc


_last_probe: tuple[float, bool] = (0.0, False)


def available() -> bool:
    """Is ComfyUI up? Cached briefly, so a dead server costs one timeout a page."""
    global _last_probe
    now = time.time()
    when, ok = _last_probe
    if now - when < 10.0:
        return ok
    try:
        _get("/system_stats", timeout=2.0)
        ok = True
    except NoComfyUI:
        ok = False
    _last_probe = (now, ok)
    return ok


def status_line() -> str:
    """One sentence for the page, whichever way it went."""
    if available():
        return f"ComfyUI at {COMFY_URL}"
    return (f"No ComfyUI at {COMFY_URL}. Start it with ~/ComfyUI/start.sh, or set "
            f"COMFYUI_URL if it runs elsewhere.")


def upload_reference(body: bytes, filename: str) -> str:
    """Put a reference image where ComfyUI's LoadImage can see it.

    Returns the name ComfyUI chose, which is not always the one we sent -- it
    renames on collision rather than overwriting someone else's file.
    """
    boundary = f"----tabletop{uuid.uuid4().hex}"
    safe = os.path.basename(filename) or "reference.png"
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="image"; filename="{safe}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        body, b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
        f"--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(
        f"{COMFY_URL}/upload/image", b"".join(parts),
        {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            got = json.load(r)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise NoComfyUI(f"upload to ComfyUI failed: {exc}") from exc
    name = got.get("name") or safe
    sub = got.get("subfolder") or ""
    return f"{sub}/{name}" if sub else name


def _queue_position(prompt_id: str) -> int | None:
    """How many prompts ComfyUI will finish first, or None once ours is gone.

    0 means ours is the one running. The queue entries are positional lists
    whose second element is the prompt id.
    """
    try:
        q = _get("/queue", timeout=5)
    except NoComfyUI:
        return None
    for entry in q.get("queue_running", []):
        if len(entry) > 1 and entry[1] == prompt_id:
            return 0
    pending = [e for e in q.get("queue_pending", []) if len(e) > 1]
    for i, entry in enumerate(sorted(pending, key=lambda e: e[0])):
        if entry[1] == prompt_id:
            return i + 1
    return None


def _fetch_output(filename: str, subfolder: str = "") -> bytes:
    q = urllib.parse.urlencode(
        {"filename": filename, "subfolder": subfolder, "type": "output"})
    with urllib.request.urlopen(f"{COMFY_URL}/view?{q}", timeout=120) as r:
        return r.read()


# --------------------------------------------------------------------- graphs

def _base(prompt: str, negative: str, width: int, height: int,
          batch: int, seed: int, steps: int) -> dict:
    """Checkpoint, VAE, the two LoRAs, and the conditioning both graphs share."""
    return {
        "ckpt": {"class_type": "CheckpointLoaderSimple",
                 "inputs": {"ckpt_name": CHECKPOINT}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "adapter": {"class_type": "LoraLoader",
                    "inputs": {"model": ["ckpt", 0], "clip": ["ckpt", 1],
                               "lora_name": ADAPTER,
                               "strength_model": 0.8, "strength_clip": 0.8}},
        # Latent consistency: 8 steps rather than 20. The whole feature is only
        # tolerable interactively because of this.
        "lcm": {"class_type": "LoraLoader",
                "inputs": {"model": ["adapter", 0], "clip": ["adapter", 1],
                           "lora_name": LCM,
                           "strength_model": 1.0, "strength_clip": 1.0}},
        "pos": {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["lcm", 1], "text": prompt}},
        "neg": {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["lcm", 1], "text": negative}},
    }


def still_graph(prompt: str, reference: str | None, size: str,
                seed: int, keep: str = DEFAULT_KEEP) -> dict:
    """One frame, no motion module. The quick look before the long wait.

    With a reference this is img2img at partial denoise, so the result keeps
    the reference's composition while showing what the prompt does to it --
    which is the question the verify step exists to answer.
    """
    w, h = SIZES.get(size, SIZES[DEFAULT_SIZE])
    g = _base(prompt, NEGATIVE, w, h, 1, seed, 8)

    if reference:
        g["ref"] = {"class_type": "LoadImage", "inputs": {"image": reference}}
        g["fit"] = {"class_type": "ImageScale",
                    "inputs": {"image": ["ref", 0], "upscale_method": "lanczos",
                               "width": w, "height": h, "crop": "center"}}
        g["latent"] = {"class_type": "VAEEncode",
                       "inputs": {"pixels": ["fit", 0], "vae": ["vae", 0]}}
        denoise = KEEP.get(keep, KEEP[DEFAULT_KEEP])["denoise"]
    else:
        g["latent"] = {"class_type": "EmptyLatentImage",
                       "inputs": {"width": w, "height": h, "batch_size": 1}}
        denoise = 1.0

    g["sampler"] = {"class_type": "KSampler",
                    "inputs": {"model": ["lcm", 0],
                               "positive": ["pos", 0], "negative": ["neg", 0],
                               "latent_image": ["latent", 0],
                               "seed": seed, "steps": 8, "cfg": 2.0,
                               "sampler_name": "lcm", "scheduler": "karras",
                               "denoise": denoise}}
    g["decode"] = {"class_type": "VAEDecode",
                   "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]}}
    g["out"] = {"class_type": "SaveImage",
                "inputs": {"images": ["decode", 0], "filename_prefix": "verify"}}
    return g


def clip_graph(prompt: str, reference: str | None, size: str, seed: int,
               length: str = DEFAULT_LENGTH, keep: str = DEFAULT_KEEP) -> dict:
    """The looping MP4.

    With a reference, SparseCtrl pins it to both ends of the sequence and the
    closed loop comes off: anchored only at frame 0 the animation drifts off
    the subject well before the end, and a closed loop then fights the anchor
    it is trying to return to.
    """
    w, h = SIZES.get(size, SIZES[DEFAULT_SIZE])
    frames = LENGTHS.get(length, LENGTHS[DEFAULT_LENGTH])
    g = _base(prompt, NEGATIVE, w, h, frames, seed, 8)

    g["latent"] = {"class_type": "EmptyLatentImage",
                   "inputs": {"width": w, "height": h, "batch_size": frames}}
    # The motion module was trained on 16-frame windows. A longer clip slides
    # that window rather than widening it, so the cost is linear and the motion
    # stays coherent instead of dissolving past frame 16.
    g["ctx"] = {"class_type": "ADE_LoopedUniformContextOptions",
                "inputs": {"context_length": min(16, frames), "context_stride": 1,
                           "context_overlap": 4,
                           "closed_loop": reference is None,
                           "fuse_method": "pyramid",
                           "use_on_equal_length": reference is None}}
    g["ad"] = {"class_type": "ADE_AnimateDiffLoaderGen1",
               "inputs": {"model": ["lcm", 0], "model_name": MOTION,
                          "beta_schedule": "lcm",
                          "context_options": ["ctx", 0]}}

    positive, negative = ["pos", 0], ["neg", 0]
    if reference:
        g["ref"] = {"class_type": "LoadImage", "inputs": {"image": reference}}
        g["fit"] = {"class_type": "ImageScale",
                    "inputs": {"image": ["ref", 0], "upscale_method": "lanczos",
                               "width": w, "height": h, "crop": "center"}}
        g["prep"] = {"class_type": "ACN_SparseCtrlRGBPreprocessor",
                     "inputs": {"image": ["fit", 0], "vae": ["vae", 0],
                                "latent_size": ["latent", 0]}}
        g["idx"] = {"class_type": "ACN_SparseCtrlIndexMethodNode",
                    "inputs": {"indexes": f"0,{frames - 1}"}}
        g["sparse"] = {"class_type": "ACN_SparseCtrlLoaderAdvanced",
                       "inputs": {"sparsectrl_name": SPARSECTRL,
                                  "use_motion": True, "motion_strength": 1.0,
                                  "motion_scale": 1.0,
                                  "sparse_method": ["idx", 0]}}
        g["cnet"] = {"class_type": "ControlNetApplyAdvanced",
                     "inputs": {"positive": ["pos", 0], "negative": ["neg", 0],
                                "control_net": ["sparse", 0], "image": ["prep", 0],
                                "strength": KEEP.get(keep, KEEP[DEFAULT_KEEP])["control"],
                                "start_percent": 0.0, "end_percent": 1.0,
                                "vae": ["vae", 0]}}
        positive, negative = ["cnet", 0], ["cnet", 1]

    g["sampler"] = {"class_type": "KSampler",
                    "inputs": {"model": ["ad", 0],
                               "positive": positive, "negative": negative,
                               "latent_image": ["latent", 0],
                               "seed": seed, "steps": 8, "cfg": 2.0,
                               "sampler_name": "lcm", "scheduler": "karras",
                               "denoise": 1.0}}
    g["decode"] = {"class_type": "VAEDecode",
                   "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]}}
    # RIFE invents the in-between frames. The motion module is trained at 8fps,
    # so a higher rate would only play the same motion faster; interpolating
    # buys smoothness the sampler never has to pay for.
    g["rife"] = {"class_type": "RIFE VFI",
                 "inputs": {"ckpt_name": "rife49.pth", "frames": ["decode", 0],
                            "clear_cache_after_n_frames": 10,
                            "multiplier": INTERPOLATE, "fast_mode": True,
                            "ensemble": True, "scale_factor": 1.0,
                            "dtype": "float32", "torch_compile": False,
                            "batch_size": 1}}
    g["out"] = {"class_type": "VHS_VideoCombine",
                "inputs": {"images": ["rife", 0],
                           "frame_rate": OUT_FPS, "loop_count": 0,
                           "filename_prefix": "clip", "format": "video/h264-mp4",
                           "pingpong": False, "save_output": True}}
    return g


# ----------------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    kind: str                       # "verify" or "clip"
    prompt: str
    size: str
    seed: int
    has_reference: bool
    prompt_id: str = ""             # ComfyUI's id for it, once submitted
    keep: str = DEFAULT_KEEP        # how much of the reference to hold on to
    length: str = DEFAULT_LENGTH    # only meaningful for a clip
    adopted_from: str = ""          # the verify this one was refined out of
    state: str = "queued"           # queued | running | done | error
    ahead: int = 0                  # jobs ComfyUI will do before this one
    running_since: float = 0.0      # when ComfyUI picked it up, not when we asked
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    result: bytes = b""
    content_type: str = ""
    filename: str = ""
    pool_id: str = ""               # set once it has been kept

    @property
    def elapsed(self) -> float:
        """Since it was asked for -- queue wait included."""
        return (self.finished or time.time()) - self.started

    @property
    def running(self) -> float:
        """Since ComfyUI actually started it. The number worth showing.

        Time spent queued behind another job says nothing about how long this
        one takes, so a clock counting it answers a different question from
        the one being asked.
        """
        if not self.running_since:
            return 0.0
        return (self.finished or time.time()) - self.running_since

    def public(self) -> dict:
        """What the page is allowed to see. Never the bytes."""
        return {"id": self.id, "kind": self.kind, "state": self.state,
                "error": self.error, "elapsed": round(self.elapsed, 1),
                "running": round(self.running, 1),
                "prompt": self.prompt, "size": self.size, "seed": self.seed,
                "has_reference": self.has_reference, "ahead": self.ahead,
                "length": self.length, "keep": self.keep,
                "adopted_from": self.adopted_from,
                "is_image": self.content_type.startswith("image/"),
                "filename": self.filename, "pool_id": self.pool_id,
                "estimate": estimate_for(self.kind, self.length)}


# Rough, measured on an Apple M1 Pro. Only ever used to pace a progress bar,
# so being wrong makes the bar wrong and nothing else. A clip scales with the
# frames asked for; a verify is one frame whatever else is set.
ESTIMATES = {"verify": 10, "clip": 240}


def estimate_for(kind: str, length: str = DEFAULT_LENGTH) -> int:
    if kind != "clip":
        return ESTIMATES.get(kind, 30)
    return round(ESTIMATES["clip"] * LENGTHS.get(length, FRAMES) / FRAMES)

_jobs: dict[str, Job] = {}
_lock = threading.Lock()
# Ten of each kind are shown, so keeping a few more than that leaves room for
# the ones still running and the ones that failed.
GALLERY = 10
MAX_JOBS = 30


def queue_view() -> list[dict]:
    """Everything ComfyUI is working on, whether or not we asked for it.

    The queue is the truth about what the machine is busy with, and a clip
    started from ComfyUI's own page delays ours exactly as much as one of ours
    does. Showing only our own would explain a four-minute wait with an empty
    list.
    """
    try:
        q = _get("/queue", timeout=4)
    except NoComfyUI:
        return []

    with _lock:
        mine = {j.prompt_id: j for j in _jobs.values() if j.prompt_id}

    rows: list[dict] = []
    running = [e for e in q.get("queue_running", []) if len(e) > 1]
    pending = sorted((e for e in q.get("queue_pending", []) if len(e) > 1),
                     key=lambda e: e[0])
    for position, entry in enumerate(running + pending):
        pid = entry[1]
        job = mine.get(pid)
        if job is not None:
            rows.append({**job.public(), "mine": True, "ahead": position})
        else:
            # Someone queued this from ComfyUI directly. We cannot say what it
            # is -- only that it is in front of ours.
            rows.append({"id": pid[:12], "mine": False, "kind": "elsewhere",
                         "state": "running" if position == 0 else "queued",
                         "ahead": position, "prompt": "started in ComfyUI",
                         "elapsed": 0.0, "running": 0.0, "estimate": 0,
                         "length": "", "keep": "", "size": "", "seed": 0,
                         "has_reference": False, "is_image": False,
                         "filename": "", "pool_id": "", "error": "",
                         "adopted_from": ""})
    return rows


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def recent(limit: int = 8, kind: str | None = None,
           done_only: bool = False) -> list[Job]:
    """Newest first. `kind` and `done_only` are what the galleries ask for."""
    with _lock:
        jobs = list(_jobs.values())
    if kind:
        jobs = [j for j in jobs if j.kind == kind]
    if done_only:
        jobs = [j for j in jobs if j.state == "done" and j.result]
    return sorted(jobs, key=lambda j: j.started, reverse=True)[:limit]


def _remember(job: Job) -> None:
    with _lock:
        _jobs[job.id] = job
        if len(_jobs) > MAX_JOBS:
            # Results are bytes in memory; keeping every one would be a leak
            # dressed up as a feature.
            for old in sorted(_jobs.values(), key=lambda j: j.started)[:-MAX_JOBS]:
                _jobs.pop(old.id, None)


def submit(kind: str, prompt: str, reference: str | None, size: str, seed: int,
           length: str = DEFAULT_LENGTH, adopted_from: str = "",
           keep: str = DEFAULT_KEEP) -> Job:
    """Queue a job and return at once; the work happens on a thread."""
    if not prompt.strip() and kind == "clip" and not reference:
        raise ValueError("a clip needs a description, a reference image, or both")

    job = Job(id=uuid.uuid4().hex[:12], kind=kind, prompt=prompt, size=size,
              seed=seed, has_reference=bool(reference), length=length,
              adopted_from=adopted_from, keep=keep)
    _remember(job)

    graph = (still_graph(prompt, reference, size, seed, keep) if kind == "verify"
             else clip_graph(prompt, reference, size, seed, length, keep))
    threading.Thread(target=_run, args=(job, graph), daemon=True).start()
    return job


def _run(job: Job, graph: dict) -> None:
    try:
        # Deliberately not "running" yet. ComfyUI decides when this starts, and
        # claiming otherwise before it has even been submitted starts a clock
        # against work nobody is doing.
        body = json.dumps({"prompt": graph, "client_id": job.id}).encode()
        req = urllib.request.Request(f"{COMFY_URL}/prompt", body,
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                prompt_id = json.load(r)["prompt_id"]
            job.prompt_id = prompt_id
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_explain(exc)) from exc

        while True:
            time.sleep(1.5)

            # Waiting behind someone else is not the same as being slow, and
            # the page says so differently.
            ahead = _queue_position(prompt_id)
            if ahead is not None:
                job.ahead = ahead
                job.state = "running" if ahead == 0 else "queued"
                if ahead == 0 and not job.running_since:
                    job.running_since = time.time()
                continue

            hist = _get(f"/history/{prompt_id}", timeout=15)
            if prompt_id not in hist:
                continue
            entry = hist[prompt_id]
            if entry.get("status", {}).get("status_str") == "error":
                raise RuntimeError(_error_from(entry))

            for out in entry.get("outputs", {}).values():
                for key in ("gifs", "videos", "images"):
                    for f in out.get(key, []):
                        name = f.get("filename")
                        if not name:
                            continue
                        job.result = _fetch_output(name, f.get("subfolder", ""))
                        job.filename = name
                        job.content_type = ("video/mp4" if name.endswith(".mp4")
                                            else "image/png")
                        job.state = "done"
                        job.finished = time.time()
                        return
            # A history entry with no outputs is a cancelled run, not a success.
            raise RuntimeError("the run produced no output -- it was most "
                               "likely cancelled from the ComfyUI queue")
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        job.state = "error"
        job.error = str(exc)[:400]
        job.finished = time.time()


def _explain(exc: urllib.error.HTTPError) -> str:
    """Turn ComfyUI's rejection into something worth showing a person."""
    try:
        payload = json.loads(exc.read().decode())
    except Exception:                             # noqa: BLE001
        return f"ComfyUI rejected the request ({exc.code})"
    err = payload.get("error") or {}
    msg = err.get("message") or f"rejected ({exc.code})"
    for node, detail in (payload.get("node_errors") or {}).items():
        first = (detail.get("errors") or [{}])[0]
        if first.get("message"):
            return f"{msg}: {first['message']} (node {node})"
    return f"{msg}. {err.get('details', '')}".strip()


def _error_from(entry: dict) -> str:
    for m in entry.get("status", {}).get("messages", []):
        if m[0] == "execution_error":
            info = m[1]
            return (f"{info.get('node_type', 'a node')} failed: "
                    f"{info.get('exception_message', 'unknown error')}")
    return "generation failed"
