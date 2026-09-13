# Content server

One server, N screens, N resolutions. **Nothing is configured on either
side** — being on the same network is the only requirement.

## How the two halves find each other

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

**Server finds screens** by browsing `_minidisplay._tcp.local.`. The device
publishes its name and panel size as TXT records, so the registry is complete
without a config file. The name comes from a TXT record rather than being
parsed out of the mDNS instance string — the device states its own identity,
so the content path can't drift from what the screen thinks it is.

### Capabilities

Each screen also says what it can do, in the same TXT record:

| Key | Example | Meaning |
|---|---|---|
| `w`, `h` | `800`, `480` | panel size in pixels |
| `round` | `0` / `1` | visible area is a circle |
| `img` | `jpeg,rgb565` | still formats the firmware accepts |
| `anim` | `gif,apng,webp` or `none` | animation sources it can play |
| `sd` | `0` / `1` | stores stills on an SD card when one is mounted |

The server acts on these. An animated item whose format is not in `anim` is
pushed as **1 frame**, so the screen never starts a clip cache and simply shows
frame 0 (which is what `/image` renders anyway). The pages show each screen's
capabilities, badge such items "still here", and draw round screens' previews
as circles. A screen with no `anim` record at all is older firmware: it is
offered clips as before. `/devices` includes a `caps` object per screen.

**Screens find the server** by not needing to. On discovery the server POSTs
the content URL into the screen's `content_url` text entity over
`web_server`'s REST API, filling in *the address that screen can actually
reach it on* — computed per device by opening a UDP socket toward it and
reading back the local address. That sends no packets but makes the kernel
pick a route, so it's correct on a multi-homed server (docker bridge, VPN,
two NICs) where `gethostname()` would give the wrong answer.

## Push, not just poll

The same REST surface is a genuine push channel: on upload, the server POSTs
`/button/refresh_content/press` and the screen fetches immediately. New
content lands in well under a second.

The device keeps a slow background poll (60s) purely as a safety net for a
missed push. That's nearly free, because `online_image` implements ETag /
Last-Modified caching — an unchanged image is one 304 with an empty body.

Pulling is required for the image itself: ESPHome cannot receive a pushed
image, since `web_server` exposes fixed per-entity endpoints and not upload
routes. Pushing a *notification* over those endpoints and letting the device
pull the bytes gets push latency without a custom C++ component.

### Entity-name contract

The server POSTs to two fixed routes, so these entity IDs in
`common/content-pull.yaml` are a contract:

| Entity | Route the server uses |
|---|---|
| `text.content_url` | `POST /text/content_url/set?value=<url>` |
| `button.refresh_content` | `POST /button/refresh_content/press` |

Rename either and `discovery.py` needs the same change. A 404 on the text
route is treated as a config mismatch (reported on the index page), not a
transient fault, so it fails loudly instead of retrying forever.

## Run it

```bash
# macOS / Linux
cd server
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python app.py

# Windows
cd server
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe app.py
```

Listens on `0.0.0.0:8099`. Open it and screens appear on their own.

Two environment variables limit discovery. `SCREENS_DISCOVERY=0` turns it
off, and `SCREENS_ONLY=tabletop-01,tabletop-02` ignores every other screen.
The tests set these themselves. Without them, a test run on the same network
re-pointed a real screen at the test process and pressed its Refresh button
dozens of times.

For screens advertising `sd=1`, the server also reads `SD Mounted`,
`SD In Use`, `SD Total` and `SD Free` every 30 s and shows them on the
overview ("SD card: stills on card · 27.4 of 29.7 GB free").

Notes for a permanent install:

- mDNS needs **UDP 5353** and the server on the **same L2 segment** as the
  screens. Across VLANs it needs an mDNS reflector/repeater.
- In Docker, use `--network host`; a bridge network breaks both multicast
  discovery and the local-IP calculation.
- For anything beyond a test, run it behind a real WSGI server:
  `pip install waitress && waitress-serve --port=8099 app:app`

## Sending content

**From a phone** — open `http://<server>:8099/`, tap the screen you want, and
use the upload form. The file picker offers the camera directly, so this
doubles as the "Android app" until there's a real one.

**From a script**

```bash
curl -F file=@photo.jpg http://<server>:8099/d/tabletop-01/content
# or raw
curl --data-binary @photo.jpg -H 'Content-Type: image/jpeg' \
     http://<server>:8099/d/tabletop-01/content
```

Uploads are validated by actually decoding them, so a bad file fails at
upload time rather than on the device. The response's `pushed_to` says how
many live screens were told to refresh.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Live screens + stored content, auto-refreshing |
| `GET` | `/devices` | Raw discovery registry (JSON) |
| `GET` | `/d/<device>/` | Upload form + preview |
| `POST` | `/d/<device>/content` | Store content and push a refresh |
| `POST` | `/d/<device>/prefs` | Set `fit`/`rot`/`q`/`bg` overrides and push (`reset=1` clears) |
| `GET` | `/d/<device>/items` | The library: ordered items + which is current |
| `GET` | `/d/<device>/items/<id>/thumb` | 160x96 PNG thumbnail |
| `POST` | `/d/<device>/items/<id>/select` | Show this item, and push |
| `DELETE` | `/d/<device>/items/<id>` | Remove one item (`POST .../delete` for forms) |
| `POST` | `/d/<device>/order` | Apply a drag-and-drop order: `{"ids": [...]}` |
| `DELETE` | `/d/<device>/content` | Clear content |
| `GET` | `/d/<device>/meta` | Stored content metadata (JSON) |
| `GET` | `/d/<device>/image` | Render for a device — **what the screen calls** |
| `GET` | `/healthz` | Liveness + whether QOI is available |

### Framing from the device page

`/d/<device>/` previews the framing live: changing fit, rotation, letterbox or
quality re-renders the preview at the panel's real aspect ratio **without
touching the screen** (the preview requests `prefs=0`, so it shows your
selection rather than what is stored). **Apply to screen** then saves the
override and pushes a refresh; **Reset** clears it and falls back to whatever
the device asked for over mDNS.

Precedence: stored prefs beat the query string for `fit`, `rot`, `q` and `bg`,
because they are a deliberate choice made after the device announced its
defaults. `w` and `h` always come from the request — those are hardware facts.
`prefs=0` opts out of the whole mechanism.

### `GET /d/<device>/image` parameters

The device doesn't choose these by hand — the server builds this URL from the
screen's mDNS TXT records (`w`, `h`, `fmt`, `fit`, `rot`, `q`) and pushes it.
Useful for testing by hand, though.

| Param | Default | Notes |
|---|---|---|
| `w`, `h` | `800`, `480` | Target size, max 4096. Always from the request — hardware, not preference |
| `fmt` | `jpeg` | `jpeg`, `png`, `qoi` |
| `fit` | `contain` | see below |
| `rot` | `0` | `0`/`90`/`180`/`270`, clockwise |
| `q` | `95` | JPEG quality |
| `bg` | `black` | Letterbox fill: any CSS colour, `#rrggbb`, or `auto` |
| `prefs` | `1` | `0` ignores stored overrides — used by the page to preview |

**Fit modes.** All produce exactly `w x h`:

| `fit` | Behaviour |
|---|---|
| `contain` | scale to fit entirely inside, letterbox the remainder |
| `cover` | scale to fill, crop the overflow |
| `width` (alias `x`) | match the width exactly; crop or pad the height |
| `height` (alias `y`) | match the height exactly; crop or pad the width |
| `stretch` | distort to fill |

**`rot`** is for a panel mounted turned. It is applied *after* fitting, and for
90/270 the source is fitted into a swapped box first, so the result is always
exactly `w x h` and content stays upright and full-bleed once the screen is
physically rotated. Rotating here rather than with ESPHome's display
`rotation:` matters: that option forces the buffered draw path and would
destroy the device's zero-copy image draw.

**`bg=auto`** fills each letterbox bar with the median colour of the picture
edge it touches, sampled a few pixels deep (median, so a thin bright edge or
JPEG ringing cannot skew it). Because only one axis ever has slack after
fitting, the two bars are independent — a landscape with sky above and grass
below gets a sky-coloured top bar and a grass-coloured bottom one. An
unparseable colour falls back to black rather than failing the render.

Renders are cached in memory keyed by source hash + parameters, so several
screens asking for different sizes each pay the resize once.

## Format choice

- **JPEG** (default) — smallest, and the S3 decodes baseline JPEG fine. Output
  is forced non-progressive; ESPHome's decoder handles baseline only.
- **PNG** — lossless, but slower to decode on-device and much bigger.
- **QOI** — lossless *and* far faster to decode than PNG, at roughly PNG-ish
  size. Genuinely the best option for flat/UI content on a slow MCU. Optional
  because it needs `qoi` + `numpy`; uncomment them in `requirements.txt`, then
  change `fmt` in the device's mDNS TXT records **and** `format: QOI` in the
  device YAML — the two must agree, since the device states its format
  explicitly at compile time.

## Library

Every image sent to a device is kept, not just the latest:

```
data/<device>/
    index.json      ordered item list + which one is current
    items/<id>      the original uploaded bytes, verbatim
    prefs.json      render overrides
```

`index.json` stores the list in **playlist order**. It starts chronological
because uploads append, and drag-and-drop rewrites it. That ordering is
persisted rather than derived from timestamps because it is what a future
slideshow will walk.

The device page lists the library with thumbnails, the current item
highlighted, drag-to-reorder (saved on drop), a **Show** button per row and a
remove button.

Ids are random, not content hashes, so sending the same picture twice
correctly gives two entries. Reordering is deliberately tolerant: ids the
client omits keep their relative order at the end and unknown ids are
ignored, so a stale browser tab cannot lose your library. Deleting the
current item promotes its neighbour rather than leaving the screen dangling,
and an index entry whose file has gone missing is dropped on read instead of
turning into a 500.

The old single-`source` layout is adopted into the library automatically on
first access, keeping its filename and timestamp.

**Only originals are stored — never rendered output.** So changing a panel
size, fit, rotation, quality or format re-renders from source with no
re-upload, and two screens of different sizes share one library. Renders live
in a 32-entry in-memory LRU keyed by `(image sha, parameters)`.

## Tests

```bash
./.venv/bin/python test_library.py      # 140 checks, no network
./.venv/bin/python test_content.py      # 121 checks, no network
./.venv/bin/python test_discovery.py    # 38 checks, uses real mDNS
```

(On Windows: `./.venv/Scripts/python.exe` instead of `./.venv/bin/python`.)

`test_library.py` covers accumulation and ordering, thumbnails, selection
following through to the rendered output, deleting the *current* item,
partial and malformed reorders, adoption of the legacy layout, and an index
entry whose file has vanished.

`test_content.py` covers the parts a screen depends on being correct: that it
gets its panel's exact pixel dimensions, that the JPEG is baseline (ESPHome
decodes baseline only), that an unchanged image really is a 304 with an empty
body, and that bad input is rejected at upload rather than on the device.

`test_discovery.py` stands up a stub that behaves like the real device —
advertises the same mDNS service and TXT records, answers the same two REST
routes — then checks the whole loop: discovered, configured with its own
panel size, pushed to on upload, that exact URL serves a correctly-sized
JPEG, a repeat fetch is a 304, and the screen is dropped when it leaves the
network. It registers a real mDNS service on the loopback interface, so it
needs UDP 5353 to be usable locally.
