"""
block_ids_from_source.py -- derive Mindustry's block Content.id order from source.

Content.id is assigned by construction order (Content ctor: `id = getBy(type).size`),
and ContentLoader.createBaseContent() calls Blocks.load() before any mod content.
So the order in which `xxx = new SomeBlock("name"){{...}}` executes inside the
Blocks constructor IS the vanilla id order.

This script extracts that order and cross-checks it against the official wiki's
id table (tools/wiki_block_ids.json) to see whether the two agree.
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BLOCKS = os.path.join(os.path.dirname(HERE),
                      "Mindustry-Source", "core", "src", "mindustry",
                      "content", "Blocks.java")
WIKI = os.path.join(HERE, "wiki_block_ids.json")

# `fooBar = new SomeBlock("some-name"){{`  or  `fooBar = new SomeBlock("some-name"){`
DECL = re.compile(r'^\s*([A-Za-z_]\w*)\s*=\s*new\s+([A-Za-z_][\w.]*)\s*\(\s*"([a-z0-9\-]+)"')
# blocks constructed from a CONTENT object rather than a string, e.g.
#   oreCopper = new OreBlock(Items.copper){{      -> content name "ore-copper"
ORE = re.compile(r'^\s*([A-Za-z_]\w*)\s*=\s*new\s+(\w*OreBlock)\s*\(\s*Items\.(\w+)\s*\)')
# unnamed constructions, e.g.  `new ConstructBlock(i);`
ANON = re.compile(r'^\s*new\s+([A-Za-z_][\w.]*)\s*\(')
# the bulk-construct loop in Blocks.load()
LOOP = re.compile(r'for\s*\(\s*int\s+\w+\s*=\s*1\s*;\s*\w+\s*<=\s*Vars\.maxBlockSize\s*;')

CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def camel_to_kebab(s: str) -> str:
    return CAMEL.sub("-", s).lower()


def build_order(lines: list[str]) -> list[str]:
    """Ordered list of vanilla block names, including bulk-constructed ones.

    Content.id is assigned at construction, so anything constructed without a
    field reference still consumes an id:
      * `build1..build16`  -- the ConstructBlock bulk loop
      * `ore-<item>`       -- OreBlock(Items.x) constructors take a content
    """
    order: list[str] = []
    seen: set[str] = set()

    def push(nm: str) -> None:
        if nm and nm not in seen:
            seen.add(nm)
            order.append(nm)

    pending_bulk = False
    for ln in lines:
        if LOOP.search(ln):
            pending_bulk = True
            continue
        if pending_bulk:
            if ANON.match(ln):
                for i in range(1, 17):          # Vars.maxBlockSize = 16
                    push("build%d" % i)
                pending_bulk = False
                continue
            if ln.strip() and not ln.strip().startswith("//"):
                pending_bulk = False

        m = ORE.match(ln)
        if m:
            push("ore-" + camel_to_kebab(m.group(3)))
            continue

        m = DECL.match(ln)
        if m:
            push(m.group(3))
    return order


def main() -> None:
    with open(BLOCKS, encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    order = build_order(lines)

    print(f"parsed {len(order)} vanilla block constructions from Blocks.java")
    print(f"  first 10: {order[:10]}")
    print(f"  last   4: {order[-4:]}")
    print()

    src_index = {name: i for i, name in enumerate(order)}

    with open(WIKI, encoding="utf-8") as fh:
        wiki = json.load(fh)["blocks"]
    wiki_by_name = {v: int(k) for k, v in wiki.items()}
    print(f"wiki table has {len(wiki_by_name)} blocks")
    print()

    both = [n for n in src_index if n in wiki_by_name]
    print(f"names present in both: {len(both)}")
    offsets: dict[int, list[str]] = {}
    for n in both:
        offsets.setdefault(wiki_by_name[n] - src_index[n], []).append(n)

    print("wiki_id - source_index histogram (top 8):")
    for off, names in sorted(offsets.items(), key=lambda kv: -len(kv[1]))[:8]:
        print(f"   offset {off:+5d}: {len(names):4d} blocks   e.g. {names[:4]}")
    print()

    best = max(offsets.items(), key=lambda kv: len(kv[1]))
    pct = 100 * len(best[1]) / max(len(both), 1)
    print(f"dominant offset {best[0]:+d} covers {len(best[1])}/{len(both)} ({pct:.1f}%)")
    if len(offsets) == 1:
        print("=> ORDER IDENTICAL, only a constant shift (missing bulk-constructed blocks)")
    elif pct > 0.95:
        print("=> order effectively identical (a couple of inserts apart)")
    else:
        print("=> order differs; version mismatch or more unnamed constructions")

    print()
    print("=== offset segmentation (locates every missing construction) ===")
    pairs = sorted(((src_index[n], wiki_by_name[n], n) for n in both))
    cur_off = None
    seg_start = None
    for si, wi, n in pairs:
        off = wi - si
        if off != cur_off:
            if cur_off is not None:
                print(f"   offset {cur_off:+d}: source#{seg_start} .. "
                      f"(len {count})  first={first!r}")
            cur_off, seg_start, count, first = off, si, 1, n
        else:
            count += 1
    if cur_off is not None:
        print(f"   offset {cur_off:+d}: source#{seg_start} .. (len {count})  first={first!r}")

    print()
    print("spot checks:")
    for want in ["overdrive-dome", "overdrive-projector", "micro-processor",
                 "world-processor", "canvas", "message", "build1", "build16",
                 "ore-copper"]:
        s = src_index.get(want)
        w = wiki_by_name.get(want)
        print(f"   {want:22} source#{s if s is not None else '-':<5} "
              f"wiki={w if w is not None else '-'}")


if __name__ == "__main__":
    main()
