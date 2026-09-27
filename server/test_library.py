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
import re
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


def flat(html: str) -> str:
    """Collapse whitespace, so a probe does not depend on where the template
    happens to wrap an attribute."""
    return re.sub(r"\s+", " ", html)


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

    print("\nthe given name leads, the filename follows the selection")
    cur_id = c.get("/d/f1/items").get_json()["current"]
    c.post("/d/f1/prefs", json={"name": "face", "desc": "tight crop for the hallway"})
    rows = {u["id"]: u for u in c.get("/d/f1/items").get_json()["used"]}
    check("the description is stored with the variant",
          rows[cur_id]["desc"] == "tight crop for the hallway", str(rows[cur_id].get("desc")))
    page = flat(c.get("/d/f1/").get_data(as_text=True))
    check("the row is titled by its name", '<span class="nm" title=' in page and ">face<" in page)
    check("the description shows under it", "tight crop for the hallway" in page)
    check("and the form offers it back", 'name="desc" maxlength="200"' in page)
    others = [u for u in c.get("/d/f1/items").get_json()["used"] if u["id"] != cur_id]
    if others:
        check("an unselected row does not spend a line on the filename",
              page.count(others[0]["filename"]) <= 2,
              f'{others[0]["filename"]} x{page.count(others[0]["filename"])}')
    c.post("/d/f1/prefs", json={"reset": 1})
    rows = {u["id"]: u for u in c.get("/d/f1/items").get_json()["used"]}
    check("reset clears the description too", rows[cur_id]["desc"] == "")

    print("\nselecting a picture loads its framing")
    c.post(f"/d/f1/items/{pool_ids[0]}/select")
    c.post("/d/f1/prefs", json={"rot": 90, "zoom": 140})
    c.post(f"/d/f1/items/{pool_ids[1]}/select")
    c.post("/d/f1/prefs", json={"rot": 180, "zoom": 100})
    c.post(f"/d/f1/items/{pool_ids[0]}/select")
    page = flat(c.get("/d/f1/").get_data(as_text=True))
    check("the form comes up with this picture's rotation",
          'name="rot" value="90" checked' in page)
    check("and its zoom", "Zoom 140%" in page or "value=\"140\"" in page or "140" in page)
    first_png = c.get("/d/f1/image?w=60&h=40&fmt=png").data
    c.post(f"/d/f1/items/{pool_ids[1]}/select")
    page = flat(c.get("/d/f1/").get_data(as_text=True))
    check("switching picture switches the form to its own framing",
          'name="rot" value="180" checked' in page and 'name="rot" value="90" checked' not in page)
    check("and the render follows the variant, not the screen",
          c.get("/d/f1/image?w=60&h=40&fmt=png").data != first_png)

    print("\nnaming a framing")
    cur = c.get("/d/f1/items").get_json()["current"]
    c.post("/d/f1/prefs", json={"name": "face", "zoom": 130})
    rows = {u["id"]: u for u in c.get("/d/f1/items").get_json()["used"]}
    check("apply saves the name too", rows[cur]["variant"] == "face", str(rows[cur]["variant"]))
    check("alongside the framing", rows[cur]["config"].get("zoom") == 130)
    page = flat(c.get("/d/f1/").get_data(as_text=True))
    check("and the form shows it back",
          'name="name" maxlength="60" placeholder="e.g. face" value="face"' in page)
    c.post("/d/f1/prefs", json={"reset": 1})
    rows = {u["id"]: u for u in c.get("/d/f1/items").get_json()["used"]}
    check("reset clears the name as well", rows[cur]["variant"] == "")

    print("\nthe page says what the panel is")
    srv.library.set_shape(DATA_TEST, "geo1", "480x800")
    srv.library.set_shape(DATA_TEST, "geo2", "240x240r")
    c.post(f"/d/geo1/items/{pool_ids[0]}/select")
    c.post(f"/d/geo2/items/{pool_ids[0]}/select")
    p1 = c.get("/d/geo1/").get_data(as_text=True)
    p2 = c.get("/d/geo2/").get_data(as_text=True)
    check("resolution and orientation, for a screen that is offline",
          "480×800 portrait" in p1, "480×800 portrait")
    check("a round panel is called round, not oriented", "240×240 round" in p2)
    check("and its pictures are drawn as circles",
          "round-screen" in p2 and "bezel round" in p2)
    check("a rectangular one is not", "round-screen" not in p1)
    idx = c.get("/").get_data(as_text=True)
    check("the overview says it too",
          "480×800 portrait" in idx and "240×240 round" in idx)
    check("and rounds that card's pictures", "bezel mini round" in idx)
    check("a screen page offers the way back", 'href="/"' in flat(p1)
          and "All screens" in p1)

    print("\nthe shelf says which screens are answering, and can be ordered")
    # Injected rather than discovered: this file never talks to the network,
    # but the overview's two orders only differ once something is live.
    live = srv.discovery.Screen(name="geo2", host="127.0.0.1", port=9,
                                 width=240, height=240)
    srv.registry._screens["geo2._x"] = live
    try:
        idx = c.get("/").get_data(as_text=True)
        check("an offline screen says so on the panel itself",
              '<span class="offtag">offline</span>' in flat(idx))
        order = re.findall(r'<a class="nm" href="/d/([^/]+)/"', idx)
        check("a live one does not",
              flat(idx).count('class="offtag"') == len(order) - 1)
        check("online first by default", order and order[0] == "geo2", str(order))
        by_name = c.get("/?sort=name").get_data(as_text=True)
        names = re.findall(r'<a class="nm" href="/d/([^/]+)/"', by_name)
        check("by name when asked", names == sorted(names), str(names))
        check("and the choice is marked in the control",
              '<a href="/?sort=name" class="now"' in flat(by_name))
        check("the choice is remembered for the next visit",
              re.findall(r'<a class="nm" href="/d/([^/]+)/"',
                         c.get("/").get_data(as_text=True)) == names)
        check("nonsense falls back to the default order",
              re.findall(r'<a class="nm" href="/d/([^/]+)/"',
                         c.get("/?sort=sideways").get_data(as_text=True))[0] == "geo2")
        print("\na screen off the network can be forgotten")
        c.post(f"/d/gone1/items/{pool_ids[0]}/select")
        c.post("/scenes", data={"name": "before"}, content_type=FORM)
        before = srv.library.scenes(DATA_TEST)[-1]["entries"]
        check("it is in the scene that was saved", "gone1" in before)
        check("a live screen is not offered for removal",
              '/d/geo2/remove' not in c.get("/").get_data(as_text=True))
        check("and refuses to be removed if asked anyway",
              c.post("/d/geo2/remove").status_code == 409)
        idx = flat(c.get("/").get_data(as_text=True))
        check("an offline one is offered, and asks first",
              '/d/gone1/remove' in idx and "Yes, remove" in idx)
        pool_before = len(c.get("/pool").get_json())
        check("removing it answers by going back to the shelf",
              c.post("/d/gone1/remove", data={"return_to": "/"},
                     content_type=FORM, headers=BROWSER).status_code == 303)
        check("the screen is gone from the shelf",
              "gone1" not in c.get("/").get_data(as_text=True))
        check("and from what we hold state for",
              "gone1" not in srv.library.devices(DATA_TEST))
        check("its folder is gone with it", not (DATA_TEST / "gone1").exists())
        check("and it is not remembered back into a card",
              "gone1" not in srv.library.seen(DATA_TEST)
              and "gone1" not in c.get("/?all=1").get_data(as_text=True))
        check("the pictures stay in the library",
              len(c.get("/pool").get_json()) == pool_before)
        check("the scene forgets that screen and keeps the rest",
              "gone1" not in srv.library.scenes(DATA_TEST)[-1]["entries"]
              and srv.library.scenes(DATA_TEST)[-1]["entries"])
        check("removing it twice is a 404, not a second removal",
              c.post("/d/gone1/remove").status_code == 404)
        # Leave the scene shelf as it was: the tests below count what is on it.
        c.post(f"/scenes/{srv.library.scenes(DATA_TEST)[-1]['id']}/delete")
    finally:
        srv.registry._screens.pop("geo2._x", None)
        c.set_cookie("sort", "", expires=0)

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
    for probe in ['id="library"', 'id="unused"', "Playlist", "Library",
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

    print("\na scene holds the screens you pick, and only live ones can join")
    c.post(f"/d/a/items/{ids[0]}/select")
    c.post(f"/d/b/items/{ids[1]}/select")
    # "a" is on the network for this block; "b" is not.
    srv.registry._screens["a._x"] = srv.discovery.Screen(name="a", host="127.0.0.1", port=9)
    try:
        r = c.post("/scenes", json={"name": "Just A", "screens": ["a"]})
        check("only the screens ticked are taken", set(r.get_json()["entries"]) == {"a"},
              str(list(r.get_json()["entries"])))
        r = c.post("/scenes", json={"name": "Try B", "screens": ["a", "b"]})
        check("an offline screen cannot be added",
              set(r.get_json()["entries"]) == {"a"}, str(list(r.get_json()["entries"])))

        both = c.post("/scenes", json={"name": "Both"}).get_json()
        check("but a caller that picks nothing still gets them all",
              {"a", "b"} <= set(both["entries"]))
        saved_b = both["entries"]["b"]
        c.post(f"/d/b/items/{ids[0]}/select")   # b moves on while it is asleep
        r = c.post(f"/scenes/{both['id']}/update", json={"screens": ["a", "b"]})
        check("an offline member is kept exactly as it was saved",
              r.get_json()["entries"]["b"] == saved_b, str(r.get_json()["entries"].get("b")))
        r = c.post(f"/scenes/{both['id']}/update", json={"screens": ["a"]})
        check("and can still be dropped on purpose",
              set(r.get_json()["entries"]) == {"a"})

        print("\na scene can be edited: its name, its screens, its picture")
        c.post(f"/d/a/items/{ids[1]}/select")
        r = c.post(f"/scenes/{both['id']}/update",
                   json={"screens": ["a"], "name": "Renamed"})
        check("saving renames it", r.get_json()["name"] == "Renamed")
        check("and takes what is on the screens now, in the same act",
              r.get_json()["entries"]["a"]["item"]
              == c.get("/d/a/items").get_json()["current"])
        check("editing an unknown scene 404s",
              c.post("/scenes/deadbeef/update", json={}).status_code == 404)

        r = c.post(f"/scenes/{both['id']}/duplicate", json={})
        copy = r.get_json()
        check("duplicate takes a free name", copy["name"] == "Renamed copy", copy["name"])
        check("with the same screens and a new id",
              copy["entries"] == r.get_json()["entries"] and copy["id"] != both["id"])
        check("duplicating again does not collide",
              c.post(f"/scenes/{both['id']}/duplicate", json={}).get_json()["name"]
              == "Renamed copy 2")

        print("\nthe page says which scene is on the screens")
        c.post(f"/scenes/{both['id']}/apply")
        check("a scene whose screens still match is the one on screen",
              srv.library.scene_on_screen(DATA_TEST, both["id"]))
        html = flat(c.get("/").get_data(as_text=True))
        check("the matching scene is marked", "on screen</span>" in html)
        check("and named above the list", '<b class="onnow">' in html)
        check("the screens are offered as checkboxes",
              'name="screens" value="a"' in html and 'name="pick" value="1"' in html)
        check("an offline one is offered locked",
              'name="screens" value="b" disabled' in html)
        check("with Edit and Duplicate on each scene",
              ">Edit<" in html and ">Duplicate<" in html)
        # Out of the way first: those two also record a=ids[0], and a scene
        # that matches outranks one that has been changed since.
        for spare in ("Just A", "Try B"):
            sid = next(s["id"] for s in c.get("/scenes").get_json()
                       if s["name"] == spare)
            c.delete(f"/scenes/{sid}")
        c.post(f"/d/a/items/{ids[0]}/select")
        check("and it stops being on screen once a screen moves on",
              not srv.library.scene_on_screen(DATA_TEST, both["id"]))
        html = flat(c.get("/").get_data(as_text=True))
        check("the scene that was put up is starred instead",
              '<span class="star" title=' in html)
        check("and the line above says so", "was put up, and a screen has been"
              " changed since" in html)
        state = c.get("/scenes/state").get_json()
        check("the mark is available without reloading the page",
              next(x["dirty"] for x in state["scenes"] if x["id"] == both["id"])
              and state["note"]["star"])
        c.post(f"/scenes/{both['id']}/update", json={"screens": ["a"]})
        html = flat(c.get("/").get_data(as_text=True))
        check("saving the scene again clears the star",
              '<span class="star" title=' not in html
              and '<span class="star" hidden' in html)
        check("and the marks are in the markup either way, only hidden",
              not c.get("/scenes/state").get_json()["note"]["star"])

        print("\nreframing counts as a change too, not only switching picture")
        c.post(f"/scenes/{both['id']}/apply")
        check("the scene is back on screen after applying it",
              srv.library.scene_on_screen(DATA_TEST, both["id"]))
        c.post("/d/a/prefs", json={"zoom": 175})
        check("a zoom on the shown picture takes it off screen",
              not srv.library.scene_on_screen(DATA_TEST, both["id"]))
        check("and the page stars it",
              '<span class="star" title=' in flat(c.get("/").get_data(as_text=True)))
        recorded = srv.library.scene_get(DATA_TEST, both["id"])["entries"]["a"]
        c.post("/d/a/prefs", json={"zoom": recorded["config"]["zoom"]})
        check("putting the framing back puts the scene back on screen",
              srv.library.scene_on_screen(DATA_TEST, both["id"]))
        c.post("/d/a/prefs", json={"zoom": 100})
        check("a zoom of 100 is no zoom at all, not a change",
              srv.library._framing_key({"zoom": 100}) == {})

        print("\nthe save form saves back into the scene, or beside it")
        idx = flat(c.get("/?all=1").get_data(as_text=True))
        check("it saves into the scene you are in",
              'action="/scenes/' + both["id"] + '/update"' in idx)
        check("with that scene's name already in it", 'value="Renamed"' in idx)
        check("and the screens still tickable", 'name="screens" value="a"' in idx)
        check("it offers to keep the old one instead", "Save as duplicate" in idx)
        # Both buttons belong to being in a scene, not to having changed it.
        c.post(f"/scenes/{both['id']}/update", json={"screens": ["a"]})
        settled = flat(c.get("/?all=1").get_data(as_text=True))
        check("both are still there once the scene is back on screen",
              "Save as duplicate" in settled
              and 'action="/scenes/' + both["id"] + '/update"' in settled)
        c.post("/d/a/prefs", json={"zoom": 100})

        was = srv.library.scene_get(DATA_TEST, both["id"])["entries"]["a"]
        r = c.post(f"/scenes/{both['id']}/saveas",
                   json={"name": "Renamed", "screens": ["a"]})
        copy = r.get_json()
        check("saving as a duplicate never replaces by name",
              copy["name"].startswith("Renamed copy")
              and any(x["name"] == "Renamed" for x in c.get("/scenes").get_json()),
              copy["name"])
        check("the scene it came from is left as it was",
              srv.library.scene_get(DATA_TEST, both["id"])["entries"]["a"] == was)
        check("the copy holds what the screens show now",
              srv.library.scene_on_screen(DATA_TEST, copy["id"]))
        check("and is the scene you are now in",
              srv.scene_marks()["active"]["id"] == copy["id"])

        print("\nthe ticks load from the scene, and a live screen can join it")
        srv.registry._screens["b._x"] = srv.discovery.Screen(name="b", host="127.0.0.1",
                                                             port=9)
        try:
            chips = flat(c.get("/?all=1").get_data(as_text=True))
            check("a screen in the scene comes up ticked",
                  'name="screens" value="a" checked' in chips)
            check("one that is not, but is online, can be ticked",
                  'name="screens" value="b" >' in chips)
            r = c.post(f"/scenes/{both['id']}/update", json={"screens": ["a", "b"]})
            check("and ticking it puts it in the scene",
                  set(r.get_json()["entries"]) == {"a", "b"})
        finally:
            srv.registry._screens.pop("b._x", None)
        c.post(f"/scenes/{both['id']}/update", json={"screens": ["a"]})

        print("\nediting a scene changes what it is called, not what it holds")
        held = srv.library.scene_get(DATA_TEST, copy["id"])["entries"]
        r = c.post(f"/scenes/{copy['id']}/labels",
                   json={"name": "Guests", "desc": "for when people are over"})
        check("it renames", r.get_json()["name"] == "Guests")
        check("and describes", r.get_json()["desc"] == "for when people are over")
        check("without re-reading a single screen",
              srv.library.scene_get(DATA_TEST, copy["id"])["entries"] == held)
        check("a name another scene already has is refused",
              c.post(f"/scenes/{copy['id']}/labels", json={"name": "Renamed"})
              .status_code == 400)
        shown = flat(c.get("/?all=1").get_data(as_text=True))
        # Named by the file it shows, NOT the "no longer in the library"
        # fallback -- which shares the "<device> · " prefix and would let a
        # broken lookup pass unnoticed.
        on_a = srv.library.pool_get(
            DATA_TEST, srv.library.variant(
                DATA_TEST, c.get("/d/a/items").get_json()["current"])["src"])
        check("each scene shows what it puts on each screen",
              f'<span class="shot" title="a · {on_a["filename"]}"' in shown, shown[:0])
        check("and nothing is drawn as missing that is not",
              "picture no longer in the library" not in shown)
        check("the name comes first, then the pictures",
              shown.index('<span class="who">')
              < shown.index('<span class="sthumbs">')
              < shown.index('<span class="rightside">'))
        check("dates and counts are gone from the row",
              " screens · saved " not in shown and "· applied " not in shown)
        # Older scenes here hold "b", which is not on the network.
        check("a screen that is not answering is greyed in the preview",
              'class="shot off"' in shown)
        check("and says so when you point at it", "· offline\"" in shown)
        # geo2 is the round panel from the geometry block; live for a moment,
        # so a scene can be saved with it in.
        srv.registry._screens["geo2._x"] = srv.discovery.Screen(
            name="geo2", host="127.0.0.1", port=9, width=240, height=240,
            round=True)
        try:
            round_scene = c.post("/scenes", json={"name": "Round one",
                                                  "screens": ["geo2"]}).get_json()
            check("a round screen's picture is round there too",
                  '<span class="shot rnd"' in flat(
                      c.get("/?all=1").get_data(as_text=True)))
        finally:
            srv.registry._screens.pop("geo2._x", None)
            c.delete(f"/scenes/{round_scene['id']}")
            c.post(f"/scenes/{both['id']}/apply")
        check("the description shows in the list",
              "for when people are over" in flat(c.get("/?all=1").get_data(as_text=True)))
        c.delete(f"/scenes/{copy['id']}")

        print("\nworking in a scene narrows the shelf to its screens")
        c.post(f"/scenes/{both['id']}/update", json={"screens": ["a"]})
        c.post(f"/scenes/{both['id']}/apply")
        idx = flat(c.get("/").get_data(as_text=True))
        check("the scene's screen is there", '<a class="nm" href="/d/a/"' in idx)
        check("one that is not in it is hidden, but still in the page",
              'data-name="b" hidden' in idx
              and '<a class="nm" href="/d/b/"' in idx)
        check("and the line says how many are showing, and how to see the rest",
              'Showing <span class="fshown">1</span> of' in idx
              and '<a href="/?all=1">show all</a>' in idx)
        check("the scene's own picker still lists them all, or none could join",
              'name="screens" value="b"' in idx)
        every = flat(c.get("/?all=1").get_data(as_text=True))
        check("show all brings them back", 'data-name="b" hidden' not in every)
        check("a hidden card is still whole, so ticking can reveal it",
              'data-name="b"' in idx and "/d/b/remove" in idx)
        check("and the page carries the script that reveals it",
              'data-narrowed=1' in idx)
        check("with a way back to just the scene", "show only Renamed" in every)

        print("\nan offline screen offers nothing but Remove")
        asleep = every.split('<a class="nm" href="/d/b/"', 1)[1].split("</article>", 1)[0]
        awake = every.split('<a class="nm" href="/d/a/"', 1)[1].split("</article>", 1)[0]
        check("its card is drawn as asleep", "isoff" in every)
        check("its pictures cannot be switched", "disabled>" in asleep)
        check("but it can still be forgotten", "/d/b/remove" in asleep)
        check("a live screen keeps its buttons", "disabled>" not in awake)

        print("\nand you can leave a scene without losing it")
        check("the way out is offered", "Leave scene" in idx)
        check("leaving is accepted",
              c.post("/scenes/release", data={"return_to": "/"},
                     content_type=FORM, headers=BROWSER).status_code == 303)
        after = flat(c.get("/").get_data(as_text=True))
        check("the shelf shows every screen again",
              '<a class="nm" href="/d/b/"' in after)
        check("nothing claims to be up any more", "Leave scene" not in after)
        # Scoped to the scene list: `pill on` is also how a live screen is
        # marked, and those are still online.
        listed = after.split('<ul id="scenes">', 1)[1]
        check("not even a scene whose screens still match",
              '<span class="pill on"><span' not in listed
              and srv.library.scene_on_screen(DATA_TEST, both["id"]))
        check("the note goes back to explaining what a scene is",
              "A scene saves the picture and settings" in after)
        check("and the scene itself is untouched",
              any(x["id"] == both["id"] for x in c.get("/scenes").get_json()))
        check("nor is anything starred",
              not c.get("/scenes/state").get_json()["note"]["star"])
    finally:
        srv.registry._screens.pop("a._x", None)
        for sid in [s["id"] for s in c.get("/scenes").get_json()
                    if s["name"] in {"Just A", "Try B", "Renamed",
                                     "Renamed copy", "Renamed copy 2"}]:
            c.delete(f"/scenes/{sid}")

    print("\nrefresh asks the screens, and believes the answer")
    import http.server, threading as _t
    class _Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a):
            pass
    srv_http = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
    _t.Thread(target=srv_http.serve_forever, daemon=True).start()
    port = srv_http.server_address[1]
    try:
        # One screen that answers, one that does not.
        srv.registry._screens["up._x"] = srv.discovery.Screen(
            name="up", host="127.0.0.1", port=port)
        srv.registry._screens["down._x"] = srv.discovery.Screen(
            name="down", host="127.0.0.1", port=9)
        c.get("/")   # noting them as seen is what the page does
        check("both are remembered as met",
              {"up", "down"} <= set(srv.library.seen(DATA_TEST)))
        r = c.post("/screens/refresh", json={})
        out = r.get_json()
        check("the one that answers stays", "up" not in out["gone"], str(out))
        check("the one that does not is marked offline", out["gone"] == ["down"])
        check("and it still has a card, from what we remember of it",
              '<a class="nm" href="/d/down/"' in flat(
                  c.get("/?all=1").get_data(as_text=True)))
        check("the button can show it is working",
              'class="inline refresh"' in flat(c.get("/").get_data(as_text=True)))
        seat = c.post("/screens/refresh", data={"return_to": "/"},
                      content_type=FORM, headers=BROWSER)
        check("a browser is sent back with what was found",
              seat.status_code == 303 and "checked=" in seat.headers["Location"],
              seat.headers.get("Location", ""))
        check("and the page says it",
              "Knocked on" in c.get(seat.headers["Location"]).get_data(as_text=True))
        # A screen we remember and cannot see, that answers again, comes back.
        r = c.post("/screens/refresh", json={})
        check("a remembered screen that answers again comes back",
              "down" not in srv.registry.known_names())
        srv.library.note_seen(DATA_TEST, "down",
                              {"host": "127.0.0.1", "port": port})
        check("revived once it answers",
              "down" in c.post("/screens/refresh", json={}).get_json()["back"])
        check("and is online again", "down" in srv.registry.known_names())
    finally:
        srv_http.shutdown()
        for key in ("up._x", "down._x", "down.revived"):
            srv.registry._screens.pop(key, None)
        c.post("/d/up/remove")
        c.post("/d/down/remove")

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
          'data-base="/d/a/image' in html)
    check("thumbnails carry their name for the caption", 'data-name="' in html)
    check("the caption element exists", 'class="nm"' in html)
    script = c.get("/static/app.js").get_data(as_text=True)
    check("the pick handler is present", "form.pick" in script)
    check("it posts JSON, so the server answers JSON not a 303",
          'Content-Type": "application/json' in script)

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
    check("badge shows the frame count", "6 · 0.7s" in html and "8 · " in html)
    check("badge shows the duration", "0.7s" in html)
    # Three chips, not four: the two clips appear in the playlist, one of them
    # again in the index's card, and the still is never badged.
    check("the still gets no badge", html.count('class="anim"') == 3, str(html.count('class="anim"')))

    # the index badge follows whatever is current on that screen. `all=1`
    # because a scene has been applied by now, and the shelf narrows to it.
    c.post(f"/d/anim/items/{apng['id']}/select")
    idx = c.get("/?all=1").get_data(as_text=True)
    check("index badges the current clip", 'class="anim"' in idx and "6 · 0.7s" in idx)
    c.post(f"/d/anim/items/{st['id']}/select")
    idx = c.get("/?all=1").get_data(as_text=True)
    check("and drops the badge for a still", "6 frames" not in idx)

    print("\na screen that cannot animate says so where the promise is made")
    stills_only = srv.discovery.Screen(name="anim", host="127.0.0.1", port=9,
                                       anim=())
    srv.registry._screens["anim._x"] = stills_only
    try:
        c.post(f"/d/anim/items/{g['id']}/select")
        page = flat(c.get("/d/anim/").get_data(as_text=True))
        check("the row says it plainly", ">1st frame only</span>" in page)
        check("and the motion chip stops promising motion",
              'class="anim muted"' in page)
        check("said again under the picture itself",
              "shows stills only" in page and "the rest of it is not played" in page)
        shelf = flat(c.get("/?all=1").get_data(as_text=True))
        check("the shelf says it too", ">1st frame only</span>" in shelf)
        c.post(f"/d/anim/items/{st['id']}/select")
        check("a still gets none of that",
              "the rest of it is not played" not in flat(
                  c.get("/d/anim/").get_data(as_text=True)))
    finally:
        srv.registry._screens.pop("anim._x", None)

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
