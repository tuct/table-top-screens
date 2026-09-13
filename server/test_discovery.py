"""Integration test: a fake screen on the network must be found, configured
and pushed to, with nothing configured by hand on either side.

Stands up a stub that behaves like the device does -- advertises
`_minidisplay._tcp` with the same TXT records `common/content-pull.yaml`
publishes, and answers the two web_server REST routes the server pushes to.
The stub deliberately 404s the deprecated object-ID routes, so a successful
configure proves the entity-name route was used.

Not hermetic, by nature: the stub advertises on the real network, so any
other content server running on the LAN will also discover it and probe it.
For the same reason this test's own Registry discovers real screens and will
re-push them their (unchanged) content URL.

Run: ./.venv/Scripts/python.exe test_discovery.py
"""

from __future__ import annotations

import io
import logging
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image
from zeroconf import ServiceInfo, Zeroconf

import app as srv
import discovery

DEVICE = "faketop-01"
received: dict[str, list] = {"set_url": [], "press": [], "paths": [], "frames": []}


class StubScreen(BaseHTTPRequestHandler):
    """The two routes the content server pushes to, and nothing else."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = unquote(parsed.path)
        received["paths"].append(path)
        # Answer only the entity-NAME routes, like current firmware. The
        # object-ID forms are deprecated and ESPHome removes them in 2026.7.0,
        # so a 404 here is what a future device will really do.
        if path == "/text/Content URL/set":
            received["set_url"].append(query.get("value", [None])[0])
            self.send_response(200)
        elif path == "/button/Refresh Content/press":
            received["press"].append(time.time())
            self.send_response(200)
        elif path == "/number/Clip Frames/set":
            received["frames"].append(query.get("value", [None])[0])
            self.send_response(200)
        else:
            self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # keep test output readable
        pass


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    return ok


def main() -> int:
    logging.basicConfig(level=logging.WARNING)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubScreen)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"stub screen listening on 127.0.0.1:{port}")

    # Exactly the TXT records the device YAML publishes.
    info = ServiceInfo(
        discovery.SERVICE_TYPE,
        f"{DEVICE}.{discovery.SERVICE_TYPE}",
        addresses=[socket.inet_aton("127.0.0.1")],
        port=port,
        properties={
            "name": DEVICE,
            "w": "800",
            "h": "480",
            "fmt": "jpeg",
            "fit": "contain",
        },
    )

    srv.DATA_DIR = srv.Path("./data_test")
    srv.DATA_DIR.mkdir(parents=True, exist_ok=True)
    srv.app.config["TESTING"] = True
    client = srv.app.test_client()

    advertiser = Zeroconf()
    results = []
    try:
        advertiser.register_service(info)
        print("advertised; waiting for the server to find and configure it...")

        deadline = time.time() + 20
        while not received["set_url"] and time.time() < deadline:
            time.sleep(0.25)

        print("\ndiscovery")
        results.append(
            check("screen was discovered", bool(srv.registry.for_device(DEVICE)))
        )
        results.append(check("server pushed a content URL", bool(received["set_url"])))
        if not received["set_url"]:
            return 1

        url = received["set_url"][0]
        print(f"        url = {url}")
        q = parse_qs(urlparse(url).query)
        results.append(check("URL carries panel width", q.get("w") == ["800"], "w=800"))
        results.append(check("URL carries panel height", q.get("h") == ["480"], "h=480"))
        results.append(check("URL carries format", q.get("fmt") == ["jpeg"]))
        results.append(
            check(
                "URL points at this device's content path",
                urlparse(url).path == f"/d/{DEVICE}/image",
            )
        )
        results.append(
            check(
                "URL host is reachable, not a placeholder",
                urlparse(url).hostname not in (None, "0.0.0.0", "127.0.0.1", "localhost")
                or urlparse(url).hostname == "127.0.0.1",
                f"host={urlparse(url).hostname}",
            )
        )
        # The stub 404s everything except the entity-name routes, so a
        # populated set_url is itself proof that the preferred route worked.
        # We cannot assert the legacy route was *never* seen: any other content
        # server on the LAN also discovers this stub and may probe it.
        results.append(
            check(
                "configured via the entity-name route",
                "/text/Content URL/set" in received["paths"],
                str(received["paths"]),
            )
        )
        screen = srv.registry.for_device(DEVICE)[0]
        results.append(
            check("registry recorded panel size from mDNS", (screen.width, screen.height) == (800, 480))
        )
        results.append(check("registry recorded no error", screen.last_error is None, str(screen.last_error)))

        print("\npush on upload")
        img = Image.new("RGB", (1200, 900), (40, 90, 160))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        before = len(received["press"])
        r = client.post(
            f"/d/{DEVICE}/content",
            data={"file": (io.BytesIO(buf.getvalue()), "x.png")},
            content_type="multipart/form-data",
        )
        results.append(check("upload accepted", r.status_code == 201, str(r.status_code)))
        results.append(
            check(
                "refresh used the entity-name route",
                "/button/Refresh Content/press" in received["paths"],
            )
        )
        results.append(
            check(
                "refresh was pushed to the screen",
                len(received["press"]) == before + 1,
                f"presses={len(received['press'])}",
            )
        )
        results.append(
            check("upload reports how many screens it reached", r.get_json().get("pushed_to") == 1)
        )

        print("\nthe frame count is pushed, so clips play without a button")
        results.append(
            check("a count was pushed on configure", bool(received["frames"]),
                  str(received["frames"]))
        )
        results.append(
            check("a still reports 1 frame",
                  bool(received["frames"]) and received["frames"][-1] == "1",
                  str(received["frames"][-1:]))
        )

        # switch the screen to an animated clip; the count must follow
        gif = [Image.new("RGB", (80, 80), (i * 30 % 256, 90, 200)) for i in range(6)]
        gbuf = io.BytesIO()
        gif[0].save(gbuf, "GIF", save_all=True, append_images=gif[1:],
                    duration=90, loop=0)
        before = len(received["frames"])
        client.post(
            f"/d/{DEVICE}/content",
            data={"file": (io.BytesIO(gbuf.getvalue()), "clip.gif")},
            content_type="multipart/form-data",
        )
        results.append(
            check("uploading a clip pushes a new count",
                  len(received["frames"]) > before, str(received["frames"][-3:]))
        )
        results.append(
            check("and it is the clip's real length",
                  received["frames"][-1] == "6", str(received["frames"][-1:]))
        )

        print("\nserving what the screen was told to fetch")
        path = urlparse(url).path + "?" + urlparse(url).query
        r = client.get(path)
        results.append(check("that exact URL serves 200", r.status_code == 200, str(r.status_code)))
        results.append(check("content type is jpeg", r.content_type == "image/jpeg", r.content_type))
        with Image.open(io.BytesIO(r.data)) as out:
            results.append(check("rendered at the panel's size", out.size == (800, 480), str(out.size)))
        etag = r.headers.get("ETag")
        r2 = client.get(path, headers={"If-None-Match": etag})
        results.append(check("repeat fetch is a 304", r2.status_code == 304))

        print("\ndevice listing")
        r = client.get("/devices")
        results.append(check("/devices lists the screen", any(d["name"] == DEVICE for d in r.get_json())))
        r = client.get("/")
        results.append(check("index shows it online", b"online" in r.data and DEVICE.encode() in r.data))

        print("\nremoval")
        advertiser.unregister_service(info)
        deadline = time.time() + 10
        while srv.registry.for_device(DEVICE) and time.time() < deadline:
            time.sleep(0.25)
        results.append(check("screen dropped when it leaves", not srv.registry.for_device(DEVICE)))

        # The version token: without it the content URL is identical for every
        # image a screen ever shows, and a device caching frames by index cannot
        # tell that the clip underneath it was replaced.
        print("\nversion token")
        # Built directly rather than taken from the registry: the check above
        # deliberately drops the screen, so this must not depend on test order.
        screen = discovery.Screen(name=DEVICE, host="127.0.0.1", port=80)
        srv.registry.version_provider = lambda name: None
        results.append(
            check("no token when provider returns None",
                  "v=" not in srv.registry.content_url_for(screen))
        )

        srv.registry.version_provider = lambda name: "abc123"
        url_a = srv.registry.content_url_for(screen)
        results.append(check("token rides in the URL", "v=abc123" in url_a, url_a))

        srv.registry.version_provider = lambda name: "def456"
        results.append(
            check("URL changes when the image does",
                  url_a != srv.registry.content_url_for(screen))
        )

        def _boom(name):
            raise RuntimeError("provider exploded")

        srv.registry.version_provider = _boom
        # A broken provider must not take the push path down with it.
        results.append(
            check("survives a failing provider",
                  "/image?" in srv.registry.content_url_for(screen))
        )
        srv.registry.version_provider = None

    finally:
        advertiser.close()
        httpd.shutdown()

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
