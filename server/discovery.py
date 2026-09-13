"""Zero-config discovery and push for mini screens.

Both directions are solved without any address being typed anywhere:

  server -> screens : each screen advertises `_minidisplay._tcp.local.` with
                      its name, panel size and preferred format in TXT
                      records, so browsing mDNS yields a complete registry.

  screens -> server : the screen never needs to know where the server is. On
                      discovering a screen, the server POSTs the content URL
                      into the screen's `content_url` text entity via its own
                      `web_server` REST API, filling in the address the screen
                      can actually reach it on (see `local_ip_toward`).

The same REST surface gives a genuine push channel: pressing the screen's
`refresh_content` button makes it fetch immediately, so new content lands in
well under a second instead of waiting for the next poll.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

import requests
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

SERVICE_TYPE = "_minidisplay._tcp.local."
HTTP_TIMEOUT = 4.0

# web_server routes we push to. The entity-NAME form is the supported one;
# the object-ID form ("content_url") is deprecated and ESPHome removes it in
# 2026.7.0, so it is only a fallback for older firmware. Each tuple is
# (preferred, legacy).
TEXT_SET_PATHS = (f"/text/{quote('Content URL')}/set", "/text/content_url/set")
BUTTON_PRESS_PATHS = (
    f"/button/{quote('Refresh Content')}/press",
    "/button/refresh_content/press",
)
# How many frames the current content has. The device caches and plays
# automatically when this is > 1, so the server -- the only side that knows
# whether the content is a clip -- drives the whole thing with one number.
# A device without a clip cache simply 404s, which is not an error.
# Two clip caches exist, and a screen has exactly one of them: "SD Clip
# Frames" on a board with a card, "Clip Frames" on one that caches in PSRAM.
# Each path is tried in turn and a 404 just means "not this board", so the same
# push works for both without the server needing to know which is which.
NUMBER_SET_PATHS = (
    f"/number/{quote('SD Clip Frames')}/set",
    "/number/sd_clip_frames/set",
    f"/number/{quote('Clip Frames')}/set",
    "/number/clip_frames/set",
)
# Read from screens that advertise `sd=1`, published by common/sd-card.yaml.
# key -> web_server route; sizes are in MB there.
SD_STATE_PATHS = {
    "mounted": f"/binary_sensor/{quote('SD Mounted')}",
    "in_use": f"/binary_sensor/{quote('SD In Use')}",
    "total_mb": f"/sensor/{quote('SD Total')}",
    "free_mb": f"/sensor/{quote('SD Free')}",
}
# Free space changes slowly and the device only re-measures once a minute.
SD_POLL_INTERVAL = 30.0
CONFIGURE_RETRIES = 5
CONFIGURE_BACKOFF = 3.0

log = logging.getLogger("discovery")


@dataclass
class Screen:
    name: str
    host: str
    port: int = 80
    width: int = 800
    height: int = 480
    fmt: str = "jpeg"
    fit: str = "contain"
    # None means "use the server's default"; a screen can pin its own via a
    # `q` mDNS TXT record without any change here.
    quality: int | None = None
    # Degrees clockwise, for a panel mounted turned. Declared via a `rot` TXT
    # record; the server applies it when rendering.
    rot: int = 0
    # Capabilities, from the `round`, `img`, `anim` and `sd` TXT records.
    round: bool = False
    # Still formats the firmware accepts.
    img: tuple[str, ...] = ("jpeg",)
    # Animation source formats it can play ("gif", "apng", "webp"). None means
    # the firmware predates the record, so nothing is known and clips are
    # offered as before; an empty tuple means stills only.
    anim: tuple[str, ...] | None = None
    # Stores stills on an SD card (when one is mounted).
    sd: bool = False
    # How clips reach the screen: "mjpeg" (one /clip.mjpeg download, played
    # from memory) or "frames" (per-frame fetches). None: firmware predates it.
    clip: str | None = None
    # Live card state read back from the screen (see Registry.refresh_sd):
    # {"mounted", "in_use", "total_mb", "free_mb", "at"}, or None if never read.
    sd_state: dict | None = None
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    configured_url: str | None = None
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "size": f"{self.width}x{self.height}",
            "fmt": self.fmt,
            "fit": self.fit,
            "quality": self.quality,
            "rot": self.rot,
            "caps": self.caps(),
            "sd_state": self.sd_state,
            "last_seen": self.last_seen,
            "configured_url": self.configured_url,
            "last_error": self.last_error,
        }

    def caps(self) -> dict:
        return {
            "x": self.width,
            "y": self.height,
            "round": self.round,
            "img": list(self.img),
            "anim": None if self.anim is None else list(self.anim),
            "sd": self.sd,
            "clip": self.clip,
        }

    def can_animate(self, source_format: str) -> bool:
        """Whether this screen plays a clip whose source is `source_format`
        ("gif", "apng", "webp"). Unknown firmware gets the benefit of the doubt."""
        return self.anim is None or source_format in self.anim

    def config_key(self) -> tuple:
        """Everything that, when it changes, means the screen needs re-pushing."""
        return (self.host, self.port, self.width, self.height, self.fmt,
                self.quality, self.rot, self.round, self.img, self.anim, self.sd,
                self.clip)


def _csv(value: str | None) -> tuple[str, ...] | None:
    """A comma-separated TXT value as a tuple; "none" or empty is ()."""
    if value is None:
        return None
    items = tuple(v.strip().lower() for v in value.split(",") if v.strip())
    return () if items in ((), ("none",)) else items


def local_ip_toward(host: str) -> str:
    """Our own address on the route to `host`.

    Opening a UDP socket and connecting sends no packets but makes the kernel
    pick a route, so this gives the right answer on a multi-homed server
    (docker bridge, VPN, two NICs) instead of whatever `gethostname` returns.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


class Registry:
    """Thread-safe set of discovered screens, keyed by mDNS instance."""

    def __init__(self, content_port: int) -> None:
        self.content_port = content_port
        # Set by app.py: Screen -> frame count of whatever it is showing, as
        # that screen should play it (1 if it cannot animate the source).
        # A callback rather than a lookup, so discovery keeps knowing nothing
        # about the library.
        self.frames_provider = None
        # Returns a token identifying a device's current image, or None.
        self.version_provider = None
        self._screens: dict[str, Screen] = {}
        self._lock = threading.Lock()

    # -- reads -------------------------------------------------------------
    def all(self) -> list[Screen]:
        with self._lock:
            return sorted(self._screens.values(), key=lambda s: s.name)

    def for_device(self, device: str) -> list[Screen]:
        with self._lock:
            return [s for s in self._screens.values() if s.name == device]

    def known_names(self) -> set[str]:
        with self._lock:
            return {s.name for s in self._screens.values()}

    # -- writes ------------------------------------------------------------
    def put(self, key: str, screen: Screen) -> Screen | None:
        """Store a screen. Returns it if it needs (re)configuring."""
        with self._lock:
            existing = self._screens.get(key)
            if (
                existing
                and existing.config_key() == screen.config_key()
                and existing.configured_url
            ):
                existing.last_seen = time.time()
                return None  # unchanged and already configured
            screen.first_seen = existing.first_seen if existing else time.time()
            if existing and screen.sd:
                screen.sd_state = existing.sd_state  # keep showing it meanwhile
            self._screens[key] = screen
            return screen

    def drop(self, key: str) -> None:
        with self._lock:
            self._screens.pop(key, None)

    def mark(self, key: str, url: str | None, error: str | None) -> None:
        with self._lock:
            if (s := self._screens.get(key)) is not None:
                s.configured_url = url
                s.last_error = error

    # -- push --------------------------------------------------------------
    def content_url_for(self, screen: Screen) -> str:
        params = {
            "w": screen.width,
            "h": screen.height,
            "fmt": screen.fmt,
            "fit": screen.fit,
        }
        if screen.quality is not None:
            params["q"] = screen.quality
        if screen.rot:
            params["rot"] = screen.rot
        # A version token for the CURRENT image. The other parameters describe
        # the panel, not the content, so without this the URL is identical for
        # every image a screen ever shows -- and a device caching frames by
        # index has no way to notice the clip changed underneath it.
        if self.version_provider is not None:
            try:
                token = self.version_provider(screen.name)
            except Exception as exc:  # noqa: BLE001 - provider is app-supplied
                log.warning("version_provider failed for %s: %s", screen.name, exc)
                token = None
            if token:
                params["v"] = token
        query = urlencode(params)
        host = local_ip_toward(screen.host)
        return f"http://{host}:{self.content_port}/d/{screen.name}/image?{query}"

    def configure(self, key: str, screen: Screen) -> None:
        """Tell a screen where to fetch from. Retries: it may still be booting."""
        url = self.content_url_for(screen)
        base = f"http://{screen.host}:{screen.port}"
        for attempt in range(1, CONFIGURE_RETRIES + 1):
            err = None
            for path in TEXT_SET_PATHS:
                try:
                    r = requests.post(
                        base + path, params={"value": url}, timeout=HTTP_TIMEOUT
                    )
                except requests.RequestException as exc:
                    err = str(exc)
                    break  # transport problem: retrying another path won't help
                if r.status_code == 200:
                    log.info("configured %s -> %s", screen.name, url)
                    self.mark(key, url, None)
                    self.push_frames(screen)
                    self.refresh_sd(screen)  # so the page has it right away
                    return
                if r.status_code == 404:
                    err = (
                        "no 'Content URL' text entity (is common/content-pull.yaml "
                        "in the device config?)"
                    )
                    continue  # try the legacy object-ID form
                err = f"HTTP {r.status_code} from {base + path}"
                break
            log.warning(
                "configure %s failed (attempt %d/%d): %s",
                screen.name,
                attempt,
                CONFIGURE_RETRIES,
                err,
            )
            self.mark(key, None, err)
            if attempt < CONFIGURE_RETRIES:
                time.sleep(CONFIGURE_BACKOFF)

    def push_frames(self, screen: Screen) -> bool:
        """Tell a screen how many frames its current content has."""
        if self.frames_provider is None:
            return False
        try:
            count = int(self.frames_provider(screen))
        except Exception as exc:  # noqa: BLE001 - provider is app-supplied
            log.warning("frames_provider failed for %s: %s", screen.name, exc)
            return False
        base = f"http://{screen.host}:{screen.port}"
        for path in NUMBER_SET_PATHS:
            try:
                r = requests.post(
                    base + path, params={"value": count}, timeout=HTTP_TIMEOUT
                )
            except requests.RequestException as exc:
                log.warning("frame count push to %s failed: %s", screen.name, exc)
                return False
            if r.status_code == 200:
                log.info("told %s it has %d frame(s)", screen.name, count)
                return True
            if r.status_code != 404:
                return False
        return False  # no such entity: this screen has no clip cache

    def refresh_sd(self, screen: Screen) -> dict | None:
        """Read the card state from a screen that stores stills on SD.

        Stored on the screen and returned; None (and the old state kept) when
        the screen does not answer, so one missed poll does not blank the page.
        A 404 means firmware without the SD entities, which is not an error.
        """
        if not screen.sd:
            return None
        base = f"http://{screen.host}:{screen.port}"
        state: dict = {}
        for key, path in SD_STATE_PATHS.items():
            try:
                r = requests.get(base + path, timeout=HTTP_TIMEOUT)
            except requests.RequestException as exc:
                log.debug("sd state from %s failed: %s", screen.name, exc)
                return None
            if r.status_code == 404:
                state[key] = None
                continue
            if r.status_code != 200:
                return None
            try:
                value = r.json().get("value")
            except ValueError:
                return None
            if key in ("mounted", "in_use"):
                state[key] = bool(value)
            else:
                # NaN (no card) arrives as null or a non-number.
                state[key] = (
                    float(value)
                    if isinstance(value, (int, float)) and value == value
                    else None
                )
        state["at"] = time.time()
        with self._lock:
            screen.sd_state = state
        return state

    def poll_sd_forever(self, interval: float = SD_POLL_INTERVAL) -> None:
        while True:
            for screen in self.all():
                if screen.sd:
                    self.refresh_sd(screen)
            time.sleep(interval)

    def push_url(self, screen: Screen) -> bool:
        """Re-send the content URL if it changed since the screen was configured.

        The URL's `v` token names the current content (item + framing), so a
        screen that caches content can tell from the URL alone whether it
        already holds what it is being switched to -- and show it without
        pulling anything. Returns True if a new URL was delivered.
        """
        url = self.content_url_for(screen)
        if url == screen.configured_url:
            return False
        base = f"http://{screen.host}:{screen.port}"
        for path in TEXT_SET_PATHS:
            try:
                r = requests.post(base + path, params={"value": url}, timeout=HTTP_TIMEOUT)
            except requests.RequestException as exc:
                log.warning("url push to %s failed: %s", screen.name, exc)
                return False
            if r.status_code == 200:
                with self._lock:
                    screen.configured_url = url
                log.info("switched %s -> %s", screen.name, url)
                return True
            if r.status_code != 404:
                return False
        return False

    def notify(self, device: str) -> int:
        """Push: make every screen for `device` fetch now. Returns how many."""
        pushed = 0
        for screen in self.for_device(device):
            base = f"http://{screen.host}:{screen.port}"
            # URL first: a caching screen decides from it whether the refresh
            # below needs the network at all.
            self.push_url(screen)
            for path in BUTTON_PRESS_PATHS:
                try:
                    r = requests.post(base + path, timeout=HTTP_TIMEOUT)
                except requests.RequestException as exc:
                    log.warning("refresh %s failed: %s", screen.name, exc)
                    break
                if r.status_code == 200:
                    pushed += 1
                    # Content may have changed from a still to a clip or back.
                    self.push_frames(screen)
                    break
                if r.status_code != 404:
                    log.warning("refresh %s: HTTP %s", screen.name, r.status_code)
                    break
        if pushed:
            log.info("pushed refresh to %d screen(s) for %s", pushed, device)
        return pushed


class _Listener(ServiceListener):
    def __init__(self, registry: Registry, only: set[str] | None = None) -> None:
        self.registry = registry
        # When set, every other screen on the network is ignored -- never
        # configured, never pushed to. Tests need this: they run on the real
        # LAN, and without it a test run re-pointed a real screen at the test
        # process and hammered its Refresh button.
        self.only = only

    def _handle(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=3000)
        if info is None:
            log.warning("no service info for %s", name)
            return

        addresses = info.parsed_addresses()
        if not addresses:
            log.warning("no address for %s", name)
            return

        txt = {
            k.decode("utf-8", "replace"): (v or b"").decode("utf-8", "replace")
            for k, v in (info.properties or {}).items()
        }

        def as_int(key: str, default: int) -> int:
            try:
                return int(txt.get(key, default))
            except (TypeError, ValueError):
                return default

        # `name` comes from a TXT record rather than being parsed out of the
        # mDNS instance string -- the device states its own identity, so the
        # content path can never drift from what the screen thinks it is.
        device = txt.get("name") or name.split(".")[0]
        if self.only is not None and device not in self.only:
            return
        screen = Screen(
            name=device,
            host=addresses[0],
            port=info.port or 80,
            width=as_int("w", 800),
            height=as_int("h", 480),
            fmt=txt.get("fmt", "jpeg"),
            fit=txt.get("fit", "contain"),
            quality=as_int("q", 0) or None,
            rot=as_int("rot", 0) % 360,
            round=txt.get("round", "0") in ("1", "true", "yes"),
            img=_csv(txt.get("img")) or (txt.get("fmt", "jpeg"),),
            anim=_csv(txt.get("anim")),
            sd=txt.get("sd", "0") in ("1", "true", "yes"),
            clip=(txt.get("clip") or "").strip().lower() or None,
        )

        if (needs_config := self.registry.put(name, screen)) is not None:
            log.info(
                "discovered %s at %s:%d (%dx%d %s)",
                screen.name,
                screen.host,
                screen.port,
                screen.width,
                screen.height,
                screen.fmt,
            )
            threading.Thread(
                target=self.registry.configure,
                args=(name, needs_config),
                daemon=True,
            ).start()

    add_service = _handle
    update_service = _handle

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        log.info("screen went away: %s", name)
        self.registry.drop(name)


def start(
    content_port: int, browse: bool = True, only: set[str] | None = None
) -> tuple[Registry, Zeroconf | None]:
    """Start discovery. `browse=False` gives an empty registry that never
    touches the network; `only` restricts it to the named screens."""
    registry = Registry(content_port)
    if not browse:
        log.info("discovery disabled")
        return registry, None
    zc = Zeroconf()
    ServiceBrowser(zc, SERVICE_TYPE, _Listener(registry, only))
    threading.Thread(target=registry.poll_sd_forever, daemon=True).start()
    log.info("browsing for %s%s", SERVICE_TYPE,
             f" (only {', '.join(sorted(only))})" if only is not None else "")
    return registry, zc
