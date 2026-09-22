"""Pool + per-screen assignment tests.

Covers the parts that are easy to get wrong and painful to debug later: that
the pool stores each image once, that a screen's two lists stay disjoint and
together cover the pool, that removing from a screen is *not* deleting from
the pool, and that both earlier storage layouts are adopted rather than
orphaned.

Run: ./.venv/Scripts/python.exe test_library.py
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
from pathlib import Path

from PIL import Image

# Before importing app: never discover, configure or push to real screens.
os.environ["SCREENS_DISCOVERY"] = "0"

import app as srv  # noqa: E402

DATA_TEST = Path("./data_lib_test")
FORM = "application/x-www-form-urlencoded"
BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    results.append(bool(ok))


def png(colour, size=(600, 400)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


def upload(c, device, colour, name, size=(600, 400)):
    return c.post(
        f"/d/{device}/content",
        data={"file": (io.BytesIO(png(colour, size)), name)},
        content_type="multipart/form-data",
    )


def lists(c, device):
    """(payload, sources used, sources not used).

    A screen shows VARIANTS, so `used` rows carry the variant id as `id` and
    the picture as `src_id`; these tests are about which picture is where.
    """
    j = c.get(f"/d/{device}/items").get_json()
    return j, [i["src_id"] for i in j["used"]], [i["id"] for i in j["unused"]]


def main() -> int:
    srv.DATA_DIR = DATA_TEST
    shutil.rmtree(DATA_TEST, ignore_errors=True)
    DATA_TEST.mkdir(parents=True)
    srv.app.config["TESTING"] = True
    c = srv.app.test_client()

    print("the pool stores each image once")
    r = upload(c, "a", (200, 0, 0), "red.png")
    check("upload accepted", r.status_code == 201, str(r.status_code))
    first_id = r.get_json()["id"]
    r2 = upload(c, "a", (200, 0, 0), "red-again.png")
    check("identical bytes reuse the pool entry", r2.get_json()["id"] == first_id)
    check("pool has one image, not two", len(c.get("/pool").get_json()) == 1)
    upload(c, "a", (0, 200, 0), "green.png")
    upload(c, "a", (0, 0, 200), "blue.png")
    check("pool now has three", len(c.get("/pool").get_json()) == 3)
    check("pool is newest first",
          c.get("/pool").get_json()[0]["filename"] == "blue.png")

    print("\nupload assigns to the screen and shows it")
    j, used, unused = lists(c, "a")
    check("all three used by screen a", len(used) == 3, str(len(used)))
    check("nothing unused for a", unused == [], str(unused))
    check("newest is on screen", j["current_src"] == used[-1])

    print("\na second screen starts with everything unused")
    j, used_b, unused_b = lists(c, "b")
    check("screen b uses nothing", used_b == [], str(used_b))
    check("b sees the whole pool as unused", len(unused_b) == 3, str(len(unused_b)))
    check("b has nothing on screen", j["current"] is None)
    check("b 404s until it picks something",
          c.get("/d/b/image?w=80&h=48").status_code == 404)

    pool_ids = [i["id"] for i in c.get("/pool").get_json()]

    print("\nadding from the pool")
    check("add accepted", c.post(f"/d/b/items/{pool_ids[0]}/add").status_code == 200)
    j, used_b, unused_b = lists(c, "b")
    check("now used by b", used_b == [pool_ids[0]], str(used_b))
    check("gone from b's unused list", pool_ids[0] not in unused_b)
    check("the two lists are disjoint", not (set(used_b) & set(unused_b)))
    check("together they are the whole pool",
          sorted(used_b + unused_b) == sorted(pool_ids))
    check("first add becomes current, there being nothing", j["current_src"] == pool_ids[0])
    c.post(f"/d/b/items/{pool_ids[1]}/add")
    check("a later add does NOT steal the screen",
          c.get("/d/b/items").get_json()["current_src"] == pool_ids[0])
    check("adding twice is harmless",
          c.post(f"/d/b/items/{pool_ids[1]}/add").status_code == 200)
    check("still two used", len(lists(c, "b")[1]) == 2)
    check("adding an unknown id 404s",
          c.post("/d/b/items/0000000000000000/add").status_code == 404)

    print("\nselect shows it, assigning first if needed")
    check("select accepted", c.post(f"/d/b/items/{pool_ids[2]}/select").status_code == 200)
    j, used_b, _ = lists(c, "b")
    check("select assigned it too", pool_ids[2] in used_b)
    check("and put it on screen", j["current_src"] == pool_ids[2])
    shown = c.get("/d/b/image?w=80&h=48&fmt=png").data
    c.post(f"/d/b/items/{pool_ids[1]}/select")
    check("the render follows the selection",
          c.get("/d/b/image?w=80&h=48&fmt=png").data != shown)

    print("\nremoving from a screen is not deleting from the pool")
    r = c.post(f"/d/b/items/{pool_ids[1]}/remove", data="", content_type=FORM,
               headers=BROWSER)
    check("remove redirects for a browser", r.status_code == 303, str(r.status_code))
    j, used_b, unused_b = lists(c, "b")
    check("no longer used by b", pool_ids[1] not in used_b)
    check("back in b's unused list", pool_ids[1] in unused_b)
    check("still in the pool",
          any(i["id"] == pool_ids[1] for i in c.get("/pool").get_json()))
    check("still used by screen a", pool_ids[1] in lists(c, "a")[1])
    check("current reassigned, not left dangling", j["current"] is not None)
    check("b still renders", c.get("/d/b/image?w=80&h=48").status_code == 200)
    check("removing something not used 404s",
          c.delete(f"/d/b/items/{pool_ids[1]}").status_code == 404)

    print("\ndeleting from the pool removes it everywhere")
    victim = pool_ids[0]
    r = c.delete(f"/pool/{victim}")
    check("delete accepted", r.status_code == 200, str(r.status_code))
    check("it names the screens affected",
          sorted(r.get_json()["affected"]) == ["a", "b"], str(r.get_json()))
    check("gone from the pool",
          not any(i["id"] == victim for i in c.get("/pool").get_json()))
    check("gone from a", victim not in lists(c, "a")[1])
    check("gone from b", victim not in lists(c, "b")[1])
    check("file removed from disk",
          not (DATA_TEST / "_pool" / "items" / victim).exists())
    check("deleting twice 404s", c.delete(f"/pool/{victim}").status_code == 404)
    check("both screens still render",
          c.get("/d/a/image?w=80&h=48").status_code == 200
          and c.get("/d/b/image?w=80&h=48").status_code == 200)

    print("\nthumbnails are pool-level, shared between screens")
    tid = c.get("/pool").get_json()[0]["id"]
    r = c.get(f"/pool/{tid}/thumb")
    check("thumb served", r.status_code == 200, str(r.status_code))
    check("thumb is png", r.content_type == "image/png", r.content_type)
    with Image.open(io.BytesIO(r.data)) as t:
        check("thumb is small", t.size == (160, 96), str(t.size))
    check("unknown thumb 404s", c.get("/pool/0000000000000000/thumb").status_code == 404)
    check("malformed thumb id 400s", c.get("/pool/nothex/thumb").status_code == 400)

    print("\nframing belongs to the picture, not the screen")
    pool_ids = [i["id"] for i in c.get("/pool").get_json()]
    c.post(f"/d/f1/items/{pool_ids[0]}/add")
    c.post(f"/d/f1/items/{pool_ids[1]}/add")
    c.post(f"/d/f1/items/{pool_ids[0]}/select")
    c.post("/d/f1/prefs", json={"fit": "cover", "rot": 90})
    first = c.get("/d/f1/items").get_json()
    c.post(f"/d/f1/items/{pool_ids[1]}/select")
    second = c.get("/d/f1/items").get_json()
    check("each picture carries its own framing",
          first["used"][0]["config"].get("rot") == 90
          and second["used"][1]["config"].get("rot") in (None, 90),
          str(second["used"][1]["config"]))
    c.post("/d/f1/prefs", json={"rot": 180})
    after = c.get("/d/f1/items").get_json()["used"]
    check("reframing one leaves the other alone",
          after[0]["config"]["rot"] == 90 and after[1]["config"]["rot"] == 180,
          str([u["config"].get("rot") for u in after]))
    check("the screen's defaults follow the last framing",
          c.get("/d/f1/meta") is not None
          and srv.read_prefs("f1").get("rot") == 180, str(srv.read_prefs("f1")))

    print("\na duplicate is the same picture framed twice")
    current = c.get("/d/f1/items").get_json()["current"]
    r = c.post(f"/d/f1/variants/{current}/duplicate", data={"name": "face"},
               content_type=FORM)
    check("duplicate accepted", r.status_code == 200, str(r.status_code))
    copy_id = r.get_json()["variant"]["id"]
    j = c.get("/d/f1/items").get_json()
    check("it went on the screen", j["current"] == copy_id)
    check("and is a separate row of the same source",
          len([u for u in j["used"] if u["src_id"] == pool_ids[1]]) == 2,
          str([(u["id"], u["src_id"]) for u in j["used"]]))
    check("carrying its name", any(u["variant"] == "face" for u in j["used"]))
    c.post("/d/f1/prefs", json={"zoom": 200})
    rows = {u["id"]: u for u in c.get("/d/f1/items").get_json()["used"]}
    check("framing the copy leaves the original crop alone",
          rows[copy_id]["config"]["zoom"] == 200
          and rows[current]["config"].get("zoom", 100) != 200,
          str((rows[copy_id]["config"], rows[current]["config"])))

    print("\nvariants are keyed by panel shape")
    srv.library.set_shape(DATA_TEST, "f2", "240x240r")
    c.post(f"/d/f2/items/{pool_ids[0]}/select")
    round_row = c.get("/d/f2/items").get_json()["used"][0]
    flat_row = next(u for u in c.get("/d/f1/items").get_json()["used"]
                    if u["src_id"] == pool_ids[0])
    check("a round screen gets its own variant of the same picture",
          round_row["id"] != flat_row["id"] and round_row["src_id"] == flat_row["src_id"],
          f'{round_row["shape"]} vs {flat_row["shape"]}')
    check("each recorded against its shape",
          round_row["shape"] == "240x240r" and flat_row["shape"] == "800x480",
          f'{round_row["shape"]} / {flat_row["shape"]}')

    print("\na screen that learns its real shape re-keys, keeping its framing")
    # This is the migration case: lists converted before the screen was ever
    # seen point at variants made for the default shape, which every other
    # screen would then share.
    c.post(f"/d/f3/items/{pool_ids[1]}/select")
    c.post("/d/f3/prefs", json={"fit": "cover", "zoom": 120})
    before = c.get("/d/f3/items").get_json()["used"][0]
    check("starts at the default shape", before["shape"] == "800x480", before["shape"])
    srv.library.set_shape(DATA_TEST, "f3", "240x240r")
    after = c.get("/d/f3/items").get_json()["used"][0]
    check("re-keyed to the panel it turned out to have",
          after["shape"] == "240x240r" and after["id"] != before["id"],
          f'{before["shape"]} -> {after["shape"]}')
    check("carrying the framing across",
          after["config"].get("zoom") == 120 and after["config"].get("fit") == "cover",
          str(after["config"]))
    check("and the same picture", after["src_id"] == before["src_id"])
    others = {u["id"] for u in c.get("/d/f1/items").get_json()["used"]}
    check("no longer sharing a variant with a differently shaped screen",
          after["id"] not in others)

    # Two screens of the SAME shape do share: one framing per source and
    # shape is the whole point of keying them that way.
    c.post(f"/d/f3/items/{pool_ids[0]}/select")
    mine = c.get("/d/f3/items").get_json()["current"]
    theirs = next(u["id"] for u in c.get("/d/f2/items").get_json()["used"]
                  if u["src_id"] == pool_ids[0])
    check("screens of one shape share a picture's framing", mine == theirs,
          f"{mine} vs {theirs}")

    print("\nreordering only touches what the screen uses")
    for pid in [i["id"] for i in c.get("/pool").get_json()]:
        c.post(f"/d/a/items/{pid}/add")
    used_a = lists(c, "a")[1]
    r = c.post("/d/a/order", json={"ids": list(reversed(used_a))})
    check("reorder accepted", r.status_code == 200, str(r.status_code))
    check("order applied", r.get_json()["order_src"] == list(reversed(used_a)))
    r = c.post("/d/a/order", json={"ids": [used_a[0]]})
    check("partial reorder keeps everything",
          sorted(r.get_json()["order_src"]) == sorted(used_a))
    check("named id moved to the front", r.get_json()["order_src"][0] == used_a[0])
    r = c.post("/d/a/order", json={"ids": ["0000000000000000"]})
    check("ids the screen does not use are ignored", r.status_code == 200)
    check("nothing lost", sorted(r.get_json()["order_src"]) == sorted(used_a))
    check("malformed reorder rejected",
          c.post("/d/a/order", json={"nope": 1}).status_code == 400)

    print("\nclearing a screen leaves the pool alone")
    before = len(c.get("/pool").get_json())
    check("clear", c.delete("/d/a/content").status_code == 204)
    j, used_a, unused_a = lists(c, "a")
    check("a uses nothing", used_a == [], str(used_a))
    check("a has nothing on screen", j["current"] is None)
    check("pool untouched", len(c.get("/pool").get_json()) == before)
    check("everything shows as unused", len(unused_a) == before)
    check("page still renders", c.get("/d/a/").status_code == 200)

    print("\nthe pool directory is not mistaken for a screen")
    check("index renders", c.get("/").status_code == 200)
    check("_pool is not listed as a device",
          "_pool" not in c.get("/").get_data(as_text=True))
    check("and cannot be addressed as one",
          c.get("/d/_pool/items").status_code == 400,
          str(c.get("/d/_pool/items").status_code))

    print("\npage shows both lists")
    c.post(f"/d/a/items/{c.get('/pool').get_json()[0]['id']}/select")
    html = c.get("/d/a/").get_data(as_text=True)
    for probe in ['id="library"', 'id="unused"', "Currently used", "Not used",
                  "/thumb", ">Show<", 'draggable="true"', "on screen"]:
        check(f"page contains {probe}", probe in html)

    print("\nadopting the per-device layout (items/ + index.json)")
    old = DATA_TEST / "old1"
    (old / "items").mkdir(parents=True)
    (old / "items" / "aaaaaaaaaaaa").write_bytes(png((11, 22, 33), (320, 200)))
    (old / "index.json").write_text(json.dumps({
        "current": "aaaaaaaaaaaa",
        "items": [{"id": "aaaaaaaaaaaa", "filename": "old-a.png",
                   "content_type": "image/png", "uploaded_at": 1700000000.0}],
    }), encoding="utf-8")
    j, used_o, _ = lists(c, "old1")
    check("adopted into the pool", len(used_o) == 1, str(used_o))
    check("it is on the screen", j["current_src"] == used_o[0])
    check("filename survived", j["used"][0]["filename"] == "old-a.png")
    check("it renders", c.get("/d/old1/image?w=80&h=48&fmt=png").status_code == 200)
    check("old per-device file cleaned up",
          not (old / "items" / "aaaaaaaaaaaa").exists())
    check("adoption is idempotent", len(lists(c, "old1")[1]) == 1)

    print("\nadopting the original layout (source + meta.json)")
    old2 = DATA_TEST / "old2"
    old2.mkdir(parents=True)
    (old2 / "source").write_bytes(png((44, 55, 66), (500, 500)))
    (old2 / "meta.json").write_text(json.dumps({
        "filename": "legacy.png", "content_type": "image/png",
        "updated_at": 1700000001.0,
    }), encoding="utf-8")
    j, used_l, _ = lists(c, "old2")
    check("adopted", len(used_l) == 1, str(used_l))
    check("filename survived", j["used"][0]["filename"] == "legacy.png")
    check("it renders", c.get("/d/old2/image?w=80&h=48&fmt=png").status_code == 200)
    check("source removed", not (old2 / "source").exists())

    print("\nscenes capture what every screen shows, and how")
    ids = [i["id"] for i in c.get("/pool").get_json()]
    c.post(f"/d/a/items/{ids[0]}/select")
    c.post(f"/d/b/items/{ids[1]}/select")
    c.post("/d/a/prefs", json={"fit": "cover", "rot": 90, "zoom": 150})

    r = c.post("/scenes", json={"name": "Evening"})
    check("scene saved", r.status_code == 201, str(r.status_code))
    scene = r.get_json()
    check("it recorded both screens", {"a", "b"} <= set(scene["entries"]), str(list(scene["entries"])))
    check("and the framing prefs", scene["entries"]["a"]["prefs"]["zoom"] == 150,
          str(scene["entries"]["a"]["prefs"]))
    check("unnamed scene rejected", c.post("/scenes", json={"name": "  "}).status_code == 400)

    # move everything away from the scene, then restore
    c.post(f"/d/a/items/{ids[1]}/select")
    c.post("/d/b/items/" + ids[0] + "/select")
    c.post("/d/a/prefs", json={"reset": 1})
    check("state really changed",
          c.get("/d/a/items").get_json()["current_src"] == ids[1])

    r = c.post(f"/scenes/{scene['id']}/apply")
    check("apply accepted", r.status_code == 200, str(r.status_code))
    check("a restored", c.get("/d/a/items").get_json()["current_src"] == ids[0])
    check("b restored", c.get("/d/b/items").get_json()["current_src"] == ids[1])
    check("prefs restored too",
          c.get("/d/a/items") is not None and
          json.loads((DATA_TEST / "a" / "prefs.json").read_text())["zoom"] == 150)
    check("applying an unknown scene 404s",
          c.post("/scenes/deadbeef/apply").status_code == 404)

    check("saving the same name replaces rather than duplicating",
          len(c.post("/scenes", json={"name": "Evening"}).get_json() and
              c.get("/scenes").get_json()) == 1)

    print("\na scene survives its picture leaving the pool")
    c.post("/scenes", json={"name": "Fragile"})
    fragile = [sc for sc in c.get("/scenes").get_json() if sc["name"] == "Fragile"][0]
    gone = c.get("/d/a/items").get_json()["current"]
    c.delete(f"/pool/{gone}")
    r = c.post(f"/scenes/{fragile['id']}/apply")
    check("apply still succeeds", r.status_code == 200, str(r.status_code))
    check("and skips the missing picture", gone not in str(r.get_json()["changed"]))

    print("\nscenes show on the index and can be deleted")
    html = c.get("/").get_data(as_text=True)
    check("index lists scenes", 'id="scenes"' in html)
    check("with an Apply button", ">Apply<" in html)
    sid = c.get("/scenes").get_json()[0]["id"]
    check("delete", c.delete(f"/scenes/{sid}").status_code == 204)
    check("gone", not any(x["id"] == sid for x in c.get("/scenes").get_json()))
    check("deleting twice 404s", c.delete(f"/scenes/{sid}").status_code == 404)

    print("\nswitching from the main page happens in place")
    ids = [i["id"] for i in c.get("/pool").get_json()]
    c.post(f"/d/a/items/{ids[0]}/select")
    html = c.get("/").get_data(as_text=True)
    check("the preview is refreshable without a reload",
          'class="preview"' in html and "data-base=" in html)
    check("thumbnails carry their filename for the caption", 'data-name="' in html)
    check("the caption element exists", 'class="caption"' in html)
    check("the pick handler is present", "form.pick" in html)
    check("it posts JSON, so the server answers JSON not a 303",
          'Content-Type": "application/json' in html)

    # the JSON path the handler actually uses
    r = c.post(f"/d/a/items/{ids[1]}/select", json={})
    check("JSON select returns JSON, no redirect", r.status_code == 200 and r.is_json,
          str(r.status_code))
    check("and it really switched", c.get("/d/a/items").get_json()["current_src"] == ids[1])

    # no-JS fallback: a form post with return_to comes back to the main page
    r = c.post(f"/d/a/items/{ids[0]}/select", data={"return_to": "/"},
               content_type=FORM, headers=BROWSER)
    check("form post honours return_to", r.headers.get("Location") == "/",
          str(r.headers.get("Location")))
    r = c.post(f"/d/a/items/{ids[0]}/select", data="", content_type=FORM,
               headers=BROWSER)
    check("without return_to it still goes to the device page",
          r.headers.get("Location") == "/d/a/", str(r.headers.get("Location")))

    print("\nreturn_to cannot be turned into an open redirect")
    for evil in ("//evil.example", "https://evil.example", "http://evil.example/x"):
        r = c.post(f"/d/a/items/{ids[0]}/select", data={"return_to": evil},
                   content_type=FORM, headers=BROWSER)
        check(f"rejects {evil}", r.headers.get("Location") == "/d/a/",
              str(r.headers.get("Location")))
    r = c.post(f"/d/a/items/{ids[0]}/select", data={"return_to": "/d/b/"},
               content_type=FORM, headers=BROWSER)
    check("but a local path is allowed", r.headers.get("Location") == "/d/b/")

    print("\nanimated uploads are identified and shown animated")

    def anim(fmt, n, name, **kw):
        fr = [Image.new("RGB", (120, 120), (i * 30 % 256, 90, 200 - i * 20))
              for i in range(n)]
        b = io.BytesIO()
        if n > 1:
            fr[0].save(b, fmt, save_all=True, append_images=fr[1:], **kw)
        else:
            fr[0].save(b, fmt)
        return c.post("/d/anim/content",
                      data={"file": (io.BytesIO(b.getvalue()), name)},
                      content_type="multipart/form-data")

    # APNG is the awkward one: Pillow reports it as format "PNG", so the
    # format name alone cannot distinguish it from a still.
    r = anim("PNG", 6, "clip.png", duration=120, loop=0)
    check("APNG accepted", r.status_code == 201, str(r.status_code))
    apng = r.get_json()
    check("APNG identified as animated", apng["animated"] is True, str(apng.get("animated")))
    check("with its frame count", apng["frames"] == 6, str(apng.get("frames")))
    check("and its duration", apng["duration_ms"] == 720, str(apng.get("duration_ms")))
    check("APNG still reports format PNG", apng["source_format"] == "PNG")

    g = anim("GIF", 8, "clip.gif", duration=80, loop=0).get_json()
    check("GIF identified", g["animated"] is True and g["frames"] == 8, str(g))
    w = anim("WEBP", 5, "clip.webp", duration=100, loop=0).get_json()
    check("animated WebP identified", w["animated"] is True and w["frames"] == 5, str(w))
    st = anim("PNG", 1, "still.png").get_json()
    check("a still PNG is not animated", st["animated"] is False and st["frames"] == 1, str(st))
    j = anim("JPEG", 1, "still.jpg").get_json()
    check("a JPEG is not animated", j["animated"] is False)

    print("\nthe original is served so a browser can play it")
    for it, ctype in ((apng, "image/png"), (g, "image/gif"), (w, "image/webp")):
        r = c.get(f"/pool/{it['id']}/raw")
        check(f"raw {it['filename']} -> {ctype}",
              r.status_code == 200 and r.content_type == ctype, r.content_type)
    check("content type comes from what Pillow decoded, not the upload header",
          c.get(f"/pool/{apng['id']}/raw").content_type == "image/png")
    check("unknown item 404s", c.get("/pool/0000000000000000/raw").status_code == 404)

    print("\nlists animate the clip and badge it")
    html = c.get("/d/anim/").get_data(as_text=True)
    check("animated items are thumbnailed from the original",
          f'src="/pool/{apng["id"]}/raw"' in html)
    check("stills still use the rendered thumbnail",
          f'src="/pool/{st["id"]}/thumb"' in html)
    check("badge shows the frame count", "6 frames" in html and "8 frames" in html)
    check("badge shows the duration", "0.7s" in html)
    check("the still gets no badge", html.count('class="anim"') == 3, str(html.count('class="anim"')))

    # the index badge follows whatever is current on that screen
    c.post(f"/d/anim/items/{apng['id']}/select")
    idx = c.get("/").get_data(as_text=True)
    check("index badges the current clip", 'class="anim"' in idx and "6 frames" in idx)
    c.post(f"/d/anim/items/{st['id']}/select")
    idx = c.get("/").get_data(as_text=True)
    check("and drops the badge for a still", "6 frames" not in idx)

    print("\nan animated upload is playable as video")
    c.post(f"/d/anim/items/{g['id']}/select")
    info = c.get("/d/anim/video").get_json()
    check("video source is the upload", info["source"] == "uploaded", str(info))
    check("with the GIF frame count", info["frames"] == 8, str(info))
    c.post(f"/d/anim/items/{apng['id']}/select")
    info = c.get("/d/anim/video").get_json()
    check("an APNG is playable too", info["source"] == "uploaded" and info["frames"] == 6, str(info))
    f0 = c.get("/d/anim/frame?n=0&w=240&h=240&fmt=png").data
    f3 = c.get("/d/anim/frame?n=3&w=240&h=240&fmt=png").data
    check("APNG frames differ from one another", f0 != f3)
    c.delete("/d/anim/content")

    print("\nJSON callers still get JSON")
    pid = c.get("/pool").get_json()[0]["id"]
    check("select via JSON", c.post(f"/d/a/items/{pid}/select", json={}).is_json)
    check("add via JSON", c.post(f"/d/a/items/{pid}/add", json={}).is_json)
    check("order via JSON", c.post("/d/a/order", json={"ids": [pid]}).is_json)

    shutil.rmtree(DATA_TEST, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
