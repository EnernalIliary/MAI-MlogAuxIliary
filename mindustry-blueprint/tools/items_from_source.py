"""
items_from_source.py -- derive vanilla item content IDs from Items.java.

Why this exists
---------------
`Content.id = Vars.content.getBy(type).size` (Content.java:21) -> the ID is the
**construction order index**, not the order in the `public static Item a, b, c;`
declaration block. In Items.java those two orders differ: the field list starts
with `scrap`, but the first `new Item(...)` in load() is `copper`.

Block ids are derived the same way by block_ids_from_source.py.

Mods append after vanilla (ContentLoader runs createBaseContent() before
createModContent()), so these indices are stable for vanilla items in any modded
game -- only a *different game version* can shift them.

Output: tools/item_ids.json  {"items": {"0": "copper", ...}, "count": n}

Usage:
    python tools/items_from_source.py [path/to/Items.java] [-o out.json]
"""
from __future__ import annotations

import json
import os
import re
import sys

DEFAULT_SRC = r"Mindustry-Source/core/src/mindustry/content/Items.java"
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "item_ids.json")

# `copper = new Item("copper", ...)`  /  `copper = new Item("copper")`
CONSTRUCT = re.compile(r'(\w+)\s*=\s*new\s+Item\s*\(\s*"([^"]+)"')


def load_body(src: str) -> str:
    """Extract the text of `public static void load(){ ... }`."""
    text = open(src, encoding="utf-8").read()
    m = re.search(r"void\s+load\s*\(\s*\)\s*\{", text)
    if not m:
        raise SystemExit(f"could not find load() in {src}")
    i = m.end()
    depth = 1
    start = i
    while i < len(text) and depth:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return text[start:i - 1]


def derive(src: str) -> dict[int, str]:
    body = load_body(src)
    out: dict[int, str] = {}
    for idx, mm in enumerate(CONSTRUCT.finditer(body)):
        # sanity: the field name should match the constructor name
        out[idx] = mm.group(2)
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    src = args[0] if args else DEFAULT_SRC
    out = DEFAULT_OUT
    for i, a in enumerate(sys.argv):
        if a == "-o" and i + 1 < len(sys.argv):
            out = sys.argv[i + 1]

    if not os.path.exists(src):
        print(f"source not found: {src}", file=sys.stderr)
        return 2

    items = derive(src)
    data = {
        "source": os.path.basename(src),
        "note": "Content.id = construction order. Mods append; vanilla ids are stable per game version.",
        "count": len(items),
        "items": {str(k): v for k, v in sorted(items.items())},
    }
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"{len(items)} items -> {out}")
    print()
    print("id  name")
    for k, v in sorted(items.items()):
        print(f"{k:>3}  {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
