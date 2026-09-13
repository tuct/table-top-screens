# Table top - mini screens

check first:

a) can we play short videos / animations on e.g waveshare 4.3 with esp32s3
b) control via local network and android app, based on esphome

**Goal:** several tabletop screens, mixed ESP32 variants and mixed panels,
all fed from one place over the local network.

---

## Layout

```
esphome/
  tabletop-01.yaml                      device instance: identity only
  tabletop-02.yaml                      "
  boards/
    waveshare-s3-touch-lcd-4.3.yaml     800x480 RGB parallel, GT911, CH422G
    seeed-xiao-round-display.yaml       240x240 round SPI, GC9A01A, CHSC6X
  common/
    base.yaml                           wifi / api / ota / web_server / diagnostics
    content-pull.yaml                   mDNS advertisement + content path
  secrets.yaml.example
server/
  app.py                                content server (Flask + Pillow)
  discovery.py                          mDNS browse + push to screens
  library.py                            per-device image library
  test_library.py  test_content.py  test_discovery.py
  README.md                             API, formats, the discovery contract
```

### The board contract

A board package owns everything panel-specific and must declare:

| Substitution | Meaning |
|---|---|
| `display_width` / `display_height` | panel size, sent to the server via mDNS |
| `display_round` | `"1"` for a circular panel, sent to the server via mDNS |
| `default_fit` | `contain` / `cover` / `width` / `height` / `stretch` |
| `dot_x` / `dot_y` | where the activity dot goes |

It also owns its **display lambda**, because the boot checklist has to be laid
out for the panel it is on.

**This was tested, not assumed.** Adding the Seeed round display needed one
new `boards/*.yaml` and a three-line device file — and **no change at all** to
`common/`, to `discovery.py`, or to the server. The round panel declares
`default_fit: cover` (its corners are behind the bezel, so letterboxing would
put bars where nothing is visible) and moves the activity dot to top-centre
for the same reason; the server picks both up from the mDNS TXT records and
renders 240x240 from the same library that feeds the 800x480 screen.

### Capabilities

Every screen advertises what it can do in its mDNS TXT record — `w`, `h`,
`round`, `img` (still formats), `anim` (animation formats, or `none`), `sd`
and `clip` (`frames` or `mjpeg`) — and the server adapts. The board declares the hardware facts (`w`, `h`,
`round`); the packages a device includes declare the rest, by overriding
defaults set in `common/content-pull.yaml`. Between packages the later one's
substitutions win, so **capability packages go after `content`** in the device
file.

| Screen | Packages | Advertises |
|---|---|---|
| tabletop-01 (Waveshare 4.3) | `sd-card` + `sd-still` | `anim=none sd=1 img=jpeg,rgb565` |
| tabletop-02 (round XIAO) | `sd-card` + `mjpeg-clip` | `round=1 anim=gif,apng,webp sd=0 clip=mjpeg` |

**The Waveshare 4.3 is stills only.** Its GIF path cached clips by
JPEG-decoding every frame into a 768 KB buffer, which is exactly the memory
this board should not spend on content. So it no longer includes
`sd-clip.yaml`, advertises `anim=none`, and the server sends it the first
frame of anything animated.

### MJPEG content from memory (`common/mjpeg-clip.yaml`)

This follows [derdacavga/video-Player](https://github.com/derdacavga/video-Player).
Stills and clips both stay compressed as JPEG. They're held in PSRAM and on the
card, and each frame is decoded straight to the panel.

**Everything is an item.** The server's `/clip.mjpeg` returns one frame for a
still and many for a clip. An item is named by the `v` token of the content
URL, which covers the picture *and* its framing, plus Target FPS. The server
pushes that URL on every switch, so the screen knows what it's switching to
before it touches the network:

```
held in memory          ->  shown instantly, no network
evicted, but on card    ->  /mjpeg/cache/<key>.mjp -> PSRAM, no network
never seen              ->  downloaded once: to the card, or into PSRAM if no card
```

Memory holds `cache_bytes` (5 MB) of items and evicts the least recently shown
first. A single item is at most `max_bytes` (4 MB); longer clips lose trailing
frames. The card keeps every item. Refresh presses and background polls cost
nothing for content the screen already holds.

**Why decoding every frame is fine.** ESPHome's image decoder has JPEGDEC
produce RGB8888, then pushes every pixel through a virtual `draw_pixel()` with
float scaling. That per-pixel output, not the JPEG decode, is where the
~1.8 s per frame goes. `sd_clip` asks JPEGDEC for RGB565 and blits whole
blocks, as video-Player does.

**How long a clip can be.** Real 240×240-equivalent video averages about
2.8 KB per frame at ffmpeg `-q:v 7`, or roughly double at q80. 4 MB is about
35–70 s at 20 fps. `Clip Seconds` reports the real figure.

**Files on the card.** Put them in `/mjpeg`. Two formats work: the server's
TTMJ, or plain back-to-back JPEGs such as `ffmpeg -c:v mjpeg` or
video-Player's converter produce. Pick a file with **Next SD Clip** or by
typing its name into **SD Clip**. The choice survives reboots, and new content
from the server replaces it.

### Stills on the SD card (`common/sd-still.yaml`)

When a card is mounted, a still never touches a decode buffer:

```
GET …/image?…&fmt=rgb565   →  streamed to /still/new.565, ≤64 KB per loop tick
                           →  renamed over /still/image.565, ETag kept alongside
/still/image.565           →  read a band at a time into internal RAM → panel
```

- **Conditional GET.** The stored ETag goes out as `If-None-Match`, so an
  unchanged picture costs one empty 304.
- **Works offline at power-up.** The picture is on the card, so it shows
  before WiFi or the server are up.
- **"If available" is decided per fetch.** With no card mounted,
  `fetch_content` falls back to `online_image` and JPEG; pulling the card
  under a still hands the panel back to that path.
- **Non-blocking.** The body is streamed from `SdClip::loop()`, so web_server
  and pushes stay responsive during a 768 KB transfer.
- **Paced.** A fetch request is a flag, so a burst of pushes collapses into one
  fetch, fetches are at least 2 s apart, and an unchanged (304) picture is
  never re-blitted. Failures retry after 2 s, 4 s and 6 s, then back off
  30 s → 1 min → 2 min → 4 min → 5 min; new content from the server skips the
  wait.
- **Card status.** Mounted and in-use state, plus free and total space, are
  published as `SD Mounted`, `SD In Use`, `SD Free` and `SD Total`. The boot
  checklist shows them (`SD card ... stills on card, 27.4 of 29.7 GB free`),
  and so does the server's overview.
- No activity dot on an SD still: there is no copy of the picture in RAM to
  composite the dot against or erase it back to.

Caveat, so nobody over-reads "no PSRAM": `mipi_rgb` hardcodes
`fb_in_psram = 1`, so the panel's own framebuffer still lives in PSRAM. What
this path removes is PSRAM use **for content**: no decode buffer, no clip cache.

**Not yet measured on hardware:** raw transfer time for 768 KB, now that
`buffer_size_rx` is 8192. The ~16 s figure in `sd-clip.yaml` matches the old
512-byte read size (~48 KB/s), so it may well predate that fix — check the
`Still: N KB stored in M ms` log line.

## Phase 1 — S3 + still images (working on hardware)

Deliberately scoped to stills. Per the S3 analysis below, motion on that
board is limited to small baked-in animations; rather than fight that, phase 1
gets the whole pipeline working end to end — board, touch, discovery, content
delivery, HA/app control — and motion arrives with the P4.

**Board:** Waveshare ESP32-S3-Touch-LCD-4.3, 800x480, `mipi_rgb` preset
`ESP32-S3-TOUCH-LCD-4.3`, GT911 touch, CH422G expander.

### Zero-config discovery

No IP address is typed anywhere. Same network is the only requirement.

```
  screen boots
      │  advertises _minidisplay._tcp   { name, w, h, fmt, fit }
      ▼
  server browses mDNS ──► learns name + IP + panel size
      │
      │  POST /text/content_url/set?value=http://<server-ip>:8099/d/<name>/image?w=…&h=…
      ▼
  screen fetches that URL and draws it
```

- **Server → screens:** browses `_minidisplay._tcp.local.`; the device
  publishes its name and panel size as mDNS TXT records, so the registry
  needs no config file.
- **Screens → server:** they never need to know where it is. On discovery the
  server POSTs the content URL into the screen's `content_url` text entity
  over `web_server`'s REST API — filling in the address *that screen* can
  reach it on, computed per device rather than from `gethostname()`, so it's
  correct on a multi-homed server.
- **`restore_value: true`** on that text entity means a screen keeps working
  through a reboot even if the server is down at the time.

### Push latency without a custom component

ESPHome cannot receive a pushed image — `web_server` exposes fixed per-entity
endpoints, not upload routes. But pushing a *notification* over those
endpoints and letting the device pull the bytes gets push latency for free:
on upload the server POSTs `/button/refresh_content/press` and the screen
fetches immediately, well under a second.

The 60s background poll is only a safety net for a missed push, and it's
nearly free: `online_image` implements ETag caching, so an unchanged image is
one 304 with an empty body. (`component.update` preserves the stored ETag;
`set_url` deliberately resets it.)

### Two details that matter more than they look

**Image format — and why the display lambda does not call `it.image()`.**
The image is `type: RGB565`, so the decoded buffer is already in the panel's
pixel format.

It is tempting to assume `it.image()` then blits that buffer straight out.
It does not. `Image::draw()` in
[`image.cpp`](https://github.com/esphome/esphome/blob/dev/esphome/components/image/image.cpp)
has no bulk path at all — the `IMAGE_TYPE_RGB565` branch is unconditionally

```cpp
for (int img_x = ...) for (int img_y = ...)
  display->draw_pixel_at(x + img_x, y + img_y, this->get_rgb565_pixel_(img_x, img_y));
```

one virtual call per pixel, whatever the type or byte order: 57,600 calls for
a 240×240 frame, 384,000 for 800×480. Measured, that capped the round panel at
about **2 fps**.

So the board lambdas call `draw_pixels_at()` themselves, passing
`get_data_start()` directly — a single DMA-able write of the same bytes. That
is the only reason the format matters; `draw_pixels_at()` is where the
matching-bitness fast path into `esp_lcd_panel_draw_bitmap()` actually lives.

**The byte order follows from that choice**, and this is trap 3 below in
reverse. `it.image()` would want `LITTLE_ENDIAN`, because `image.cpp`
hardcodes `encode_uint16(read(pos + 1), read(pos))`. The blit path instead
needs the *display's* convention — big-endian for both `mipi_rgb` and
`mipi_spi` — so the config is `byte_order: BIG_ENDIAN`. Getting this backwards
is exactly what produced the R→B, G→R, B→G colours during bring-up.

`auto_clear_enabled: false` and `update_interval: never` on the display serve
the same end: nothing else should touch the panel between frames.

**Backlight — do not add a light on CH422G EXIO2.** On this board EXIO2
drives the backlight *and* the panel enable line; they are the same pin.
Turning "the backlight" off powers down the display logic while the graphics
stack keeps rendering into it — in openHASP this shows up as crash-and-reboot
on wake, or a permanently black screen
([discussion](https://github.com/HASwitchPlate/openHASP/discussions/602)).
We avoid it only because the `mipi_rgb` preset claims EXIO2 as `enable_pin`
and asserts it once at setup, with nothing toggling it at runtime. The cost
is that brightness isn't adjustable at all.

Real dimming needs SMD rework: remove the resistor and capacitor coupling the
backlight driver to the display enable signal, then wire GPIO6 — the pin
labelled **Sensor AD** on the right-hand header — to the AP3032 control pin.
Done on our board:

![Backlight decoupling mod: red wire from the backlight driver area to the
Sensor AD pin](waveshare_4_4_bcl_hack.png)

The board file has the ready-to-uncomment LEDC output + `monochromatic` light
for after the mod.

### Getting it running

```bash
# 1. content server
cd server && python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe app.py            # listens on :8099

# 2. device
cd esphome && cp secrets.yaml.example secrets.yaml   # then fill it in
esphome run tabletop-01.yaml
```

Then open `http://<server>:8099/` — the screen appears on its own — tap it and
upload an image. See [server/README.md](server/README.md) for the API,
discovery contract and format trade-offs (JPEG vs PNG vs QOI).

mDNS caveats for a permanent install: needs UDP 5353 and the server on the
same L2 segment as the screens (across VLANs it needs an mDNS reflector), and
in Docker it needs `--network host` — a bridge network breaks both multicast
discovery and the local-IP calculation.

### Verified

**Running on hardware (2026-09-12).** Image pushed from the server and
displayed with correct colours; screen discovered over mDNS at
`192.168.86.64` and auto-configured with `http://192.168.86.41:8099/...`, the
correct same-subnet address chosen per device. No address configured anywhere.

- `esphome config` valid, `esphome compile` links, ESPHome 2026.6.5 /
  esp-idf 5.5.4, 1.1 MB `firmware.bin`.
- **`server/test_content.py`: 28/28.** Upload (multipart and raw) → render →
  200 with ETag → 304 with an empty body on repeat → each panel size gets its
  own render and its own ETag → output is baseline, not progressive, JPEG,
  which is what ESPHome's decoder requires. Bad formats, bad fit modes,
  non-integer and oversized dimensions, invalid device names, empty and
  non-image uploads all rejected.
- **`server/test_discovery.py`: 19/19.** Against a stub advertising the same
  mDNS service and TXT records and answering the same two REST routes as the
  real device: discovered, configured with its own panel size, pushed to on
  upload, that exact pushed URL serves a correctly-sized JPEG, repeat fetch
  304s, and the screen is dropped when it leaves the network.
- **Touch confirmed.** GT911 comes up at address 0x5D with a hardware
  interrupt on GPIO4, reset on CH422G EXIO1 — the guessed pin was right.

### Six traps this brought up, and how each was settled

Worth reading before bringing up the next board. Not one was a wiring fault —
they were wrong defaults, stale documentation, or races — and every one
presented as something other than its cause.

**1. Flash size — instant bootloop.** Waveshare's spec page says 16 MB. This
board is an **M0N8R8 = 8 MB**. `esptool flash-id` reports GigaDevice
`c8:4017` (GD25Q64). With `flash_size: 16MB` ESPHome generates two 0x7C0000
app partitions, and the bootloader rejects the table against the real chip:

```
partition 3 invalid - offset 0x7d0000 size 0x7c0000 exceeds flash chip size 0x800000
```

Never trust the product page — run `esptool --port COMx flash-id` first.

**2. Backlight — panel initialises, screen stays dark.** On this board
CH422G EXIO2 drives the backlight *and* the panel enable line. On a
**modded** board (ours: coupling parts removed, wire to "Sensor AD") GPIO6 is
the only thing that lights the backlight, so without an LEDC output on GPIO6
everything works and the screen is black. On an *unmodded* board, never put a
light on EXIO2 — switching it off kills the display logic while the graphics
stack keeps rendering, which is the openHASP
[crash-on-wake](https://github.com/HASwitchPlate/openHASP/discussions/602).

**3. Colour — `byte_order`, and it looks like something else entirely.**
Symptom was R→blue, G→red, B→green, which reads as a channel rotation and
sends you hunting through `data_pins` and `color_order`. It is neither. It is
a byte-order mismatch: an 8-bit shift drags R's high bits into RGB565's 6-bit
green field, and the 5/6/5 asymmetry makes the result *look* like a rotation.
Predicted vs observed matched on all six test bands.

- Fix: `byte_order: LITTLE_ENDIAN` on the image — which is also the default
  when omitted, and what ESPHome's image component warns you to use.
- It **cannot** be set on the display: `mipi_rgb`'s `byte_order` only
  populates display metadata, and `process_runtime_image_config()` reads the
  image's own key, never the metadata. Silently ignored for `online_image`.
- `color_order` is **inert** on `mipi_rgb`: data pins are emitted hardcoded
  blue→green→red then rotated, and `color_mode_` has a getter nothing calls.
  The docs claiming "bgr (default)" are stale — the generated code emits
  `COLOR_ORDER_RGB`. Don't spend a build on it.
- The pin mapping is **correct** and needs no override. Verified against the
  generated `main.cpp`: un-rotating the emitted `add_data_pin` order
  reproduces Waveshare's documented `D0..D15` exactly.

There is a real open upstream bug nearby —
[esphome#10772](https://github.com/esphome/esphome/issues/10772), where a
contributor confirms "ESP32-S3-TOUCH-LCD-4.3 is also RGB (and needs to be
updated)" — but the installed 2026.6.5 and current `dev` have byte-identical
`data_pins`, and the `color_order: RGB` workaround in that issue cannot work,
because the option is never consulted.

**4. WiFi — the SSID simply was not there.** `No networks found` looping
forever looked like a hidden SSID (so `fast_connect: true` went in) and even
more like a dead antenna. Both wrong: the configured SSID was not in range,
and once the right one was set the board scanned normally and found three
BSSIDs of it. Two lessons:

- The antenna question was settled **without a reflash**: the fallback
  hotspot `tabletop-01-setup` was visible from a laptop at **95%**, BSSID one
  above the board's own MAC. A board whose AP you can see has a working
  radio, so look there before suspecting hardware.
- `fast_connect: true` is now **off**. On this network the SSID is a mesh
  with three BSSIDs, and fast_connect tries the *saved* BSSID first; when
  that one is no longer the best it fails, costing ~24s of retries before a
  scan finds a good AP. Keep it for a genuinely hidden SSID or a single AP.

**5. Every fetch must be guarded on `wifi.connected`.** Two things fire
before the network is up, both logging
`HTTP Request failed; Not connected to network`: `restore_value` replays the
`content_url` `on_value` trigger during `setup()`, and an ESPHome `interval`
fires **once immediately at startup**, not after its period. So `set_url`
uses `update: false`, and a 5s interval retries until the first image lands.

**6. `online_image.get_width()` cannot tell you whether content loaded.**
With `resize` set it is non-zero from boot, so the "waiting for content"
placeholder never appeared. There is now a `content_loaded` global set in
`on_download_finished`.

### Image quality

JPEG's default **4:2:0 chroma subsampling** was the dominant defect, not the
quality setting — it halves colour resolution in both axes and smears every
sharp colour edge. Measured on the test pattern:

| config | size | worst-pixel error | mean error |
|---|---|---|---|
| q88 4:2:0 (old) | 23.1 kB | **170** | 1.42 |
| q88 4:4:4 | 33.2 kB | 57 | 0.37 |
| **q95 4:4:4 (now)** | 45.5 kB | 17 | 0.25 |
| q100 4:4:4 | 68.8 kB | 4 | 0.17 |

Turning subsampling off alone took worst-case error 170 → 57 at identical
quality. RGB565 quantisation is ±4 on R/B and ±2 on G, so q95 is transparent
for photographs; only q100 gets the worst case below the panel's own step, so
hard-edged graphics still benefit from `q=100` or from lossless QOI.

### What the screen shows

**Boot checklist**, until the first image lands:

```
tabletop-01
Connecting to WiFi ... ok
Searching for server ... ok
Getting content ...
```

Every line is derived from live state, so it cannot claim a stage succeeded
when it has not, and a screen that is not working says why instead of sitting
blank. Errors appear underneath in red.

**Activity dot** — a 16px square top-left while a fetch is in flight, cycling
amber / cyan / magenta. It is drawn with `draw_pixels_at()` straight to the
panel rather than through the display writer: a 512-byte DMA instead of a
768 KB full-frame repaint, so blinking costs nothing and does not disturb the
picture. A full repaint erases it; after an unchanged fetch (304) the repaint
happens only if a dot was actually drawn.

Honest caveat: `online_image` streams the download across `loop()` calls, so
the dot animates during transfer — but **JPEG cannot decode until it has every
byte**, and that final decode blocks for ~1.7s, during which the dot freezes.
It is an "in progress" light, not a smooth spinner.

### Flicker during a fetch, and why

`mipi_rgb` runs the panel in ESP-IDF's **bounce-buffer mode**, all hardcoded
with no YAML knobs: `fb_in_psram = 1`, `bounce_buffer_size_px = width * 10`
(16 KB), `num_fbs = 1`. In that mode the **CPU** copies framebuffer chunks
from PSRAM into internal SRAM inside an ISR.

The scanout load is relentless — at this preset's 16 MHz pixel clock:

| | |
|---|---|
| Total line / lines | 1070 px / 492 |
| Frame | 32.9 ms → **30.4 Hz** |
| Read from PSRAM | 750 KB/frame → **23.3 MB/s, continuously** |

A single ~1.7s CPU-bound JPEG decode competes for exactly the two things that
refill needs: CPU time for the ISR and PSRAM bandwidth. Starve it and lines
arrive late — which is the flicker.

Mitigations, best first:

1. **Fetch rarely.** Done: `poll_interval` is 30min and there is a
   "Background Poll" switch, because the server pushes on change anyway. This
   does not make a fetch cleaner, it makes fetches rare.
2. **QOI instead of JPEG.** QOI is a byte-level unpack — no Huffman, no IDCT,
   and no need to buffer the whole image before decoding, so the long
   blocking call largely disappears. Measured on our own content: 218.6 kB
   QOI vs 77.5 kB JPEG, so ~2.8x the download but far less CPU, and lossless
   into the bargain. Needs `pip install qoi numpy`, `fmt: qoi` in the mDNS TXT
   and `format: QOI` on the device. **Not yet measured on hardware.**
3. **Lower the pixel clock** (16 → 12 MHz) cuts scanout bandwidth ~25%, but
   drops refresh to ~23 Hz, which can itself look flickery. A trade, not a win.
4. **PSRAM at 120 MHz** gives ~50% more bandwidth, but ESPHome requires
   `enable_idf_experimental_features: true` and 240 MHz CPU, and warns
   "use at your own risk". Last resort.

### Do not set `poll_interval: 0s`

It validates, and it does the opposite of disabling. `interval:` takes
`positive_time_period_milliseconds`, and `IntervalTrigger` is a
`PollingComponent`, so 0 means *fire every loop iteration* — continuous HTTP.
`never` is not accepted here either (that is `cv.update_interval`, which
`interval:` does not use). Use the **Background Poll** switch, or a long
period.

### Measured on hardware

**Fetch + decode + draw of an 800x480 baseline JPEG: ~1.9 s.** Consistent
across sizes (23.6 kB → 1983 ms, 33.3 kB → 1914 ms), so it is decode-bound,
not network-bound — software JPEG on the S3, plus a one-off 768 KB PSRAM
buffer allocation. It blocks the main loop, which ESPHome reports as
`online_image took a long time for an operation (1965 ms)`.

Fine for a photo frame; too slow for snappy transitions, and it rules out any
slideshow faster than a few seconds per frame without double-buffering. It is
also a second, independent argument for the P4: its hardware JPEG decoder
would make this ~50x faster, and that matters even for stills.

Flash 28.4% of the 3.75 MB app partition, RAM 15.7% — plenty of headroom.

### Phase 1 open items
- Try `fmt=qoi` for graphics: lossless, and should beat JPEG on flat colour
  while decoding far faster than PNG. Needs `pip install qoi numpy` plus
  `format: QOI` on the device.
- Decide whether a `placeholder` image beats the "Waiting for a content
  server..." text (`online_image` supports one).
- Set `logger: level` back to `INFO` in `common/base.yaml` once bring-up is
  done — it is at `DEBUG` for diagnostics.
- Submit the 4.3 findings upstream: at minimum the docs fix for
  `color_order`, which is documented but inert on `mipi_rgb`.
---

## Phase 2 — ESP32-P4 for motion

Why the P4, and what the video component would look like.

### Decision: ESP32-P4, not S3

**Target hardware:** [Waveshare ESP32-P4-WIFI6-Touch-LCD-4.3](https://www.waveshare.com/esp32-p4-wifi6-touch-lcd-4.3.htm)

| | |
|---|---|
| SoC | ESP32-P4NRW32, dual-core RISC-V @400MHz + LP core |
| Flash / PSRAM | 32 MB NOR flash, **32 MB PSRAM** (in-package) |
| Panel | 480x800 IPS, **MIPI-DSI 2-lane** (D-PHY v1.1, up to 2x1.5 Gbps) |
| Touch | 5-point capacitive |
| Wireless | **ESP32-C6-MINI-1 coprocessor** over SDIO (WiFi 6 + BLE 5) |
| Multimedia | **HW JPEG codec**, H.264 **encoder**, ISP, PPA, 2D-DMA |
| Storage / IO | SDIO 3.0 TF slot, MIPI-CSI (OV5647), ES8311 + ES7210 audio, USB-OTG HS, 40-pin HAT header, battery + RTC |

### Why this changes the answer

The S3 verdict was "~8 fps full-screen MJPEG, software decode". The P4 has a
**hardware JPEG decoder**, and it is not marginal — per the ESP-IDF docs, at
360 MHz / 200 MHz SPI RAM it does 1920x1080 YUV422 to RGB at **48 fps**, and
320x480 at up to **571 fps**. Our 480x800 (384 kpx) sits near the fast end of
that curve, so decode is effectively free.

**The bottleneck moves to PSRAM bandwidth.** Waveshare's own MP4-player
example says this outright: *"Blue-screen flickering during playback is
usually caused by insufficient PSRAM bandwidth. Try RGB565 output, a lower
resolution, a lower frame rate, or higher JPEG compression."* Their
recommended encode is **RGB565, native resolution, 20 fps, `-q:v 6`**.

Budget for 480x800 RGB565 (768 KB/frame):

| Traffic | MB/s |
|---|---|
| DSI scanout reading the framebuffer @60 Hz | ~46 |
| Writing decoded frames @25 fps | ~19 |
| Reading the MJPEG bitstream (`-q:v 6`, ~40 KB/frame) | ~1 |

So 20-25 fps full-screen is the realistic target, and it is a bandwidth
budget, not a compute one. Source bitrate (~1 MB/s) is trivial for both the
SDIO 3.0 card slot and WiFi-6-over-SDIO.

Note H.264 on the P4 is **encode-only** in hardware; decode is software.
Some marketing copy claims 4K H.264 decode — that is wrong. **MJPEG is the
right container for this project.**

### ESPHome support status on P4 — usable, with sharp edges

- ESP32-P4 is supported as of **ESPHome 2026.3.0** (still carries an
  engineering-sample warning).
- Display: the **`mipi_dsi`** platform is P4-only and has presets for the
  7B (1024x600), 4C (720x720), 3.4C (800x800), P4-NANO-10.1 and several
  standalone DSI panels — **but not the 4.3 (480x800)**. We need
  `model: CUSTOM` with explicit dimensions + init sequence.
- WiFi goes through the **`esp32_hosted`** component (P4 to C6 over SDIO),
  with C6 firmware OTA-updatable. **Known instability:**
  [esphome/esphome#10956](https://github.com/esphome/esphome/issues/10956)
  reports WiFi association usually failing on a Waveshare P4 board. Worth
  validating early — this is the main project risk, and it hits question
  (b), not (a).

---

## Plan: a `mjpeg_player` component for ESPHome

Goal: play MJPEG full-screen at 20-25 fps, controllable from HA / an Android
app. ESPHome has **no video playback component at all**, so this is new code.

### Direct-to-panel vs LVGL — direct wins, and here is the proof

Reading [`mipi_dsi.cpp`](https://github.com/esphome/esphome/blob/dev/esphome/components/mipi_dsi/mipi_dsi.cpp):
`MipiDsi::draw_pixels_at()` checks whether the source bitness matches the
panel's, and if it does, calls `write_to_display_()` which calls
**`esp_lcd_panel_draw_bitmap()` directly**. No ESPHome framebuffer, no format
conversion, no CPU copy — just a DMA submit. The internal 768 KB `buffer_` is
allocated lazily by `check_buffer_()` and is only touched by
`draw_pixel_at()` / `fill()`, so a pure video path never allocates it.

Compare the LVGL route: decode, memcpy into an LVGL canvas/image buffer,
LVGL renders into its draw buffer, flush, panel. That is **two extra
full-frame trips through PSRAM per frame** — ~31 MB/s of avoidable traffic at
20 fps, against the exact resource the Waveshare notes identify as the
limit. So "direct is faster" is not a hunch here; it is the difference
between fitting in the bandwidth budget and not.

**Consequence: v1 needs zero changes to ESPHome core.** `draw_pixels_at()`
is public. An external component can decode into an aligned buffer and push
it straight to the panel.

### Three implementation tiers

**Tier 1 — external component, one DMA copy** *(start here)*

```
reader task            decode task
SD / HTTP  --ring-->   jpeg_decoder_process()  -->  display->draw_pixels_at()
                       (HW, RGB565 out)             (-> esp_lcd_panel_draw_bitmap)
```

Two tasks on the two cores so I/O overlaps decode. atomic14's S3 work found
overlapping decode and display is exactly where the frame rate comes from —
same principle, much faster decoder.

**Tier 2 — true zero-copy into the scanout buffer**

`esp_lcd_dpi_panel_get_frame_buffer()` hands back the driver's framebuffer;
`jpeg_decoder_process()` can decode *straight into it*. Decode output becomes
the scanout buffer and the copy disappears entirely. Needs `num_fbs: 2` for
double-buffering to avoid tearing, plus the vsync / refresh-complete
callback.

This is already half-built upstream:
**[esphome/esphome#16853](https://github.com/esphome/esphome/pull/16853)
"[mipi_dsi] Add DMA2D-backed async flush support"** (open since 2026-06)
exposes the DPI frame buffers, adds a refresh-completion wait helper, and
adds a DMA2D path with perf counters. That PR is the natural foundation —
worth commenting on it rather than duplicating it.

**Tier 3 — PPA composition (video + LVGL UI together)**

The hard part. Mixing full-screen video with an LVGL overlay needs either PPA
blending or restricting LVGL to draw outside the video rect. Defer this; v1
should own the screen exclusively.

### Implementation notes / gotchas

- **Guard on `USE_ESP32_VARIANT_ESP32P4`** — the HW JPEG decoder is P4-only.
  `mipi_dsi` is already P4-only, so this is consistent with precedent.
- **Buffer alignment is not optional.** Use `jpeg_alloc_decoder_mem()`; the
  2D-DMA requires cache-line *and* byte alignment. Misaligned buffers give
  silent corruption, not an error.
- **Dimension divisibility:** YUV420 needs w,h divisible by 16. 480/16=30,
  800/16=50 — our panel is clean. YUV422 needs w%16, h%8.
- **Input must be baseline JPEG**, YUV444/422/420/grayscale.
- **Container:** skip MP4/AVI demuxing in v1. Simplest source is a `.mjpeg`
  file (concatenated JPEGs, framed on SOI/EOI) or MJPEG-over-HTTP
  (`multipart/x-mixed-replace`) — the latter also makes HA the video source.
- **ESPHome surface:** actions `mjpeg_player.play` / `pause` / `stop` /
  `seek` with templatable `file` / `url`, triggers `on_playback_finished` /
  `on_frame`, and a `media_player`-ish state so it shows up in HA. That is
  what connects this back to (b).
- **Encode recipe** (adapted from Waveshare's):

  ```bash
  ffmpeg -i input.mp4 -c:v mjpeg -q:v 6 -vf scale=480:800 -r 20 -an out.mjpeg
  ```

### Reference implementations to mine

Waveshare ships working P4 examples in
[waveshareteam/ESP32-P4-WIFI6-Touch-LCD-X](https://github.com/waveshareteam/ESP32-P4-WIFI6-Touch-LCD-X):

- `examples/esp-idf/10_mp4_player` — **MJPEG-in-MP4/AVI from microSD to the
  DSI panel.** Closest thing to what we want; read this first.
- `examples/esp-idf/09_video_lcd_display` — CSI camera to DSI panel via
  `esp_video`.
- `examples/esp-idf/12_usb_extend_screen` — the board as a USB display.

Caveat: that repo's BSP covers the 7 / 8 / 10.1 variants (ILI9881C and
JD9365) — **the 4.3 is not in its variant table**, so panel init has to come
from elsewhere.

---

## Open items

1. **Identify the actual hardware on hand** (see below) — everything else
   depends on which board and interface it is.
2. **Find the 4.3 panel's driver IC + init sequence.** Not documented in
   Waveshare's wiki, product page, spotpear mirror, or the GitHub example
   repo's variant table. Options: read the IC marking off the panel FPC,
   pull the BSP from the ESP Component Registry
   (`waveshare/esp32_p4_wifi6_touch_lcd_*`), or dump the factory firmware.
   Once we have it, submitting a `mipi_dsi` preset upstream is easy —
   gtjoseph has been adding exactly these
   ([#13840](https://github.com/esphome/esphome/pull/13840),
   [#14023](https://github.com/esphome/esphome/pull/14023)).
3. **Validate `esp32_hosted` WiFi stability** before building anything on
   top of it (see #10956).
4. Decide tier 1 vs waiting on / contributing to PR #16853.

### How to identify the board and panel interface

- `esptool --port COMx chip-id` — confirms ESP32-P4 vs S3, and flash size.
- **The P4 has no radio of its own** — if the board does WiFi, there is a
  separate C6/C5 module on it. Its presence confirms the board family.
- **Interface by ribbon connector:** MIPI-DSI is a narrow FPC (~15-22 pin,
  0.5 mm pitch); RGB parallel needs ~40-50 conductors; SPI/QSPI panels have
  a small connector with the driver IC on the flex. Count the pins.
- Look for silkscreen labels near the connector (`DSI`, `RGB`, `LCD`) and
  peel the tape on the panel flex to read the driver IC marking.
- Sanity check: a P4 board with a *DSI* panel is the case we want. A P4 with
  an RGB parallel panel would mean `mipi_rgb`, not `mipi_dsi`, and we would
  lose the DSI framebuffer tricks in tier 2.

---

## Appendix: why not the ESP32-S3 (original investigation)

Kept for the record — this is what ruled the S3 out.

- **Panel support was fine:** ESPHome's `mipi_rgb` has an
  `ESP32-S3-TOUCH-LCD-4.3` preset; GT911 touch works. Needs `esp-idf` +
  PSRAM.
- **Baked-in animations only, and tiny.** `image` / `animation` embeds **raw
  uncompressed** frames in flash: 800x480 RGB565 = 768 KB/frame. On this
  board's real 8 MB flash that is about 5 full-screen frames (~10 on the
  16 MB the product page advertises). A 200x200 sprite gets ~50, about 5 s at
  10 fps.
  Fine for a looping corner animation, useless for video. Also, full-frame
  `animimg` triggers 100 ms+ LVGL blocking warnings
  ([esphome/issues#6286](https://github.com/esphome/issues/issues/6286)).
- **`online_image`** (JPEG/PNG over HTTP into PSRAM, re-fetched on interval)
  gives slideshow-grade refresh, 1-3 fps, no GIF support.
- **No hardware JPEG decoder.** Software decode with SIMD-optimised
  `esp_new_jpeg` / `JPEGDEC` measured 20 ms for 272x233 on an S3
  (~3.2 Mpx/s), which extrapolates to **~8 fps at 800x480**. Independent
  reports of 800x480 panels on S3 land at 4-10 fps. microSD reads cap at
  ~1-1.7 MB/s.

Conclusion: smooth full-screen video is not reachable on an S3 in any
framework. The P4's hardware JPEG codec is the whole reason this project is
viable.

---

## Appendix: (b) local-network / Android control

Unchanged by the P4 switch — all of these ride on ESPHome's normal surfaces.
Ranked by effort:

1. **Home Assistant + companion app** (least work). ESPHome's native API
   (TCP 6053, protobuf) is HA's native transport; fully local, no cloud.
2. **`web_server` + PWA / WebView** (no HA needed). REST:
   `GET /<domain>/<name>`, `POST /<domain>/<name>/<action>` (`turn_on`,
   `turn_off`, `toggle`, `set`, `press`), plus server-sent events at
   `/events` for live state. Basic/digest auth.
   [ESPHome Web App](https://github.com/DanielBaulig/esphome-web-app) is a
   ready-made PWA over exactly this. Caveat: `web_server` "takes up a lot of
   memory" — a non-issue with 32 MB PSRAM.
3. **Native Android app over REST/SSE** — same endpoints, plain HTTP from
   Kotlin. Simplest path for a custom app.
4. **Native API from Android** — clients exist only in Python
   (`aioesphomeapi`) and JS; you would generate Kotlin stubs from
   `api.proto`. Not worth it over option 3.
5. **MQTT** — ESPHome speaks it directly; any MQTT Android client works.

The native API and `web_server` can coexist on one device, so HA integration
and a standalone app are not mutually exclusive.

---

## Sources

**P4 hardware / video**

- [Waveshare ESP32-P4-WIFI6-Touch-LCD-4.3 product page](https://www.waveshare.com/esp32-p4-wifi6-touch-lcd-4.3.htm) / [docs](https://docs.waveshare.com/ESP32-P4-WIFI6-Touch-LCD-4.3)
- [ESP-IDF JPEG codec (P4)](https://docs.espressif.com/projects/esp-idf/en/stable/esp32p4/api-reference/peripherals/jpeg.html) — decode perf figures, alignment rules
- [ESP-IDF MIPI DSI LCD (P4)](https://docs.espressif.com/projects/esp-idf/en/stable/esp32p4/api-reference/peripherals/lcd/dsi_lcd.html) — `esp_lcd_dpi_panel_get_frame_buffer()`, `num_fbs`, vsync hooks
- [Waveshare P4 example code](https://github.com/waveshareteam/ESP32-P4-WIFI6-Touch-LCD-X) — `10_mp4_player`, `09_video_lcd_display`
- [Espressif: ESP H.264 usage guide](https://developer.espressif.com/blog/2025/07/esp-h264-use-tips/)

**ESPHome**

- [`mipi_dsi` display driver](https://esphome.io/components/display/mipi_dsi/) / [source](https://github.com/esphome/esphome/blob/dev/esphome/components/mipi_dsi/mipi_dsi.cpp) / [Waveshare presets](https://github.com/esphome/esphome/blob/dev/esphome/components/mipi_dsi/models/waveshare.py)
- [PR #16853 — DMA2D async flush + framebuffer access](https://github.com/esphome/esphome/pull/16853)
- [Issue #10956 — P4/C6 esp32_hosted WiFi instability](https://github.com/esphome/esphome/issues/10956)
- [ESPHome 2026.3.0 changelog](https://esphome.io/changelog/2026.3.0/) — P4 engineering_sample option
- [ESP32-P4-Nano device page](https://devices.esphome.io/devices/waveshare-esp32-p4-nano/) — `esp32_hosted` YAML
- [web_server](https://esphome.io/components/web_server/) / [web API](https://esphome.io/web-api/) / [native API](https://esphome.io/components/api/)

**S3 appendix**

- [`mipi_rgb`](https://esphome.io/components/display/mipi_rgb/) / [Animation](https://esphome.io/components/image/animation/) / [Online Image](https://esphome.io/components/online_image/) / [LVGL widgets](https://esphome.io/components/lvgl/widgets/)
- [atomic14: A Faster ESP32 JPEG Decoder](https://www.atomic14.com/2023/09/30/a-faster-esp32-jpeg-decoder) / [Espressif ESP_NEW_JPEG](https://developer.espressif.com/blog/2025/09/esp-new-jpeg-introduction/)
