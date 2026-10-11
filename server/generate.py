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

import hashlib
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import video
from collections import OrderedDict
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

# A background is a PLACE. The usual negative prompt does not stop the model
# putting a hero in the middle of it, and one arrives uninvited surprisingly
# often -- which is the one thing this picture must not contain, since a
# character is composited in later.
BACKGROUND_NEGATIVE = ("person, people, man, woman, character, figure, "
                       "humanoid, creature, monster, adventurer, knight, "
                       "silhouette, crowd, portrait, hands, face, body, "
                       "statue of a person, clutter in the centre, "
                       "object blocking the centre, worst quality, low "
                       "quality, blurry, jpeg artifacts, watermark, text, "
                       "signature, pitch black, unreadable darkness, "
                       "flat lighting, evenly lit, washed out, low contrast, "
                       "busy, cluttered, intricate detail, fine texture, "
                       "visual noise, high frequency detail, brickwork "
                       "texture, objects in the centre")

# Appended to every background, after the scene and the style.
#
# This is OUR half of the prompt, and it answers two constraints the person
# typing a scene should not have to think about.
#
# A CHARACTER STANDS IN FRONT OF IT.
#   "empty, nobody"            a negative alone does not stop SD1.5 putting an
#                              adventurer in a dungeon; the word pulls one in,
#                              and saying what the picture IS beats listing
#                              what it is not.
#   "clear floor across the centre"   he needs somewhere to stand, and a
#                              pillar dead centre leaves him nowhere.
#   "eye level, human scale"   he fills most of the height, so a wide
#                              establishing shot would put him out of scale.
#   "light from the sides"     side light separates a figure from what is
#                              behind him; flat frontal light flattens him
#                              into it.
#   "centre receding into soft shadow"   he is read as a silhouette first, and
#                              a dark, quiet middle is what he reads against.
#
# IT IS SEEN AT 240-480 PIXELS.
#   "simple composition, few large shapes, low detail"
#                              ornament and texture do not survive the
#                              downscale -- they become noise, and noise next
#                              to a figure is what makes a screen look busy
#                              rather than atmospheric. Shapes survive;
#                              filigree does not.
# Every word here is spent out of the same 77-token budget as the scene, so
# each one has to earn its place. "plain untextured surfaces" and "strong
# contrast" were cut: the negative already forbids texture and flatness, and
# saying it twice only crowds out what the person actually typed.
BACKGROUND_COMPOSITION = ("empty, nobody, clear floor across the centre, "
                          "eye level, human scale, simple composition, few "
                          "large shapes, one bright light from the side, "
                          "dim middle ground")

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
    # "subject" is not another point on the denoise dial -- it steps off it.
    #
    # Denoise is GLOBAL: it trades the reference against the prompt for every
    # pixel at once. At 0.32 the figure survives and the words do nothing; at
    # 0.55 the words land and the figure is replaced -- measurably, a
    # miniature came back with a different face, no goggles and no green
    # boots. There is no value that keeps the character AND builds a scene
    # around him, because one number cannot say two things.
    #
    # So this level masks instead. The subject is cut out by flooding the
    # backdrop in from the borders, and only what is left is regenerated, at
    # denoise 1.0 -- the prompt gets full authority over the scene and none at
    # all over him. His pixels are not sampled; they are composited.
    "subject":  {"denoise": 1.0, "control": 1.0, "inpaint": True},
    "close":    {"denoise": 0.32, "control": 1.0},
    "balanced": {"denoise": 0.55, "control": 1.0},
    "loose":    {"denoise": 0.75, "control": 0.8},
}
DEFAULT_KEEP = "subject"

# Cut-out tuning. The backdrop in a product shot is pure white; a painted
# model is not. Measured across the pool, ~70% of pixels sit at min-channel
# 240+ and essentially none in the band just below, so these thresholds sit in
# an empty gap rather than on a judgement call.
WHITE_FLOOR = 238     # min channel at or above this is backdrop-bright
WHITE_SPREAD = 12     # ...and this neutral, so a pale YELLOW is not backdrop
PINHOLE = 5           # closing kernel, to survive specular highlights

# Prompts that actually produced good output on an RTX 4070 Ti, offered as real
# editable text rather than as placeholder grey.
#
# The split matters more than the wording. A SCENE prompt says what the picture
# contains and belongs on a verify, where the still is being built. A MOTION
# prompt says what moves and belongs on a clip, where the picture already
# settles how everything looks and the only open question is what animates.
# Describing the scene again on a clip wastes the prompt on things the image has
# already decided.
# ----------------------------------------------------------------- styles
#
# A style is a house look applied to every background, so two scenes generated
# weeks apart still belong on the same shelf. It is kept separate from the
# scene text because the two answer different questions: the scene says WHAT
# the place is, the style says how it should be drawn.
STYLES = {
    "dnd": {
        "label": "D&D",
        # "detailed environment" used to live here and had to go: it argues
        # with the composition's "simple, few large shapes, low detail", and
        # detail is the first thing lost at 240 pixels anyway.
        "prompt": "dungeons and dragons illustration, painterly, rich colour",
    },
    "isometric": {
        "label": "Isometric game",
        "prompt": "isometric game art, three-quarter view, clean stylised "
                  "shapes, readable silhouettes",
    },
    "own": {
        # Adds nothing. For when the scene text already says how it should
        # look and a house style would only argue with it.
        "label": "Roll your own",
        "prompt": "",
    },
}
DEFAULT_STYLE = "dnd"

# A background's own sampler settings, and NOT the clip's.
#
# A clip runs on the LCM LoRA at 8 steps because 16-32 frames at 20 steps is a
# wait rather than a loop. A background is one picture, generated once and then
# reused for weeks, so the bargain has nothing to buy: it costs a couple of
# seconds either way and every clip made from it inherits whatever it is.
# How far a refine is allowed to travel from the picture it started on.
#
# Its own scale, NOT the character flow's KEEP levels. Those are tuned for
# "restyle this figure", where half the picture changing is the point. Refining
# a place is the opposite: the room is already right and the answer is "that,
# with a door" -- at 0.55 the model repaints enough that the result is a
# sibling rather than a revision, which is not refining at all.
REFINE_LEVELS = {
    "nudge":  {"label": "a nudge — barely moves", "denoise": 0.22},
    "change": {"label": "a change — keeps the room", "denoise": 0.35},
    "rework": {"label": "a rework — same idea, redrawn", "denoise": 0.55},
}
DEFAULT_REFINE = "change"

CUTOUT_TIMEOUT = 60

# Taking the camera push out of a loop (see unzoom). The search is coarse on
# purpose: a quarter of a per cent is finer than anyone can see, and matching
# at 96x96 is enough to find a whole-frame scale while costing nothing. FLOOR
# is where a measured drift is small enough to leave alone rather than
# re-encode the clip for.
UNZOOM_STEP = 0.0025
UNZOOM_RANGE = 0.12
UNZOOM_GRID = 128
UNZOOM_FLOOR = 0.005
UNZOOM_PASSES = 2

# Closing a loop by fading its end into its start (see _crossfade_loop). A
# quarter of the clip, never fewer than 6 frames: 6 was where the seam on a
# 1 s clip stopped being findable (1.3x a normal step; 4 left it at 1.9x).
# 17 frames -> 11, 33 -> 25, 49 -> 37.
LOOP_OVERLAP_MIN = 6
LOOP_OVERLAP_DIV = 4

BACKGROUND_STEPS = 24
BACKGROUND_CFG = 7.0
BACKGROUND_SAMPLER = "dpmpp_2m"
BACKGROUND_SCHEDULER = "karras"

# What moves in a loop. Deliberately small things: a background that pans or
# changes is a video, and a screen showing it for an hour wants a place that
# is alive rather than a place that is going somewhere.
DEFAULT_MOTION = ("drifting smoke, flickering firelight, dust motes in the "
                  "light, leaves stirring")
# What a background description has to do HERE.
#
# Not "paint a nice dungeon". These are backdrops for a cut-out character on a
# 240-480 pixel screen, and that changes every answer:
#
#   1. NAME THE PLACE, PLAINLY. One or two words of it. A long description
#      buys detail that is gone by 240 pixels.
#   2. NAME ONE LIGHT, AND WHERE IT IS. Light from the side or behind is what
#      separates a figure from what is behind him. Lighting named as mood
#      ("torchlit", "dramatic") does not place anything and gets outvoted.
#   3. SAY WHAT THE GROUND IS. He stands on it, and a floor that reads is
#      what stops him floating.
#   4. LET THE BACK FALL AWAY. Shadow, mist or distance behind the middle:
#      it gives depth without detail, and a dark middle is what a character
#      reads against.
#
# What NOT to ask for: texture, ornament, props near the middle, anything
# intricate. All of it turns to noise at this size and competes with the one
# thing the screen is actually showing.
DEFAULT_SCENE = ("a quiet stone chamber, one warm light from the side, plain "
                 "stone floor, the far wall falling away into soft shadow")

# Clicked together rather than typed. Each is one of the four jobs above, so
# a description can be built by picking one from each row.
SCENE_BLOCKS = [
    {"group": "Light", "label": "Torches",
     "text": "iron torches burning in wall brackets"},
    {"group": "Light", "label": "Hearth",
     "text": "a fire burning in a stone hearth, warm firelight"},
    {"group": "Light", "label": "Daylight",
     "text": "shafts of daylight through high windows"},
    {"group": "Light", "label": "Moonlight",
     "text": "cold moonlight through a broken roof"},
    {"group": "Surfaces", "label": "Stone",
     "text": "rough stone walls, worn flagstone floor"},
    {"group": "Surfaces", "label": "Timber",
     "text": "timber beams, plank floor, whitewashed walls"},
    {"group": "Surfaces", "label": "Earth",
     "text": "packed earth floor, roots and damp soil walls"},
    {"group": "Edges", "label": "Barrels",
     "text": "barrels and crates stacked against the walls"},
    {"group": "Edges", "label": "Furniture",
     "text": "a long table and benches pushed to the sides"},
    {"group": "Edges", "label": "Rubble",
     "text": "fallen rubble and broken columns along the edges"},
    {"group": "Depth", "label": "Archway",
     "text": "an archway at the far end opening into darkness"},
    {"group": "Depth", "label": "Stairs",
     "text": "a stone stair rising in the distance"},
]

# Backgrounds get their OWN examples. The character ones begin "standing in a
# torchlit dungeon", which is written from the figure's point of view -- and
# asking for someone standing there is precisely how a person ends up in a
# picture that must not contain one. These name a place and nothing else.
SCENE_EXAMPLES = [
    {"label": "Dungeon corridor",
     "text": "an empty torchlit dungeon corridor, damp stone walls, iron "
             "braziers on the walls, scattered rubble, flagstone floor"},
    {"label": "Tavern room",
     "text": "the empty back room of a tavern, timber beams, a fire in the "
             "hearth, barrels and benches against the walls, wooden floor"},
    {"label": "Forest clearing",
     "text": "a quiet forest clearing at dusk, tall pines around the edges, "
             "moss and ferns, soft mist, open ground in the middle"},
    {"label": "Ruined hall",
     "text": "a ruined stone hall open to the sky, broken columns to the "
             "sides, ivy, shafts of light, rubble-strewn floor"},
]

MOTION_EXAMPLES = [
    {"label": "Fire and smoke",
     "text": "flickering firelight, drifting smoke, embers rising"},
    {"label": "Leaves and wind",
     "text": "leaves moving in the wind, grass swaying, drifting pollen"},
    {"label": "Water",
     "text": "water rippling, slow reflections shifting, mist drifting"},
    {"label": "Dust and air",
     "text": "dust motes floating in shafts of light, slow drifting haze"},
]

PROMPT_EXAMPLES = [
    {"label": "Torchlit dungeon",
     "kind": "scene",
     "text": "standing in a torchlit dungeon, damp stone walls, flickering "
             "braziers, drifting smoke and fog, warm light spilling from the braziers onto him, cinematic"},
    {"label": "Moonlit forest",
     "kind": "scene",
     "text": "standing in a moonlit forest clearing, tall pines, drifting mist, "
             "fireflies, cool moonlight falling on him from above, cinematic"},
    {"label": "Ambient motion",
     "kind": "motion",
     "text": "flickering torchlight, drifting smoke and fog, embers rising, "
             "subtle breathing, slight head turn, eyes blink"},
    {"label": "Wind and leaves",
     "kind": "motion",
     "text": "leaves moving in the wind, drifting mist, cloth and hair stirring, "
             "subtle breathing, eyes blink"},
]


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


def _subject_mask(src, white: int = 0, spread: int = 0):
    """White where the subject is, black where the backdrop is.

    Two tests, because one is not enough:

    * FLOOD from the four corners. Takes whatever is connected to the border,
      which is most of a product shot's backdrop, and -- unlike a brightness
      test -- leaves pale parts of the figure alone. A cream shirt survives.
    * PAPER: pixels that are near-white AND near-neutral, anywhere in the
      frame. This is what the flood cannot reach: the gap between the legs,
      the hole under an arm, the inside of a clear base. Those regions are
      genuinely backdrop but are walled off from the border, and leaving them
      in is what put a white streak down the middle of a figure.

    The paper test is safe on these photos because the backdrop is pure white
    and the figures are not: measured across the pool, 70% of pixels sit at
    min-channel 240+ and essentially none in the band just below, so the two
    populations do not overlap.

    A closing pass then fills pinholes, because a specular highlight on a
    painted model is also near-white and would otherwise punch through him.
    """
    from PIL import Image as PILImage, ImageChops, ImageDraw, ImageFilter, ImageOps

    sw, sh = src.size
    probe = src.copy()
    sentinel = (255, 0, 255)
    for xy in ((0, 0), (sw - 1, 0), (0, sh - 1), (sw - 1, sh - 1)):
        ImageDraw.floodfill(probe, xy, sentinel, thresh=40)
    r, g, b = probe.split()
    flood = ImageChops.multiply(
        ImageChops.multiply(r.point(lambda v: 255 if v == 255 else 0),
                            g.point(lambda v: 255 if v == 0 else 0)),
        b.point(lambda v: 255 if v == 255 else 0))

    white = white or WHITE_FLOOR
    spread = spread or WHITE_SPREAD
    sr, sg, sb = src.split()
    lo = ImageChops.darker(ImageChops.darker(sr, sg), sb)
    hi = ImageChops.lighter(ImageChops.lighter(sr, sg), sb)
    paper = ImageChops.multiply(
        lo.point(lambda v: 255 if v >= white else 0),
        ImageChops.difference(hi, lo).point(lambda v: 255 if v <= spread else 0))

    subject = ImageOps.invert(ImageChops.lighter(flood, paper))
    return (subject.filter(ImageFilter.MaxFilter(PINHOLE))
                   .filter(ImageFilter.MinFilter(PINHOLE)))


def _row_spans(px, x0: int, x1: int, y: int, shortest: int = 4) -> list[int]:
    """Widths of the separate runs of subject across one row."""
    out: list[int] = []
    start = None
    for x in range(x0, x1):
        on = px[x, y] > 127
        if on and start is None:
            start = x
        elif not on and start is not None:
            if x - start >= shortest:
                out.append(x - start)
            start = None
    if start is not None and x1 - start >= shortest:
        out.append(x1 - start)
    return out


def _baseplate_top(subject, box, flare: float = 1.8, plateau: float = 0.80,
                   max_frac: float = 0.45) -> int | None:
    """Row where the display base starts, in image coordinates, or None.

    Gaming miniatures are sold on a moulded disc and photographed on it. On a
    screen that disc says "a model on a table" when the scene says dungeon,
    and gives the figure a hard edge to stand on instead of the floor.

    Found by shape, because bases come black, clear, sculpted and flocked:

    * ONE SPAN. Legs cross a row as two separate runs; a disc crosses it as
      one. This is the signal that actually separates them -- width alone does
      not, because a crouching figure's legs splay as they descend and get
      wider all the way down.
    * NEAR ITS WIDEST. The first continuous row is not yet the disc; it is
      where the ankles merge. Waiting for the width to reach most of its
      maximum lands on the disc proper.

    Returns None unless both agree and the result is a plausible height, so a
    photograph without a base is left alone.
    """
    x0, y0, x1, y1 = box
    px = subject.load()
    n = y1 - y0
    if n < 20:
        return None

    rows = [_row_spans(px, x0, x1, y0 + i) for i in range(n)]
    widest = [max(r) if r else 0 for r in rows]
    low = int(n * (1.0 - max_frac))
    if low >= n:
        return None
    wmax = max(widest[low:], default=0)
    if wmax <= 0:
        return None

    disc = [i for i in range(low, n)
            if len(rows[i]) == 1 and widest[i] >= plateau * wmax]
    if not disc:
        return None
    top = min(disc)

    # Compare against the figure above: the rows that are still several
    # separate limbs, which is what a base is wide relative to.
    above = [widest[i] for i in range(int(n * 0.30), top) if len(rows[i]) > 1]
    if not above:
        return None
    legs = sorted(above)[len(above) // 2]
    if legs <= 0 or wmax < legs * flare or (n - top) > n * max_frac:
        return None
    return y0 + top


def subject_shape(body: bytes, drop_base: bool = False) -> tuple[int, int]:
    """The cut-out subject's own width and height, for choosing a canvas.

    "auto" otherwise reads the PHOTO's proportions, which say nothing about
    the figure: a miniature is shot square whatever its pose, so a wide figure
    with its arms out lands on a portrait panel and can only be scaled until
    its elbows touch the sides -- filling the width and leaving a third of the
    height empty. Measuring him instead picks the shape he actually is.
    """
    from PIL import Image as PILImage

    src = PILImage.open(io.BytesIO(body)).convert("RGB")
    subject = _subject_mask(src)
    box = subject.getbbox()
    if box is None:
        return src.size
    if drop_base:
        cut = _baseplate_top(subject, box)
        if cut is not None and cut > box[1]:
            box = (box[0], box[1], box[2], cut)
    return max(1, box[2] - box[0]), max(1, box[3] - box[1])


def isolate_subject(body: bytes, width: int, height: int,
                    fill: float = 0.88,
                    drop_base: bool = False) -> tuple[bytes, bytes]:
    """Cut the subject out and stand it on a blank canvas of the panel's shape.

    Returns (canvas PNG, mask PNG). White in the mask is what gets
    regenerated: everything except the subject.
    """
    from PIL import Image as PILImage, ImageDraw, ImageFilter, ImageOps

    src = PILImage.open(io.BytesIO(body)).convert("RGB")
    subject = _subject_mask(src)
    box = subject.getbbox()
    if box is None:
        subject = PILImage.new("L", src.size, 255)
        box = (0, 0, src.size[0], src.size[1])

    dropped = False
    if drop_base:
        cut = _baseplate_top(subject, box)
        if cut is not None and cut > box[1]:
            # Cut, not faded. A gradient looked kinder on the cut-out, but it
            # leaves the disc's own pixels semi-transparent, and the composite
            # then paints them back at partial strength -- a clear base
            # reappears as a bright white platform under his feet. The disc
            # has to be gone entirely and the floor generated in its place.
            ImageDraw.Draw(subject).rectangle(
                [box[0], cut, box[2], subject.height], fill=0)
            box = subject.getbbox() or box
            dropped = True

    # Pull the edge in by a pixel. A photographed edge is anti-aliased against
    # the white backdrop, so the outermost pixels are part figure and part
    # paper. Composited over a dark scene those read as a bright outline --
    # the halo that makes a cut-out look stuck on rather than standing in it.
    subject = subject.filter(ImageFilter.MinFilter(3))

    fig = src.copy()
    fig.putalpha(subject)
    fig = fig.crop(subject.getbbox() or box)

    # Fit BOTH axes: a figure with its arms out is wider than it is tall for
    # its height, and scaling by height alone walks it off the canvas. The
    # width is allowed closer to the edge, because what reaches it is a sword
    # tip or an elbow, and framing to keep those comfortably inside leaves him
    # small in the middle of a lot of nothing.
    scale = min(height * fill / fig.height, width * 0.96 / fig.width)
    fig = fig.resize((max(1, int(fig.width * scale)),
                      max(1, int(fig.height * scale))), PILImage.LANCZOS)

    canvas = PILImage.new("RGB", (width, height), (128, 128, 128))
    px = (width - fig.width) // 2
    # Stood near the bottom, not centred. With the base gone his soles ARE the
    # bottom of him, so he goes lower still and the generated floor meets them.
    py = int(height * (0.97 if dropped else 0.93)) - fig.height
    canvas.paste(fig, (px, py), fig)

    alpha = PILImage.new("L", (width, height), 0)
    alpha.paste(fig.getchannel("A"), (px, py))
    mask = ImageOps.invert(alpha).filter(ImageFilter.GaussianBlur(1.5))

    out_c, out_m = io.BytesIO(), io.BytesIO()
    canvas.save(out_c, format="PNG")
    mask.save(out_m, format="PNG")
    return out_c.getvalue(), out_m.getvalue()


BG_REMOVAL_MODEL = "birefnet.safetensors"

# Cut-outs are cached by the bytes that produced them. Moving a slider
# re-composites but does not re-cut: the mask does not change when a figure
# moves, and paying two seconds of GPU per nudge would make the preview
# useless.
_CUTOUTS: "OrderedDict[str, bytes]" = OrderedDict()
_CUTOUT_MAX = 24


def cutout(body: bytes) -> bytes | None:
    """Freistellen by model. Returns RGBA PNG bytes, or None if it could not.

    ComfyUI has this built in -- LoadBackgroundRemovalModel into
    RemoveBackground -- so there is no node pack and no extra Python
    dependency, only the weights.

    It replaces a flood-fill-and-near-white test that worked only on product
    shots and could not reason about the hard cases. BiRefNet removes a CLEAR
    plastic base outright, which no colour rule can: translucent plastic is
    neither backdrop nor figure. It does NOT remove an opaque base -- it reads
    a miniature on a solid disc as one object -- which is why the baseplate
    detection downstream still earns its place.

    Returns None rather than raising when ComfyUI is unreachable, so the caller
    can fall back instead of the page breaking.
    """
    key = hashlib.sha256(body).hexdigest()
    if key in _CUTOUTS:
        _CUTOUTS.move_to_end(key)
        return _CUTOUTS[key]

    from PIL import Image as PILImage

    try:
        name = upload_reference(body, f"cut_{key[:12]}.png")
        graph = {
            "load": {"class_type": "LoadBackgroundRemovalModel",
                     "inputs": {"bg_removal_name": BG_REMOVAL_MODEL}},
            "img": {"class_type": "LoadImage", "inputs": {"image": name}},
            "cut": {"class_type": "RemoveBackground",
                    "inputs": {"bg_removal_model": ["load", 0], "image": ["img", 0]}},
            # The mask comes back as a MASK; saving it as an image is the
            # simplest way to get the pixels out over HTTP.
            "vis": {"class_type": "MaskToImage", "inputs": {"mask": ["cut", 0]}},
            "out": {"class_type": "SaveImage",
                    "inputs": {"images": ["vis", 0], "filename_prefix": "cutout"}},
        }
        payload = json.dumps({"prompt": graph, "client_id": uuid.uuid4().hex}).encode()
        req = urllib.request.Request(f"{COMFY_URL}/prompt", payload,
                                     {"Content-Type": "application/json"})
        prompt_id = json.load(urllib.request.urlopen(req, timeout=30))["prompt_id"]

        mask_png = None
        deadline = time.time() + CUTOUT_TIMEOUT
        while time.time() < deadline:
            time.sleep(0.4)
            with urllib.request.urlopen(
                    f"{COMFY_URL}/history/{prompt_id}", timeout=20) as r:
                hist = json.load(r)
            if prompt_id not in hist:
                continue
            for out in hist[prompt_id].get("outputs", {}).values():
                for f in out.get("images", []):
                    mask_png = _fetch_output(f.get("filename", ""),
                                             f.get("subfolder", ""))
                    break
            break
        if not mask_png:
            log.warning("cut-out produced nothing for %s", key[:8])
            return None

        src = PILImage.open(io.BytesIO(body)).convert("RGB")
        mask = PILImage.open(io.BytesIO(mask_png)).convert("L")
        if mask.size != src.size:
            mask = mask.resize(src.size, PILImage.LANCZOS)
        src.putalpha(mask)
        buf = io.BytesIO()
        src.save(buf, format="PNG")
        out_bytes = buf.getvalue()
    except (NoComfyUI, urllib.error.URLError, OSError, TimeoutError,
            KeyError, ValueError) as exc:
        log.warning("cut-out failed: %s", exc)
        return None

    _CUTOUTS[key] = out_bytes
    while len(_CUTOUTS) > _CUTOUT_MAX:
        _CUTOUTS.popitem(last=False)
    return out_bytes


# Wan 2.2 image-to-video, used to animate a PLACE and nothing else.
#
# The character is never in this picture. That is the whole point: every
# attempt to animate a finished scene moved the figure as well -- a model
# given a man in a room will animate the man -- and no amount of prompting
# held him still, because holding him still is not what an image-to-video
# model does. Animating the empty room instead and pasting the cut-out onto
# each frame afterwards makes him byte-identical in every frame. He cannot
# drift, because nothing ever asks him to.
WAN_HIGH = "Wan2.2-I2V-A14B-HighNoise-Q4_K_M.gguf"
WAN_LOW = "Wan2.2-I2V-A14B-LowNoise-Q4_K_M.gguf"
WAN_ENCODER = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
WAN_VAE = "wan_2.1_vae.safetensors"
WAN_LORA_HIGH = "Wan2.2-Lightning_I2V-A14B-4steps-lora_HIGH_fp16.safetensors"
WAN_LORA_LOW = "Wan2.2-Lightning_I2V-A14B-4steps-lora_LOW_fp16.safetensors"

# cfg 1: what the Lightning LoRAs are trained for.
WAN_CFG = 1.0
WAN_FPS = 16

# The camera lock. At cfg 1 the negative prompt is not weakened, it is
# IGNORED -- classifier-free guidance at 1 never evaluates it -- so "camera
# movement, dolly, zoom" in WAN_NEGATIVE did nothing for as long as the graph
# ran Lightning end to end. The first steps are where Wan lays out the motion,
# so those run on the plain high-noise expert, no Lightning, at a real cfg,
# where the negative counts. Lightning then finishes as before.
#
# Measured on a corridor -- the worst case, since a vanishing point invites a
# walk down it -- with the same seed: camera push 12.0% -> 0.0%, and what was
# left moving was the flames, the lantern and the glow on the floor, where
# before it was every wall. About 60% slower (169 s against 106 s for 1 s).
WAN_STEPS = 6
WAN_LOCK_STEPS = 2
WAN_LOCK_CFG = 3.5
WAN_HANDOFF = 3          # high-noise expert hands over to low-noise here

# Clip lengths, in Wan's blocks of 4 frames plus one. Short on purpose: these
# are ambient loops, and the longer a clip runs the further the place drifts
# from the picture it started as.
WAN_LENGTHS = {"1s": 17, "2s": 33, "3s": 49}
DEFAULT_WAN_LENGTH = "2s"

WAN_NEGATIVE = ("people, characters, camera movement, camera pan, camera "
                "zoom, dolly, tracking shot, parallax, drifting camera, "
                "scene sliding, cuts, text, watermark, worst quality, "
                "blurry, distortion")

# Added to whatever motion was asked for.
#
# Wan drifts the camera: left alone it gives a slow push in, which ping-pong
# then plays forwards and backwards, so the whole room appears to breathe in
# and out. Forbidding it in the negative was not enough -- as with the
# torches, saying plainly what the shot IS beats listing what it is not. The
# second half matters as much as the first: the model still has to move
# something, and naming what rules out "move the camera" as an answer.
WAN_CAMERA = ("static locked-off camera, fixed framing, tripod shot, "
              "the room itself perfectly still, only things within it moving")


def background_clip_graph(image: str, motion: str, length: str = DEFAULT_WAN_LENGTH,
                          seed: int = 0, pingpong: bool = False) -> dict:
    """Animate a place. The picture is the first frame; the words say what moves.

    Start-image only, deliberately. Pinning the LAST frame to the same picture
    closes the loop by telling the model nothing should change, and it obeys:
    measured, that cut the motion to a sixth (0.52 against 3.25 per frame).

    PING-PONG IS OFF. It closes the loop beautifully -- the seam measured at
    1.96x a normal frame step against 7.67x -- but it also plays Wan's slow
    camera push-in forwards and then backwards, which is what made the whole
    room appear to breathe in and out, continuously, for as long as the screen
    was on. Nothing talked the model out of that drift (twice through the
    prompt) and vidstab only took 7% off it, because a smooth monotonic push
    is the camera path it is built to preserve rather than the shake it is
    built to remove. A seam once per loop is the better of the two faults: it
    happens once, where the pulsing never stops.

    A14B is a mixture of experts and both halves run: the high-noise model
    takes the early steps and hands the latent to the low-noise one mid-sample,
    each with its own matching Lightning LoRA. Three samplers rather than two:
    the high-noise expert runs twice, first bare at real cfg (see
    WAN_LOCK_STEPS -- that is what holds the camera) and then with Lightning.
    """
    frames = WAN_LENGTHS.get(length, WAN_LENGTHS[DEFAULT_WAN_LENGTH])
    return {
        "unet_hi": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": WAN_HIGH}},
        "unet_lo": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": WAN_LOW}},
        "lora_hi": {"class_type": "LoraLoaderModelOnly",
                    "inputs": {"model": ["unet_hi", 0], "lora_name": WAN_LORA_HIGH,
                               "strength_model": 1.0}},
        "lora_lo": {"class_type": "LoraLoaderModelOnly",
                    "inputs": {"model": ["unet_lo", 0], "lora_name": WAN_LORA_LOW,
                               "strength_model": 1.0}},
        "clip": {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": WAN_ENCODER, "type": "wan"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": WAN_VAE}},
        "pos": {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["clip", 0],
                           "text": f"{motion.strip()}, {WAN_CAMERA}"}},
        "neg": {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["clip", 0], "text": WAN_NEGATIVE}},
        "img": {"class_type": "LoadImage", "inputs": {"image": image}},
        "size": {"class_type": "GetImageSize", "inputs": {"image": ["img", 0]}},
        "i2v": {"class_type": "WanImageToVideo",
                "inputs": {"positive": ["pos", 0], "negative": ["neg", 0],
                           "vae": ["vae", 0],
                           "width": ["size", 0], "height": ["size", 1],
                           "length": frames, "batch_size": 1,
                           "start_image": ["img", 0]}},
        "k_lock": {"class_type": "KSamplerAdvanced",
                   "inputs": {"model": ["unet_hi", 0],
                              "positive": ["i2v", 0], "negative": ["i2v", 1],
                              "latent_image": ["i2v", 2],
                              "add_noise": "enable", "noise_seed": seed,
                              "steps": WAN_STEPS, "cfg": WAN_LOCK_CFG,
                              "sampler_name": "euler", "scheduler": "simple",
                              "start_at_step": 0, "end_at_step": WAN_LOCK_STEPS,
                              "return_with_leftover_noise": "enable"}},
        "k_hi": {"class_type": "KSamplerAdvanced",
                 "inputs": {"model": ["lora_hi", 0],
                            "positive": ["i2v", 0], "negative": ["i2v", 1],
                            "latent_image": ["k_lock", 0],
                            "add_noise": "disable", "noise_seed": seed,
                            "steps": WAN_STEPS, "cfg": WAN_CFG,
                            "sampler_name": "euler", "scheduler": "simple",
                            "start_at_step": WAN_LOCK_STEPS,
                            "end_at_step": WAN_HANDOFF,
                            "return_with_leftover_noise": "enable"}},
        "k_lo": {"class_type": "KSamplerAdvanced",
                 "inputs": {"model": ["lora_lo", 0],
                            "positive": ["i2v", 0], "negative": ["i2v", 1],
                            "latent_image": ["k_hi", 0],
                            "add_noise": "disable", "noise_seed": seed,
                            "steps": WAN_STEPS, "cfg": WAN_CFG,
                            "sampler_name": "euler", "scheduler": "simple",
                            "start_at_step": WAN_HANDOFF, "end_at_step": 10000,
                            "return_with_leftover_noise": "disable"}},
        "decode": {"class_type": "VAEDecode",
                   "inputs": {"samples": ["k_lo", 0], "vae": ["vae", 0]}},
        "out": {"class_type": "VHS_VideoCombine",
                "inputs": {"images": ["decode", 0], "frame_rate": WAN_FPS,
                           "loop_count": 0, "filename_prefix": "placeloop",
                           "format": "video/h264-mp4",
                           "pingpong": pingpong, "save_output": True}},
    }


def compose_scene(background: bytes, character: bytes, *,
                  fill: float = 0.82, drop_base: bool = True,
                  separation: float = 0.55, shadow: float = 0.55,
                  offset: float = 0.0, baseline: float = 0.95,
                  trim: float = 0.0, white: int = 0,
                  ai_cutout: bool = True) -> bytes:
    """Stand a cut-out character in a background. Returns a PNG.

    No model involved, and that is the point. Every earlier attempt put the
    figure into the picture by INPAINTING around him, which hands the sampler a
    ring of pixels to invent along his outline -- and what it invents is glow.
    That halo was never a tuning problem; it was the method. Pasting onto a
    picture that already exists cannot produce one, and it takes milliseconds
    instead of a minute.

    What the treatment does, and why each part is needed:

    * SEPARATION. The background is dimmed and softened, and darkened further
      toward the edges. A figure is read as a silhouette first, and these
      panels are 240-480 pixels wide -- a busy, equally bright backdrop and a
      character occupy the same visual weight and the eye finds neither. This
      is also the control the model would not give us from the prompt: the
      dark part goes where we want it, every time, instead of where DreamShaper
      felt like putting it.

    * A SHADOW. Without one he hovers. It is a soft ellipse under his feet,
      not a cast shadow -- the light in the background is wherever the model
      put it, so a directional shadow would contradict it as often as not.

    `separation` and `shadow` are 0..1 so they can be a slider rather than an
    adjective. 0 turns each off entirely.
    """
    from PIL import Image as PILImage, ImageDraw, ImageEnhance, ImageFilter

    bg = PILImage.open(io.BytesIO(background)).convert("RGB")
    w, h = bg.size

    # -- the figure -------------------------------------------------------
    cut = cutout(character) if ai_cutout else None
    by_model = cut is not None
    if by_model:
        rgba = PILImage.open(io.BytesIO(cut)).convert("RGBA")
        src = rgba.convert("RGB")
        subject = rgba.getchannel("A")
    else:
        # Fallback, for when ComfyUI is not running. Flood-fill from the
        # borders plus a near-white test: fine on a product shot, hopeless on
        # anything else, which is why it is no longer the first choice.
        # `white` loosens or tightens what counts as backdrop here.
        src = PILImage.open(io.BytesIO(character)).convert("RGB")
        subject = _subject_mask(src, white=white)
    box = subject.getbbox()
    if box is None:
        subject = PILImage.new("L", src.size, 255)
        box = (0, 0, src.size[0], src.size[1])
    if drop_base:
        cut = _baseplate_top(subject, box)
        if cut is not None and cut > box[1]:
            ImageDraw.Draw(subject).rectangle(
                [box[0], cut, box[2], subject.height], fill=0)
            box = subject.getbbox() or box
    if trim > 0:
        # Manual trim from the bottom, after the automatic base detection has
        # had its go. A clear base defeats the detector -- translucent plastic
        # is neither backdrop nor figure -- so there has to be a way to simply
        # say where he ends.
        b = subject.getbbox()
        if b is not None:
            cut = int(b[3] - (b[3] - b[1]) * trim)
            ImageDraw.Draw(subject).rectangle(
                [b[0], cut, b[2], subject.height], fill=0)

    if not by_model:
        # Only for the fallback. A flood-fill edge still carries the pixels
        # that were half figure and half white paper, and those read as a
        # bright outline against a dark scene. A model's matte is already
        # soft and correct, and eroding it would eat real detail.
        subject = subject.filter(ImageFilter.MinFilter(3))
    fig = src.copy()
    fig.putalpha(subject)
    fig = fig.crop(subject.getbbox() or box)

    scale = min(h * fill / fig.height, w * 0.92 / fig.width)
    fig = fig.resize((max(1, int(fig.width * scale)),
                      max(1, int(fig.height * scale))), PILImage.LANCZOS)

    px = int((w - fig.width) / 2 + offset * w)
    # Where his feet land. The floor in a generated place is wherever the
    # model put it, so this has to be movable rather than assumed.
    py = int(h * baseline) - fig.height

    # -- the backdrop -----------------------------------------------------
    out = bg
    if separation > 0:
        # Blurred by a fraction of the frame, so it looks the same on a 240
        # panel as on an 800 one rather than being tuned to one of them.
        out = out.filter(ImageFilter.GaussianBlur(max(0.6, w / 400.0) * separation))
        out = ImageEnhance.Brightness(out).enhance(1.0 - 0.34 * separation)
        out = ImageEnhance.Color(out).enhance(1.0 - 0.22 * separation)

        # Vignette: a blurred white ellipse is the "keep" mask, so the corners
        # fall away and the middle -- where he stands -- stays readable.
        vig = PILImage.new("L", (w, h), 0)
        ImageDraw.Draw(vig).ellipse(
            [-w * 0.18, -h * 0.18, w * 1.18, h * 1.18], fill=255)
        vig = vig.filter(ImageFilter.GaussianBlur(min(w, h) * 0.18))
        darker = ImageEnhance.Brightness(out).enhance(1.0 - 0.45 * separation)
        out = PILImage.composite(out, darker, vig)

    # -- grounding --------------------------------------------------------
    if shadow > 0:
        sw = max(2, int(fig.width * 0.72))
        sh = max(2, int(fig.height * 0.07))
        sx = px + (fig.width - sw) // 2
        sy = py + fig.height - sh // 2
        blob = PILImage.new("L", (w, h), 0)
        ImageDraw.Draw(blob).ellipse([sx, sy, sx + sw, sy + sh],
                                     fill=int(190 * shadow))
        blob = blob.filter(ImageFilter.GaussianBlur(max(2.0, sh * 0.6)))
        black = PILImage.new("RGB", (w, h), (0, 0, 0))
        out = PILImage.composite(black, out, blob)

    out.paste(fig, (px, py), fig)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _push_in(frame, factor: float, size: tuple[int, int]):
    """The frame as it would look with the camera that much further in.

    A centre crop and a scale back up, in one resampling step: Pillow's `box`
    takes floats, so the crop is not rounded to whole pixels first. Only ever
    called with factor >= 1, because pushing in discards pixels where pulling
    out would need pixels the frame never had.
    """
    from PIL import Image as PILImage

    w, h = frame.size
    half_w, half_h = w / (2.0 * factor), h / (2.0 * factor)
    box = (w / 2 - half_w, h / 2 - half_h, w / 2 + half_w, h / 2 + half_h)
    return frame.resize(size, PILImage.LANCZOS, box=box)


def _zoom_ramp(frames) -> list[float]:
    """How much each frame has zoomed in, relative to the first.

    Brute force on purpose. The drift is one number per frame -- a scale about
    the centre -- so trying every candidate against a small greyscale copy
    finds it using nothing but Pillow and numpy. Feature tracking is the
    general answer to a general problem; this is a specific one, and the
    specific answer is one that can be read.

    Whichever of the two pictures is the wider is the one pushed in for the
    comparison, so the crop is always inside the frame. Scaling the other way
    would mean inventing the border pixels, and matching against invented
    pixels is how a measurement quietly acquires a bias towards its edges.
    """
    import numpy as np
    from PIL import Image as PILImage

    n = UNZOOM_GRID
    small = [f.convert("L").resize((n, n), PILImage.BILINEAR) for f in frames]
    ref = small[0]
    grid = (n, n)

    steps = int(round(UNZOOM_RANGE / UNZOOM_STEP))
    candidates = [1.0 + k * UNZOOM_STEP for k in range(-steps, steps + 1)]

    # The reference pushed in, once per candidate, for every frame to reuse.
    pushed = {s: np.asarray(_push_in(ref, s, grid), dtype=np.float32)
              for s in candidates if s >= 1.0}
    flat = np.asarray(ref, dtype=np.float32)

    ramp = []
    for frame in small:
        best, score = 1.0, None
        plain = np.asarray(frame, dtype=np.float32)
        for s in candidates:
            if s >= 1.0:
                mae = np.abs(pushed[s] - plain).mean()
            else:
                # The frame is the wider one, so it gets pushed in instead.
                mae = np.abs(np.asarray(_push_in(frame, 1.0 / s, grid),
                                        dtype=np.float32) - flat).mean()
            if score is None or mae < score:
                best, score = s, float(mae)
        ramp.append(best)
    return ramp


def _unzoom_frames(frames: list) -> tuple[list, float]:
    """Take the camera push out of a loop's frames. Returns (frames, drift).

    Wan drifts the camera, and in a background that is the one thing that must
    not move. WAN_LOCK_STEPS is what actually prevents it now; this is the net
    under it, for the generation where the lock does not quite hold. Before the
    lock it was the only defence, and measured drift ran from nothing at all to
    twelve per cent. Wording never helped -- at cfg 1 the negative prompt is
    not even evaluated -- and vidstab took off only 7%, because a smooth push
    is the camera path it is built to KEEP rather than the shake it removes.

    Every frame is pushed in to match the tightest frame in the clip, which
    leaves the whole loop cropped by as much as the drift was and scaled back,
    because animating a place must not change its size. Measured again after
    correcting, and corrected again if anything is left: one pass of a search
    this coarse lands within a fraction of a per cent rather than on zero.

    It can only undo a ZOOM. A camera walking down a corridor also moves the
    near walls faster than the far end, and no crop undoes parallax -- which
    is why the lock had to happen in the sampler and not here.
    """
    w, h = frames[0].size
    worked, factors, first = frames, [1.0] * len(frames), 0.0
    for _ in range(UNZOOM_PASSES):
        ramp = _zoom_ramp(worked)
        drift = max(ramp) - min(ramp)
        first = first or drift
        if drift < UNZOOM_FLOOR:
            break
        # Accumulated, and applied to the ORIGINALS each time: correcting an
        # already-corrected frame would resample it once per pass.
        factors = [f * (max(ramp) / s) for f, s in zip(factors, ramp)]
        worked = [_push_in(f, fac, (w, h)) for f, fac in zip(frames, factors)]
    return worked, first


def _crossfade_loop(frames: list) -> list:
    """Make the clip join end to start by fading its tail into its head.

    The last few frames are blended into the first ones and then dropped, so
    the clip that comes out wraps with no jump. It costs those frames of
    length, and that is the whole price.

    The two other ways of closing a loop were both tried and both fail here:
      - ping-pong reverses time, and smoke sinking back down or a flame
        sucking itself in is exactly what nobody wants to watch;
      - pinning the last frame to the first, which on the locked graph did not
        hold still at all -- 17 frames is not enough to move AND come back,
        so Wan warped the room instead (21 away from the picture mid-clip, 11
        still in the last frame) and the seam was WORSE, 9x a normal step.
    A crossfade works BECAUSE the camera is locked: the walls are the same in
    both halves of every blend, so only what moves is actually faded, and on
    flames and smoke a fade is invisible. Measured on a 17-frame clip, the
    seam went from 6.1x a normal step to 1.3x -- smaller than the clip's own
    worst step, so there is nothing left to find.
    """
    from PIL import Image as PILImage

    n = len(frames)
    k = max(LOOP_OVERLAP_MIN, n // LOOP_OVERLAP_DIV)
    if n - k < k + 1:
        return frames                     # too short to give any away
    out = []
    for j in range(n - k):
        if j < k:
            t = (j + 1) / (k + 1)         # 0 is all tail, 1 is all head
            out.append(PILImage.blend(frames[n - k + j], frames[j], t))
        else:
            out.append(frames[j])
    return out


def finish_loop(clip: bytes) -> bytes:
    """Steady a place's loop and close it. Returns the finished clip.

    Decoded once and encoded once: the push taken out (_unzoom_frames), then
    the end faded into the start (_crossfade_loop). Returned untouched on any
    failure, because a loop with a flaw still beats no loop.
    """
    try:
        info = video.probe(clip)
        w, h = info["width"], info["height"]
        rate = info.get("fps") or WAN_FPS
        frames = list(video.iter_video_frames(clip, rate, w, h, info=info))
        if len(frames) < 3:
            return clip

        frames, drift = _unzoom_frames(frames)
        if drift >= UNZOOM_FLOOR:
            log.info("took %.1f%% of camera push out of a loop", drift * 100)
        frames = _crossfade_loop(frames)

        out = []
        for frame in frames:
            buf = io.BytesIO()
            frame.save(buf, format="PNG")
            out.append(buf.getvalue())
        return _encode_mp4(out, rate)
    except Exception as exc:  # noqa: BLE001 - a flawed loop beats no loop
        log.warning("could not finish the loop: %s", exc)
        return clip


def compose_clip(place_clip: bytes, character: bytes, *, fps: float = 0.0,
                 **placement) -> tuple[bytes, int]:
    """Paste a still character onto every frame of a moving place.

    Returns (mp4 bytes, frame count).

    This is the answer to "it always changes the character". An image-to-video
    model given a picture of a man in a room animates the man -- that is what
    it is for, and no prompt stops it, because not moving something is not an
    instruction it takes. So the man is never in the picture it sees. The empty
    place is animated, and the cut-out is pasted onto each frame afterwards.

    He is the SAME pixels in every frame, so he cannot drift, flicker or
    restyle. Not "barely moves" -- byte-identical. The only thing that moves is
    what the place was asked to move.

    It is also cheap: one cut-out, cached, then a paste per frame. No model
    runs here at all.
    """
    from PIL import Image as PILImage

    # Asked for at the clip's OWN size and rate. fit_inside() treats the box
    # as a maximum, so passing zeros asks for nothing and yields nothing.
    info = video.probe(place_clip)
    rate = fps or info.get("fps") or 16
    frames = list(video.iter_video_frames(place_clip, rate,
                                          info["width"], info["height"], info=info))
    if not frames:
        raise ValueError("the place has no frames")

    out_frames = []
    for frame in frames:
        buf = io.BytesIO()
        frame.convert("RGB").save(buf, format="PNG")
        out_frames.append(compose_scene(buf.getvalue(), character, **placement))

    return _encode_mp4(out_frames, rate), len(out_frames)


def _encode_mp4(png_frames: list[bytes], fps: float) -> bytes:
    """Frames to h264, through the ffmpeg that ships with imageio-ffmpeg.

    yuv420p and an even frame size on purpose: a browser will refuse to play
    h264 with odd dimensions, and the panels' own sizes are not guaranteed
    even once a background has been through a model.

    crf 18, a notch above what ComfyUI's own encoder used, because every clip
    through here is a SECOND encode of something already compressed. At the
    x264 default the corrected loops came out at half the size of the ones
    they were made from, which is detail being thrown away rather than saved.
    """
    import subprocess
    import tempfile
    from PIL import Image as PILImage

    first = PILImage.open(io.BytesIO(png_frames[0]))
    w, h = first.size
    w -= w % 2
    h -= h % 2

    exe = video.ffmpeg_exe()
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "clip.mp4")
        cmd = [exe, "-y", "-loglevel", "error",
               "-f", "image2pipe", "-vcodec", "png", "-r", f"{fps:g}", "-i", "-",
               "-vf", f"crop={w}:{h}:0:0",
               "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
               "-preset", "veryfast", "-movflags", "+faststart", out]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for png in png_frames:
            proc.stdin.write(png)
        proc.stdin.close()
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed: {err.decode(errors='replace')[:300]}")
        with open(out, "rb") as fh:
            return fh.read()


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
          batch: int, seed: int, steps: int, lcm: bool = True) -> dict:
    """Checkpoint, VAE, the LoRAs, and the conditioning the graphs share.

    `lcm` defaults to True, so every existing caller gets exactly the graph it
    got before. Only the background path passes False: that picture is made
    once and reused for weeks, so it is worth the slower, sharper sampler,
    whereas a clip of 16-32 frames is only tolerable interactively because of
    the latent-consistency LoRA.
    """
    g = {
        "ckpt": {"class_type": "CheckpointLoaderSimple",
                 "inputs": {"ckpt_name": CHECKPOINT}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "adapter": {"class_type": "LoraLoader",
                    "inputs": {"model": ["ckpt", 0], "clip": ["ckpt", 1],
                               "lora_name": ADAPTER,
                               "strength_model": 0.8, "strength_clip": 0.8}},
    }
    if lcm:
        # Latent consistency: 8 steps rather than 20. The whole feature is only
        # tolerable interactively because of this.
        g["lcm"] = {"class_type": "LoraLoader",
                    "inputs": {"model": ["adapter", 0], "clip": ["adapter", 1],
                               "lora_name": LCM,
                               "strength_model": 1.0, "strength_clip": 1.0}}
    tip = "lcm" if lcm else "adapter"
    g["pos"] = {"class_type": "CLIPTextEncode",
                "inputs": {"clip": [tip, 1], "text": prompt}}
    g["neg"] = {"class_type": "CLIPTextEncode",
                "inputs": {"clip": [tip, 1], "text": negative}}
    return g


def still_graph(prompt: str, reference: str | None, size: str,
                seed: int, keep: str = DEFAULT_KEEP,
                mask: str | None = None) -> dict:
    """One frame, no motion module. The quick look before the long wait.

    With a reference this is img2img at partial denoise, so the result keeps
    the reference's composition while showing what the prompt does to it --
    which is the question the verify step exists to answer.
    """
    w, h = SIZES.get(size, SIZES[DEFAULT_SIZE])
    g = _base(prompt, NEGATIVE, w, h, 1, seed, 8)

    level = KEEP.get(keep, KEEP[DEFAULT_KEEP])

    if reference and level.get("inpaint") and mask:
        # Subject held, scene built. The figure is already composited onto a
        # canvas of the right shape by isolate_subject(); VAEEncodeForInpaint
        # hands the sampler only what the mask marks, so the prompt rebuilds
        # everything around him at full denoise and never touches him.
        g["ref"] = {"class_type": "LoadImage", "inputs": {"image": reference}}
        g["msk"] = {"class_type": "LoadImageMask",
                    "inputs": {"image": mask, "channel": "red"}}
        g["latent"] = {"class_type": "VAEEncodeForInpaint",
                       "inputs": {"pixels": ["ref", 0], "vae": ["vae", 0],
                                  "mask": ["msk", 0],
                                  # Grown only slightly. A seam exactly on the
                                  # outline reads as a sticker, but every pixel
                                  # grown is a pixel of ring around him that the
                                  # model must invent -- and what it invents is
                                  # glow, which is the halo that makes a
                                  # composite look stuck on.
                                  "grow_mask_by": 2}}
        denoise = 1.0
    elif reference:
        g["ref"] = {"class_type": "LoadImage", "inputs": {"image": reference}}
        g["fit"] = {"class_type": "ImageScale",
                    "inputs": {"image": ["ref", 0], "upscale_method": "lanczos",
                               "width": w, "height": h, "crop": "center"}}
        g["latent"] = {"class_type": "VAEEncode",
                       "inputs": {"pixels": ["fit", 0], "vae": ["vae", 0]}}
        # An inpaint level that got here has no mask -- isolation found no
        # separable backdrop, or failed. Its denoise of 1.0 means "the mask
        # decides", and without a mask that would repaint the whole frame and
        # lose the subject entirely. Fall back to the most conservative level
        # instead, so the worst case is the old behaviour rather than a wipe.
        denoise = KEEP["close"]["denoise"] if level.get("inpaint") else level["denoise"]
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

    if reference and level.get("inpaint") and mask:
        # Put HIS pixels back. VAEEncodeForInpaint round-trips the whole frame
        # through the VAE, so even the part being preserved comes back
        # reconstructed and visibly softer -- which on a painted miniature is
        # exactly the detail worth keeping. The mask is inverted so the
        # composite takes the subject from the original canvas and the scene
        # from what was generated, and because the mask is feathered they meet
        # on a soft edge rather than a cut-out line.
        g["keepmask"] = {"class_type": "InvertMask", "inputs": {"mask": ["msk", 0]}}
        g["merge"] = {"class_type": "ImageCompositeMasked",
                      "inputs": {"destination": ["decode", 0],
                                 "source": ["ref", 0],
                                 "x": 0, "y": 0, "resize_source": False,
                                 "mask": ["keepmask", 0]}}
        g["out"]["inputs"]["images"] = ["merge", 0]
    return g


def background_graph(scene: str, style: str, size: str, seed: int,
                     reference: str | None = None,
                     refine: str = DEFAULT_REFINE) -> dict:
    """A place, with nobody in it.

    The second half of the two-input idea: a background is generated once and
    kept, so the same tavern can take a different character tomorrow and the
    same character can walk into a different tavern. That reuse is the reason
    it is a thing of its own rather than a step inside the character flow.

    Nothing here describes motion. What animates is recorded alongside the
    picture and spent later, when the clip is made -- a still cannot show it,
    and asking for it at this stage only muddies the scene.

    With a `reference` this becomes img2img, so a photograph or a sketch of a
    room can be restyled into the house look rather than described from
    scratch.
    """
    w, h = SIZES.get(size, SIZES[DEFAULT_SIZE])
    look = STYLES.get(style, STYLES[DEFAULT_STYLE])["prompt"]
    # Weighted up, and kept short around it. What you typed was competing with
    # fifty words of boilerplate for a 77-token budget, so "iron torches in
    # wall brackets" arrived as a rounding error -- and the lighting
    # directives that used to sit here ("evenly lit", "balanced exposure")
    # argued with "torchlit" outright, which the model settled by giving
    # neither. Darkness is now pushed back by the NEGATIVE instead, which does
    # not contradict a torch.
    said = scene.strip().replace("(", r"\(").replace(")", r"\)")
    parts = [f"({said}:1.3)" if said else "", look, BACKGROUND_COMPOSITION]
    text = ", ".join(p for p in parts if p)
    g = _base(text, BACKGROUND_NEGATIVE, w, h, 1, seed, BACKGROUND_STEPS, lcm=False)

    if reference:
        g["ref"] = {"class_type": "LoadImage", "inputs": {"image": reference}}
        g["fit"] = {"class_type": "ImageScale",
                    "inputs": {"image": ["ref", 0], "upscale_method": "lanczos",
                               "width": w, "height": h, "crop": "center"}}
        g["latent"] = {"class_type": "VAEEncode",
                       "inputs": {"pixels": ["fit", 0], "vae": ["vae", 0]}}
        denoise = REFINE_LEVELS.get(refine, REFINE_LEVELS[DEFAULT_REFINE])["denoise"]
    else:
        g["latent"] = {"class_type": "EmptyLatentImage",
                       "inputs": {"width": w, "height": h, "batch_size": 1}}
        denoise = 1.0

    g["sampler"] = {"class_type": "KSampler",
                    "inputs": {"model": ["adapter", 0],
                               "positive": ["pos", 0], "negative": ["neg", 0],
                               "latent_image": ["latent", 0],
                               "seed": seed, "steps": BACKGROUND_STEPS,
                               "cfg": BACKGROUND_CFG,
                               "sampler_name": BACKGROUND_SAMPLER,
                               "scheduler": BACKGROUND_SCHEDULER,
                               "denoise": denoise}}
    g["decode"] = {"class_type": "VAEDecode",
                   "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]}}
    g["out"] = {"class_type": "SaveImage",
                "inputs": {"images": ["decode", 0], "filename_prefix": "background"}}
    return g


def clip_graph(prompt: str, reference: str | None, size: str, seed: int,
               length: str = DEFAULT_LENGTH, keep: str = DEFAULT_KEEP,
               pingpong: bool = False) -> dict:
    """The looping MP4.

    With a reference, SparseCtrl pins it to both ends of the sequence and the
    closed loop comes off: anchored only at frame 0 the animation drifts off
    the subject well before the end, and a closed loop then fights the anchor
    it is trying to return to.

    `pingpong` is the blunt instrument for the seam that leaves: the clip is
    written out forwards then backwards, so it joins end to start whatever the
    sampler did. gen.py has had this as --pingpong all along; it was hardcoded
    off here, so the web UI could not do what the CLI could. Only sound for
    non-directional motion -- fire, water, drifting light.
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
                           "pingpong": pingpong, "save_output": True}}
    return g


# ----------------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    kind: str                       # verify | clip | background | placeloop
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
    style: str = DEFAULT_STYLE      # house look, backgrounds only
    motion: str = ""                # what should move in it, spent at clip time
    bg_id: str = ""                 # set once a background has been filed

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
                "style": self.style, "motion": self.motion, "bg_id": self.bg_id,
                "estimate": estimate_for(self.kind, self.length)}


# Rough, measured on an Apple M1 Pro. Only ever used to pace a progress bar,
# so being wrong makes the bar wrong and nothing else. A clip scales with the
# frames asked for; a verify is one frame whatever else is set.
ESTIMATES = {"verify": 10, "clip": 240, "placeloop": 170}


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
           keep: str = DEFAULT_KEEP, pingpong: bool = False,
           mask: str | None = None, style: str = DEFAULT_STYLE,
           motion: str = "", refine: str = DEFAULT_REFINE,
           bg_id: str = "", clip_length: str = DEFAULT_WAN_LENGTH) -> Job:
    """Queue a job and return at once; the work happens on a thread."""
    if not prompt.strip() and kind == "clip" and not reference:
        raise ValueError("a clip needs a description, a reference image, or both")
    if kind == "background" and not prompt.strip() and not reference:
        raise ValueError("a background needs a description, a picture, or both")
    if kind == "placeloop":
        if not reference:
            raise ValueError("animating a place needs the place's own picture")
        if not motion.strip():
            raise ValueError("say what should move in it")
        # The motion words ARE this job's prompt. Without this the queue shows
        # a running loop as "(no description)", which is the one moment you
        # want to know which of four attempts is in flight.
        prompt = motion.strip()

    job = Job(id=uuid.uuid4().hex[:12], kind=kind, prompt=prompt, size=size,
              seed=seed, has_reference=bool(reference), length=length,
              adopted_from=adopted_from, keep=keep)
    # Carried so whatever files the finished picture knows the words that made
    # it, and what should move in it later.
    job.style = style
    job.motion = motion
    job.bg_id = bg_id

    _remember(job)

    if kind == "placeloop":
        # The place alone, in motion. The character is pasted on afterwards --
        # see compose_clip for why he is never in the picture the model sees.
        graph = background_clip_graph(reference, motion, clip_length, seed)
    elif kind == "background":
        graph = background_graph(prompt, style, size, seed, reference, refine)
    elif kind == "verify":
        graph = still_graph(prompt, reference, size, seed, keep, mask)
    else:
        graph = clip_graph(prompt, reference, size, seed, length, keep, pingpong)
    threading.Thread(target=_run, args=(job, graph), daemon=True).start()
    return job


# Set once by the app, so a finished job can be filed without generate.py
# importing the library and the two growing into one module.
log = logging.getLogger(__name__)

_ON_DONE = None


def set_on_done(fn) -> None:
    """Call `fn(job)` whenever a job finishes successfully."""
    global _ON_DONE
    _ON_DONE = fn


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
                        # Before anyone sees it, and before it is filed: a
                        # loop that drifts, or jumps where it joins, is a flaw
                        # in the clip, not a step a person should ask for.
                        if job.kind == "placeloop" and name.endswith(".mp4"):
                            job.result = finish_loop(job.result)
                        job.filename = name
                        job.content_type = ("video/mp4" if name.endswith(".mp4")
                                            else "image/png")
                        job.state = "done"
                        job.finished = time.time()
                        # A background is an asset, not a result to look at
                        # once: the whole point is that the same place is
                        # reused later. Keeping it here means it is on the
                        # shelf the moment it appears, with no "save" step to
                        # forget. The hook keeps generate.py from having to
                        # know anything about the library.
                        if _ON_DONE is not None:
                            try:
                                _ON_DONE(job)
                            except Exception as exc:   # noqa: BLE001
                                log.warning("on_done failed for %s: %s", job.id, exc)
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
