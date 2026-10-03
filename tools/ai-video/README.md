# Local AI video for the tabletop screens

Generates short, looping clips on your own machine — no API, no account, no
per-clip cost — and writes them as MP4, which is what `server/` already accepts
and rescales per panel.

Nothing large lives in this directory. ComfyUI, the node packs and ~5.9 GB of
model weights are all **downloaded** by the installer, pinned to exact commits
and verified by SHA-256. What is version-controlled here is the thin layer:
the manifest of what to fetch, and the two scripts that drive it.

```
install.sh / install.ps1   ensure uv exists, hand off to bootstrap.py
bootstrap.py               all install logic, shared by both platforms
manifest.toml              what to download, and exactly which version
gen.py                     generate a clip from the command line
to_ui.py                   turn those graphs into GUI-loadable workflows
```

## Install

macOS / Linux:

```sh
cd tools/ai-video
./install.sh
```

Windows:

```powershell
cd tools\ai-video
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Both take the same flags: `--no-models` to install code only, `--dir D` to
install somewhere other than `~/ComfyUI`, `--update` to bump the pins.

You need `git` and an internet connection. You do **not** need a system Python —
`uv` fetches its own, because PyTorch publishes wheels a release or two behind
the newest CPython and the system one is routinely too new.

Re-running is safe. Every step checks for what it would create and skips it, so
the installer doubles as a repair tool; an interrupted download resumes rather
than restarting.

## Use

Start ComfyUI, which must be running for either script:

```sh
~/ComfyUI/start.sh            # start.cmd on Windows
```

Then either drive it from the terminal:

```sh
~/ComfyUI/.venv/bin/python ~/ComfyUI/tabletop/gen.py \
  "a torchlit tavern at night, firelight flickering" --screen p4 --lcm
```

or open <http://127.0.0.1:8188> and pick one of the `tabletop - …` workflows
from the sidebar. Run `to_ui.py` once to generate those.

Clips land in `~/ComfyUI/output/`. Upload one to the content server like any
other video.

### Options worth knowing

| Flag | Why |
|---|---|
| `--screen p4 \| s3 \| round` | render size matched to a panel: 384×640, 640×384, 512×512 |
| `--lcm` | 8 sampling steps instead of 20. Roughly halves the wait, slightly softer |
| `--image NAME.png` | animate an existing still instead of inventing one (see below) |
| `--frames N` | 16 is ~2 s of motion. Cost scales with this |
| `--interpolate N` | RIFE fills in frames. Default 2 → 16 fps out, at no sampler cost |
| `--no-loop` | drop the closed-loop context if a clip should not join end to start |
| `--seed N` | reproduce an earlier clip exactly |

### Animating a still from the pool

SparseCtrl pins an existing image into frame 0 and animates outward from it, so
framing you already like is preserved. Copy the picture into `~/ComfyUI/input/`
first, then:

```sh
~/ComfyUI/.venv/bin/python ~/ComfyUI/tabletop/gen.py \
  "gentle drifting light, subtle motion" --image my_picture.png --lcm
```

Pool items are stored under a content hash with no file extension, which
`LoadImage` will not open — convert to `.png` on the way in.

Two defaults change in this mode, both learned the hard way. The still is
pinned to **both ends** of the clip rather than only frame 0, and the closed
loop is **off**. Anchoring just the first frame lets everything after it drift
into a different subject — in testing, a painted elf ranger became an unrelated
hooded figure by frame 6 — and a closed loop pulls against the anchor on the
final frame. Override with `--anchor` and `--loop` if you want the old
behaviour.

Prompt for **what is already in the picture**, not what you would like added.
Asking for "drifting torchlight" over a figure that has none invents the
torchlight and discards the figure.

## What to expect for speed

Measured on an M1 Pro / 32 GB, 16 frames at 384×640:

| | sampling |
|---|---|
| 20 steps, no LCM | ~11 min |
| 8 steps, `--lcm` | ~4.7 min |

A CUDA machine is several times faster. CPU-only is slow enough to feel broken —
the installer warns if that is what you are about to get.

## Extra models

`./install.sh --extras` fetches ~20 GB more, none of which the stack needs:

| | |
|---|---|
| **LTX-Video 2B** (0.9.8 distilled) + T5 encoder | text-to-video, ~8 steps |
| **SVD img2vid-xt** | image-to-video, purpose-built for it |

They exist to answer the question the AnimateDiff models cannot: what better
motion costs. One measurement so far, on the M1 Pro — about a second of video
at 512x512:

| | time | motion |
|---|---|---|
| AnimateDiff + LCM | 101 s | subtle flicker; loops seamlessly |
| LTX-Video 2B | **45 s** | genuine frame-to-frame movement |

So LTX is both faster and visibly better here. What it does not do is close a
loop: AnimateDiff's looped context is why a clip can play forever on a screen
without a visible seam, and nothing in LTX replaces that yet. Neither extra is
wired into the content server — `server/generate.py` still builds AnimateDiff
graphs, and switching it over is an open decision rather than an oversight.

SVD is untested so far. Note that `svd_xt_1_1` is gated on HuggingFace and
needs an account; the manifest pins the original `svd_xt`, which is not.

## Wan 2.2 — a still, animated, in a real loop

`./install.sh --wan` (or `bootstrap.py --wan`) fetches ~26 GB more. It is the
only thing here that animates an existing picture **and** closes the loop.

The gap it fills is the one the table above describes: AnimateDiff loops but
barely moves, LTX moves but seams, and a screen that plays forever needs both.
Wan 2.2 gets there by construction rather than by scheduling —
`WanFirstLastFrameToVideo` takes a first *and* a last frame, so handing it the
**same picture twice** makes the loop exact.

| | |
|---|---|
| `Wan2.2-I2V-A14B-{High,Low}Noise-Q4_K_M.gguf` | 9.2 GB each |
| `umt5_xxl_fp8_e4m3fn_scaled` | 6.4 GB — Wan reads its prompt with umt5, not CLIP |
| `wan_2.1_vae` | 242 MB |
| `Wan2.2-Lightning_I2V-A14B-4steps` LoRAs, high + low | 585 MB each |

Both experts are needed on disk — A14B is a mixture of experts, high-noise for
the early steps and low-noise for the rest — but only one is resident at a
time, which is why ~9 GB of weights runs on a 12 GB card. Q4_K_M measures
around 10.5 GB of VRAM in practice. Drop to `Q4_K_S` (0.9 GB smaller per
expert) if that is too tight; `Q5_K_M` wants 16 GB.

GGUF needs `ComfyUI-GGUF` for `UnetLoaderGGUF`, which is why `--wan` installs a
node pack of its own. Nothing else here reads GGUF, so a plain install does
not get it.

Two things to know before spending the download:

- **Untested in this repo.** The pins, hashes and sizes are all verified
  against HuggingFace, and every URL answers 200 with a byte-exact length. The
  graph is not — validate it with ComfyUI's own Wan 2.2 I2V template first,
  swapping in the first/last-frame node with one picture in both slots.
- **Drop the duplicate frame.** First and last are the same image, so keeping
  both puts the same frame twice at the seam. The ComfyUI templates use an
  image selector to trim the last one. Same class of off-by-one as RIFE
  returning `2N-1`, noted below.

### The cheap alternative, which needs no download

`--pingpong` writes the clip out forwards and then backwards, so it joins end
to start whatever the sampler did. It costs nothing, works with any model, and
is now available in three places: `gen.py --pingpong`, a **Seamless** checkbox
on the generate page, and a `pingpong` pref on the content server, which
applies it when a clip is built for a screen and so covers uploaded GIFs and
video too.

It is only honest for motion with no direction — fire, water, drifting light,
smoke seen head-on. Anything that travels (rain, a walk, a pan) runs backwards
for half the loop and reads as wrong. Where it does fit, it is strictly better
than paying 26 GB.

## Pins, and updating them

`manifest.toml` pins ComfyUI and every node pack to a commit, and every model to
a SHA-256. This is deliberate: both ComfyUI and the AnimateDiff nodes move
quickly, and an unpinned install reproduces only by luck — the failure mode is a
machine set up months later quietly getting a different, broken combination.

To move forward:

```sh
./install.sh --update     # rewrites the pins to current upstream
./install.sh              # install them
#                           ...then actually generate a clip to confirm
git diff manifest.toml    # review, then commit
```

A model whose hash no longer matches is re-downloaded once, then reported as an
error rather than used — upstream replacing a file silently is exactly the kind
of thing worth failing loudly on.

## Platform status

**macOS on Apple Silicon is tested** — this is where it was built and verified.

**Windows is written but untested.** The logic is shared with macOS, so what is
unproven is narrow: NVIDIA detection via `nvidia-smi`, the CUDA wheel index, and
`start.cmd`. If the torch install fails to find a wheel, the CUDA suffix in
`manifest.toml` under `[torch]` is the line to change — it must match the
installed driver. Please report what breaks.

**Linux** should work through `install.sh` on the same code path as Windows
(CUDA or CPU wheels), but has not been run either.

## Notes

- The first `--interpolate` run downloads RIFE's weights (~50 MB) into
  `models/frame_interpolation/`. That one is fetched on demand by the node pack
  rather than listed in the manifest.
- `PYTORCH_ENABLE_MPS_FALLBACK=1` is set by `start.sh`. A few operations have no
  MPS kernel; without the fallback they abort the sampler partway through.
- RIFE returns `2N-1` frames, not `2N` — it interpolates between frames, not
  past the last one. A looping clip therefore has one slightly quicker step at
  the seam. Use `--interpolate 1` if that ever shows.
