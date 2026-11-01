"""
fetch_wiki_ids.py -- build a block-name -> wiki-ID table from the official wiki.

The official Mindustry wiki (mindustrygame.github.io/wiki) generates content
pages whose URL slug starts with the content id, e.g.

    content/blocks/crafting/181-graphite-press/
    content/blocks/environment/165-pebbles/

The numbers follow the game's Content.id assignment order, so this gives us a
block-name -> id table without building the game.

Note: ids are version- and mod-dependent. The wiki tracks the latest release;
a modded install shifts ids. Treat this as a *reference*, not ground truth.
"""
from __future__ import annotations

import collections
import json
import os
import re
import sys
import urllib.request

INDEX = "https://mindustrygame.github.io/wiki/search/search_index.json"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki_block_ids.json")

PAGE = re.compile(r"^content/blocks/([^/]+)/(\d+)-([a-z0-9\-]+)/$")


def main() -> None:
    req = urllib.request.Request(INDEX, headers={"User-Agent": "dsh"})
    with urllib.request.urlopen(req, timeout=60) as fh:
        data = json.load(fh)

    blocks: dict[int, str] = {}
    categories: dict[int, str] = {}
    dups: dict[int, list[str]] = collections.defaultdict(list)

    for doc in data.get("docs", []):
        loc = doc.get("location", "")
        m = PAGE.match(loc)
        if not m:
            continue
        cat, num, name = m.group(1), int(m.group(2)), m.group(3)
        if num in blocks and blocks[num] != name:
            dups[num].append(name)
        blocks[num] = name
        categories[num] = cat

    if not blocks:
        print("no block pages matched -- wiki layout may have changed", file=sys.stderr)
        raise SystemExit(1)

    ids = sorted(blocks)
    print(f"parsed {len(blocks)} block pages, id range {ids[0]}..{ids[-1]}")
    gaps = [i for i in range(ids[0], ids[-1] + 1) if i not in blocks]
    print(f"gaps in the id sequence: {len(gaps)}"
          + (f" (first few: {gaps[:12]})" if gaps else ""))
    if dups:
        print(f"ids appearing twice with different names: {len(dups)}")
        for k in sorted(dups)[:6]:
            print(f"   {k}: {blocks[k]!r} vs {dups[k]}")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({
            "source": INDEX,
            "note": "wiki id numbering; version/mod dependent -- reference only",
            "count": len(blocks),
            "blocks": {str(k): blocks[k] for k in ids},
            "categories": {str(k): categories[k] for k in ids},
        }, fh, ensure_ascii=False, indent=1)
    print(f"wrote {OUT}")

    # show the ones we care about
    print()
    for want in ["overdrive-dome", "overdrive-projector", "world-processor",
                 "micro-processor", "logic-processor", "hyper-processor",
                 "memory-cell", "memory-bank", "message", "switch"]:
        hits = [k for k in ids if blocks[k] == want]
        if hits:
            for h in hits:
                print(f"  {h:5d}  {want}  [{categories[h]}]")
        else:
            print(f"  {'--':>5}  {want}  (not found)")


if __name__ == "__main__":
    main()
