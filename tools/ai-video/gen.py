#!/usr/bin/env python
"""Generate short looping clips for the tabletop screens, via a local ComfyUI.

Two graphs, picked by whether --image is given:

  text -> video   AnimateDiff v3 motion module over an SD1.5 checkpoint.
  image -> video  the same, with SparseCtrl RGB anchoring frame 0 to a still,
                  so an existing pool picture gains motion instead of being
                  replaced by a new invention.

Sizes are named after the panels rather than given in pixels, because SD1.5
wants to render near 512x512 and the panel wants its own aspect ratio; the
preset is the compromise, and the content server rescales to the panel
afterwards anyway.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HOST = "http://127.0.0.1:8188"

CHECKPOINT = "DreamShaper_8_pruned.safetensors"
VAE = "vae-ft-mse-840000-ema-pruned.safetensors"
MOTION = "v3_sd15_mm.ckpt"
ADAPTER = "v3_sd15_adapter.ckpt"
SPARSECTRL = "v3_sd15_sparsectrl_rgb.ckpt"
LCM = "lcm_lora_sd15.safetensors"

# Panel -> render size. Multiples of 8, kept near SD1.5's native pixel budget:
# much above ~640x384 and a 16-frame batch stops fitting comfortably in 32 GB.
SCREENS = {
    "p4":    (384, 640),   # 480x800 portrait, Waveshare P4 4.3
    "s3":    (640, 384),   # 800x480 landscape, Waveshare S3 4.3
    "round": (512, 512),   # 240x240 round, Seeed XIAO
    "square": (512, 512),
}

NEGATIVE = ("worst quality, low quality, blurry, jpeg artifacts, watermark, "
            "text, signature, deformed, extra limbs, flicker")


def build(args) -> dict:
    """The API-format graph. Node ids are strings; links are [node_id, slot]."""
    w, h = SCREENS[args.screen] if args.size is None else args.size
    seed = args.seed if args.seed is not None else random.randrange(2**31)

    base_model = ["lcm", 0] if args.lcm else ["lora", 0]
    base_clip = ["lcm", 1] if args.lcm else ["lora", 1]

    g: dict = {
        "ckpt": {"class_type": "CheckpointLoaderSimple",
                 "inputs": {"ckpt_name": CHECKPOINT}},
        "vae": {"class_type": "VAELoader",
                "inputs": {"vae_name": VAE}},
        # The v3 adapter LoRA is what makes the v3 motion module behave; without
        # it the motion is there but the image drifts off the checkpoint's style.
        "lora": {"class_type": "LoraLoader",
                 "inputs": {"model": ["ckpt", 0], "clip": ["ckpt", 1],
                            "lora_name": ADAPTER,
                            "strength_model": args.adapter,
                            "strength_clip": args.adapter}},
        # Sliding-window context. closed_loop makes the last frame reach back to
        # the first, which is the whole point on a screen that never stops.
        "ctx": {"class_type": "ADE_LoopedUniformContextOptions",
                "inputs": {"context_length": min(16, args.frames),
                           "context_stride": 1,
                           "context_overlap": 4,
                           "closed_loop": args.loop,
                           "fuse_method": "pyramid",
                           "use_on_equal_length": args.loop}},
        "ad": {"class_type": "ADE_AnimateDiffLoaderGen1",
               "inputs": {"model": base_model, "model_name": MOTION,
                          "beta_schedule": "lcm" if args.lcm else "autoselect",
                          "context_options": ["ctx", 0]}},
        "pos": {"class_type": "CLIPTextEncode",
                "inputs": {"clip": base_clip, "text": args.prompt}},
        "neg": {"class_type": "CLIPTextEncode",
                "inputs": {"clip": base_clip, "text": args.negative}},
        # batch_size is the frame count: AnimateDiff turns a batch of latents
        # into a temporally coherent sequence.
        "latent": {"class_type": "EmptyLatentImage",
                   "inputs": {"width": w, "height": h, "batch_size": args.frames}},
        "sampler": {"class_type": "KSampler",
                    "inputs": {"model": ["ad", 0],
                               "positive": ["pos", 0], "negative": ["neg", 0],
                               "latent_image": ["latent", 0],
                               "seed": seed, "steps": args.steps, "cfg": args.cfg,
                               "sampler_name": args.sampler, "scheduler": "karras",
                               "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode",
                   "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]}},
        "out": {"class_type": "VHS_VideoCombine",
                "inputs": {"images": ["decode", 0],
                           "frame_rate": args.fps, "loop_count": 0,
                           "filename_prefix": args.name,
                           "format": "video/h264-mp4",
                           "pingpong": args.pingpong, "save_output": True}},
    }

    if args.interpolate > 1:
        # RIFE invents the in-between frames. The motion module is trained at
        # 8fps, so generating faster just plays the same motion double speed;
        # interpolating instead buys smoothness at no sampler cost.
        g["rife"] = {"class_type": "RIFE VFI",
                     "inputs": {"ckpt_name": "rife49.pth",
                                "frames": ["decode", 0],
                                "clear_cache_after_n_frames": 10,
                                "multiplier": args.interpolate,
                                "fast_mode": True, "ensemble": True,
                                "scale_factor": 1.0, "dtype": "float32",
                                "torch_compile": False, "batch_size": 1}}
        g["out"]["inputs"]["images"] = ["rife", 0]
        g["out"]["inputs"]["frame_rate"] = args.fps * args.interpolate

    if args.lcm:
        # Latent-consistency LoRA: ~8 steps at low cfg instead of ~20 at 8.
        # On an M1 Pro that is the difference between iterating and waiting.
        g["lcm"] = {"class_type": "LoraLoader",
                    "inputs": {"model": ["lora", 0], "clip": ["lora", 1],
                               "lora_name": LCM,
                               "strength_model": 1.0, "strength_clip": 1.0}}

    if args.image:
        # SparseCtrl RGB: the still is encoded into the control latent and
        # pinned at the frame given by --anchor, and the sampler fills the rest.
        g["loadimg"] = {"class_type": "LoadImage",
                        "inputs": {"image": args.image}}
        g["prep"] = {"class_type": "ACN_SparseCtrlRGBPreprocessor",
                     "inputs": {"image": ["loadimg", 0], "vae": ["vae", 0],
                                "latent_size": ["latent", 0]}}
        g["idx"] = {"class_type": "ACN_SparseCtrlIndexMethodNode",
                    "inputs": {"indexes": args.anchor}}
        g["sparse"] = {"class_type": "ACN_SparseCtrlLoaderAdvanced",
                       "inputs": {"sparsectrl_name": SPARSECTRL,
                                  "use_motion": True,
                                  "motion_strength": 1.0, "motion_scale": 1.0,
                                  "sparse_method": ["idx", 0]}}
        g["cnet"] = {"class_type": "ControlNetApplyAdvanced",
                     "inputs": {"positive": ["pos", 0], "negative": ["neg", 0],
                                "control_net": ["sparse", 0],
                                "image": ["prep", 0],
                                "strength": args.control,
                                "start_percent": 0.0, "end_percent": 1.0,
                                "vae": ["vae", 0]}}
        g["sampler"]["inputs"]["positive"] = ["cnet", 0]
        g["sampler"]["inputs"]["negative"] = ["cnet", 1]

    return g, seed, (w, h)


def post(graph: dict, client_id: str) -> str:
    body = json.dumps({"prompt": graph, "client_id": client_id}).encode()
    req = urllib.request.Request(f"{HOST}/prompt", body,
                                 {"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req))["prompt_id"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        try:
            err = json.loads(detail)["error"]
            print(f"ComfyUI rejected the graph: {err.get('message')}\n"
                  f"  {err.get('details','')}", file=sys.stderr)
            for n in json.loads(detail).get("node_errors", {}).items():
                print(f"  node {n[0]}: {n[1]}", file=sys.stderr)
        except Exception:
            print(detail, file=sys.stderr)
        sys.exit(1)


def wait(prompt_id: str) -> list[str]:
    """Block until the prompt leaves the queue, printing sampler progress."""
    t0 = time.time()
    last = ""
    while True:
        hist = json.load(urllib.request.urlopen(f"{HOST}/history/{prompt_id}"))
        if prompt_id in hist:
            entry = hist[prompt_id]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                for m in status.get("messages", []):
                    if m[0] == "execution_error":
                        print(f"\nfailed: {m[1].get('exception_message')}",
                              file=sys.stderr)
                sys.exit(1)
            files = []
            for out in entry.get("outputs", {}).values():
                for key in ("gifs", "images", "videos"):
                    for f in out.get(key, []):
                        files.append(f.get("filename", ""))
            files = [f for f in files if f]
            if not files:
                # A prompt cancelled from the GUI lands here too: it leaves a
                # history entry with no outputs and no error to report.
                print(f"\nno output after {time.time()-t0:.0f}s -- the run was "
                      f"most likely cancelled or cleared from the ComfyUI queue",
                      file=sys.stderr)
                sys.exit(1)
            print(f"\ndone in {time.time()-t0:.0f}s")
            return files
        q = json.load(urllib.request.urlopen(f"{HOST}/queue"))
        running = len(q.get("queue_running", []))
        pending = len(q.get("queue_pending", []))
        msg = f"  [{time.time()-t0:5.0f}s] running={running} pending={pending}"
        if msg != last:
            print(msg, end="\r", flush=True)
            last = msg
        time.sleep(2)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("prompt", help="what the clip should show")
    p.add_argument("--image", help="a still to animate (filename in ComfyUI/input/)")
    p.add_argument("--screen", default="p4", choices=sorted(SCREENS),
                   help="render size preset (default p4)")
    p.add_argument("--size", nargs=2, type=int, metavar=("W", "H"),
                   help="explicit size, overrides --screen")
    p.add_argument("--frames", type=int, default=16, help="frame count (default 16)")
    p.add_argument("--fps", type=float, default=8,
                   help="motion frame rate (default 8, what the model was trained at); "
                        "--interpolate multiplies the rate actually written")
    p.add_argument("--interpolate", type=int, default=2, metavar="N",
                   help="RIFE frame interpolation factor (default 2 -> 16fps out; 1 disables)")
    p.add_argument("--steps", type=int, help="default 20, or 8 with --lcm")
    p.add_argument("--cfg", type=float, help="default 8.0, or 2.0 with --lcm")
    p.add_argument("--sampler", help="default euler, or lcm with --lcm")
    p.add_argument("--lcm", action="store_true",
                   help="latent-consistency LoRA: far fewer steps, slightly softer")
    p.add_argument("--seed", type=int, help="omit for a random one")
    p.add_argument("--adapter", type=float, default=0.8,
                   help="v3 adapter LoRA strength (default 0.8)")
    p.add_argument("--control", type=float, default=1.0,
                   help="SparseCtrl strength, with --image (default 1.0)")
    p.add_argument("--anchor",
                   help="frame indexes the still is pinned to. Defaults to both "
                        "ends (\"0,N-1\"): anchoring only frame 0 lets everything "
                        "after it drift off the subject")
    p.add_argument("--negative", default=NEGATIVE)
    p.add_argument("--name", default="tabletop", help="output filename prefix")
    p.add_argument("--loop", action=argparse.BooleanOptionalAction,
                   help="closed-loop context, so the clip joins end to start. "
                        "On by default for text-to-video, off with --image, where "
                        "it pulls against the anchor on the final frame")
    p.add_argument("--pingpong", action="store_true",
                   help="append the clip reversed instead of closing the loop")
    p.add_argument("--dump", action="store_true", help="print the graph and exit")
    return p


def resolve(args):
    """Defaults that depend on --lcm, applied after parsing."""
    if args.steps is None:   args.steps = 8 if args.lcm else 20
    if args.cfg is None:     args.cfg = 2.0 if args.lcm else 8.0
    if args.sampler is None: args.sampler = "lcm" if args.lcm else "euler"
    # Animating a still wants different defaults from inventing one. Pinning
    # only frame 0 lets the rest wander into a different subject entirely, and
    # a closed loop then fights the anchor it is trying to come back to.
    if args.anchor is None:
        args.anchor = f"0,{args.frames - 1}" if args.image else "0"
    if args.loop is None:
        args.loop = not args.image
    return args


def main() -> None:
    args = resolve(build_parser().parse_args())

    graph, seed, (w, h) = build(args)
    if args.dump:
        print(json.dumps(graph, indent=2))
        return

    mode = f"image->video from {args.image}" if args.image else "text->video"
    out_fps = args.fps * max(1, args.interpolate)
    out_frames = args.frames * max(1, args.interpolate)
    print(f"{mode}  {w}x{h}  {args.frames}f @ {args.fps}fps motion "
          f"-> {out_frames}f @ {out_fps:g}fps  "
          f"steps={args.steps} seed={seed} loop={args.loop}")
    print(f"  \"{args.prompt}\"")

    files = wait(post(graph, str(uuid.uuid4())))
    outdir = Path.home() / "ComfyUI" / "output"
    for f in files:
        print(f"  -> {outdir / f}")


if __name__ == "__main__":
    main()
