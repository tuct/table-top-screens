#!/usr/bin/env python
"""Turn the API-format graphs gen.py builds into GUI-loadable workflows.

ComfyUI speaks two dialects: the terse API format the /prompt endpoint takes,
and the verbose editor format with explicit nodes, links and positions. The
generator emits the first; the canvas only opens the second. Rather than keep
a hand-drawn copy in sync, this converts one into the other, reading each
node's real input order from /object_info so widget values land in the right
slots.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import gen

HOST = "http://127.0.0.1:8188"
OI = json.load(urllib.request.urlopen(f"{HOST}/object_info"))


def is_link(v) -> bool:
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int)


def slots(class_type: str):
    """Every declared input, required first, in the order the node expects."""
    d = OI[class_type]["input"]
    out = []
    for kind in ("required", "optional"):
        for name, spec in (d.get(kind) or {}).items():
            out.append((name, spec))
    return out


def convert(api: dict, title: str) -> dict:
    ids = {name: i + 1 for i, name in enumerate(api)}

    # Column by dependency depth, so the graph reads left to right instead of
    # piling every node on the origin.
    depth: dict[str, int] = {}

    def d(n, seen=()):
        if n in depth:
            return depth[n]
        if n in seen:
            return 0
        vals = [d(v[0], seen + (n,)) + 1
                for v in api[n]["inputs"].values() if is_link(v)]
        depth[n] = max(vals, default=0)
        return depth[n]

    for n in api:
        d(n)

    column: dict[int, int] = {}
    nodes, links, link_id = [], [], 0

    for name, spec in api.items():
        ct = spec["class_type"]
        info = OI[ct]
        col = depth[name]
        row = column.get(col, 0)
        column[col] = row + 1

        node_inputs, widgets = [], []
        for slot_name, slot_spec in slots(ct):
            if slot_name not in spec["inputs"]:
                # An unsupplied optional input still needs its socket drawn.
                t = slot_spec[0]
                if not isinstance(t, list) and t not in ("INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"):
                    node_inputs.append({"name": slot_name, "type": t, "link": None})
                continue
            val = spec["inputs"][slot_name]
            if is_link(val):
                t = slot_spec[0]
                node_inputs.append({"name": slot_name,
                                    "type": t if isinstance(t, str) else "COMBO",
                                    "link": None})  # filled in below
            else:
                widgets.append(val)
                opts = slot_spec[1] if len(slot_spec) > 1 and isinstance(slot_spec[1], dict) else {}
                # Frontend-only companion widgets, which occupy a slot in the
                # saved list even though the API never sends them.
                if opts.get("control_after_generate"):
                    widgets.append("randomize")
                if opts.get("image_upload"):
                    widgets.append("image")

        outs = []
        for i, (ot, on) in enumerate(zip(info["output"],
                                         info.get("output_name") or info["output"])):
            outs.append({"name": on, "type": ot, "links": [], "slot_index": i})

        nodes.append({
            "id": ids[name], "type": ct,
            "pos": [col * 360, row * 230],
            "size": [300, 120], "flags": {}, "order": col, "mode": 0,
            "inputs": node_inputs, "outputs": outs,
            "properties": {"Node name for S&R": ct, "cnr_id": "comfy-core"},
            "widgets_values": widgets,
            "title": f"{ct}",
        })

    by_id = {n["id"]: n for n in nodes}

    # Second pass: now that every node exists, wire the links both ways.
    for name, spec in api.items():
        tgt = by_id[ids[name]]
        for slot_name, val in spec["inputs"].items():
            if not is_link(val):
                continue
            src_node, src_slot = val
            link_id += 1
            src = by_id[ids[src_node]]
            ltype = src["outputs"][src_slot]["type"]
            src["outputs"][src_slot]["links"].append(link_id)
            for inp in tgt["inputs"]:
                if inp["name"] == slot_name:
                    inp["link"] = link_id
                    break
            links.append([link_id, ids[src_node], src_slot, ids[name],
                          [i["name"] for i in tgt["inputs"]].index(slot_name), ltype])

    return {
        "id": title, "revision": 0,
        "last_node_id": len(nodes), "last_link_id": link_id,
        "nodes": nodes, "links": links, "groups": [],
        "config": {}, "extra": {}, "version": 0.4,
    }


def main() -> None:
    dest = Path.home() / "ComfyUI" / "user" / "default" / "workflows"
    dest.mkdir(parents=True, exist_ok=True)

    recipes = {
        "tabletop - text to video (LCM fast)":
            ["a crackling campfire in a dark forest at night, embers drifting up, "
             "fantasy illustration", "--screen", "p4", "--lcm", "--frames", "16"],
        "tabletop - text to video (quality)":
            ["a torchlit stone dungeon corridor, dust motes drifting, "
             "fantasy illustration", "--screen", "p4", "--frames", "16"],
        "tabletop - animate a still (SparseCtrl)":
            ["gentle motion, drifting light, subtle breathing",
             "--image", "pool_92760223.png", "--screen", "square",
             "--lcm", "--frames", "16"],
    }

    for title, argv in recipes.items():
        args = gen.resolve(gen.build_parser().parse_args(argv))
        api, _seed, _size = gen.build(args)
        ui = convert(api, title)
        out = dest / f"{title}.json"
        out.write_text(json.dumps(ui, indent=1))
        print(f"{len(ui['nodes']):2} nodes, {len(ui['links']):2} links  ->  {out.name}")


if __name__ == "__main__":
    main()
