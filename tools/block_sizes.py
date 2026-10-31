"""
block_sizes.py -- derive block sizes from Mindustry source, for blueprint framing.

A schematic's width/height is used for the preview/frame; the game recomputes it
from the tiles, so being off is cosmetic. Still, better to be right.

Sizes come from two places in Blocks.load():
  * an explicit `size = N;` inside the block's initializer
  * the constructor argument for blocks that take one (ConstructBlock(i), ...)
Anything with no explicit size defaults to 1.
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BLOCKS = os.path.join(os.path.dirname(HERE), "Mindustry-Source", "core", "src",
                      "mindustry", "content", "Blocks.java")
OUT = os.path.join(HERE, "block_sizes.json")

DECL = re.compile(r'^\s*([A-Za-z_]\w*)\s*=\s*new\s+([A-Za-z_][\w.]*)\s*\(\s*"([a-z0-9\-]+)"')
ORE = re.compile(r'^\s*([A-Za-z_]\w*)\s*=\s*new\s+(\w*OreBlock)\s*\(\s*Items\.(\w+)\s*\)')
SIZE = re.compile(r"^\s*size\s*=\s*(\d+)\s*;")
LOOP = re.compile(r"for\s*\(\s*int\s+\w+\s*=\s*1\s*;\s*\w+\s*<=\s*Vars\.maxBlockSize\s*;")
CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def main() -> None:
    lines = open(BLOCKS, encoding="utf-8").read().split("\n")
    sizes: dict[str, int] = {}

    cur: str | None = None
    depth = 0
    pending_bulk = False

    for ln in lines:
        if LOOP.search(ln):
            pending_bulk = True
            continue
        if pending_bulk:
            if re.match(r"^\s*new\s+ConstructBlock", ln):
                for i in range(1, 17):
                    sizes["build%d" % i] = i
                pending_bulk = False
                continue
            if ln.strip() and not ln.strip().startswith("//"):
                pending_bulk = False

        m = ORE.match(ln)
        if m:
            cur = "ore-" + CAMEL.sub("-", m.group(3)).lower()
            sizes.setdefault(cur, 1)
            continue

        m = DECL.match(ln)
        if m:
            cur = m.group(3)
            sizes.setdefault(cur, 1)
            depth = ln.count("{") - ln.count("}")
            continue

        if cur:
            ms = SIZE.match(ln)
            if ms:
                sizes[cur] = int(ms.group(1))
            depth += ln.count("{") - ln.count("}")
            if depth <= 0 and ("}};" in ln or ln.rstrip().endswith("}};")):
                cur = None
                depth = 0

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(sizes, fh, ensure_ascii=False, indent=1, sort_keys=True)

    print(f"derived sizes for {len(sizes)} blocks -> {OUT}")
    multi = {k: v for k, v in sizes.items() if v > 1}
    print(f"  {len(multi)} have size > 1")
    print()
    for want in ["overdrive-dome", "overdrive-projector", "micro-processor",
                 "hyper-processor", "large-logic-display", "canvas",
                 "large-canvas", "memory-bank", "world-processor", "message",
                 "build3", "build16", "core-nucleus", "vault", "container"]:
        print(f"   {want:22} size={sizes.get(want, '(unknown)')}")


if __name__ == "__main__":
    main()
