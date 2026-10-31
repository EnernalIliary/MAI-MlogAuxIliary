#!/usr/bin/env python
"""
mlogsearch -- search every logic processor in the Mindustry blueprint folder.

Usage
-----
  python tools/mlogsearch.py grep   <regex>            # search code lines
  python tools/mlogsearch.py op     <opcode> [op2...]  # blocks using opcode(s)
  python tools/mlogsearch.py var    <name>             # blocks mentioning a var
  python tools/mlogsearch.py find   <substr>           # search blueprint names
  python tools/mlogsearch.py show   <blueprint> [idx]  # print a blueprint's logic
  python tools/mlogsearch.py stats                     # opcode histogram
  python tools/mlogsearch.py top    [n]                # biggest processors
  python tools/mlogsearch.py links  <blueprint>        # link names per processor

Common options
--------------
  --art / --no-art     include / exclude pixel-art generators
  --linux              include the mlogv32 emulator blueprint (off by default)
  --full               print the whole processor code, not just the matches
  --context N          lines of context around each match (default 0)
  --limit N            max blocks to print (default 25)
  -i                   case-insensitive

Examples
--------
  python tools/mlogsearch.py grep "op add @counter" --context 4
  python tools/mlogsearch.py op ucontrol ubind
  python tools/mlogsearch.py var 不应期
  python tools/mlogsearch.py show 搬运t4
  python tools/mlogsearch.py find 集火
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import msch

SCHEM = os.environ.get("MLOG_CORPUS") or os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", "Mindustry", "schematics")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "logic-corpus", "blocks.json")

# Blueprints that are auto-generated pixel art or the RISC-V emulator.
ART_PAT = re.compile(r"(^pic|pic_|BADAPPLE|涩图|^AKIRA|picturebigger|吃吃B|"
                     r"intel corei3|1v1\.msch|^__\.msch|^\.\.\.)", re.I)
LINUX_PAT = re.compile(r"Linux on mlogv32", re.I)


class Block:
    __slots__ = ("file", "name", "kind", "x", "y", "links", "lines", "labels")

    def __init__(self, file, name, kind, x, y, links, lines, labels):
        self.file, self.name, self.kind = file, name, kind
        self.x, self.y, self.links = x, y, links
        self.lines, self.labels = lines, labels

    @property
    def id(self):
        return f"{self.file}#({self.x},{self.y})"

    def to_json(self):
        return [self.file, self.name, self.kind, self.x, self.y,
                self.links, self.lines, self.labels]

    @staticmethod
    def from_json(d):
        return Block(*d)


def build_cache(verbose=False):
    files = sorted(f for f in os.listdir(SCHEM) if f.lower().endswith(".msch"))
    out = []
    for fname in files:
        try:
            sch = msch.parse_file(os.path.join(SCHEM, fname))
        except Exception:
            continue
        for t in sorted(sch.logic_tiles(), key=lambda t: (t.y, t.x)):
            assert t.logic is not None
            out.append(Block(fname, sch.name, t.block, t.x, t.y,
                             [l.name for l in t.logic.links],
                             t.logic.statement_lines(),
                             list(sch.labels)))
        if verbose:
            print(f"  {fname}: {len(out)}", file=sys.stderr)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as fh:
        json.dump([b.to_json() for b in out], fh, ensure_ascii=False)
    return out


def load(refresh=False) -> list[Block]:
    if refresh or not os.path.exists(CACHE):
        return build_cache()
    with open(CACHE, encoding="utf-8") as fh:
        return [Block.from_json(d) for d in json.load(fh)]


def first_op(line: str) -> str:
    p = line.split(" ", 1)
    return p[0] if p else ""


def is_art(b: Block) -> bool:
    return bool(ART_PAT.search(b.file))


def is_linux(b: Block) -> bool:
    return bool(LINUX_PAT.search(b.file))


def header(b: Block) -> str:
    return (f"{b.file}  [{b.kind} @{b.x},{b.y}]  "
            f"{len(b.lines)} stmts" + (f"  links={b.links}" if b.links else ""))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_grep(blocks, args):
    rx = re.compile(args.pattern, re.I if args.insensitive else 0)
    hits = 0
    for b in blocks:
        idx = [i for i, l in enumerate(b.lines) if rx.search(l)]
        if not idx:
            continue
        hits += 1
        if hits > args.limit:
            continue
        print(f"\n=== {header(b)}  ({len(idx)} matches)")
        if args.full:
            for i, l in enumerate(b.lines):
                print(f"  {i:5d}  {l}")
        else:
            shown = set()
            for i in idx[:40]:
                lo = max(0, i - args.context)
                hi = min(len(b.lines), i + args.context + 1)
                for j in range(lo, hi):
                    if j in shown:
                        continue
                    shown.add(j)
                    mark = ">" if j == i else " "
                    print(f"  {mark}{j:5d}  {b.lines[j]}")
                print("        ...")
    print(f"\n{hits} logic block(s) matched." + (
        f" (showing first {args.limit})" if hits > args.limit else ""))


def cmd_op(blocks, args):
    wanted = set(args.opcodes)
    rows = []
    for b in blocks:
        c = collections.Counter(first_op(l) for l in b.lines)
        used = {o: c[o] for o in wanted if c[o]}
        if len(used) == len(wanted):
            rows.append((sum(used.values()), b, used))
    rows.sort(key=lambda r: -r[0])
    print(f"{len(rows)} block(s) use ALL of {sorted(wanted)}\n")
    for tot, b, used in rows[:args.limit]:
        print(f"{tot:6d}  {used}  {header(b)}")


def cmd_var(blocks, args):
    rx = re.compile(r"\b" + re.escape(args.name) + r"\b",
                    re.I if args.insensitive else 0)
    rows = []
    for b in blocks:
        c = sum(1 for l in b.lines if rx.search(l))
        if c:
            rows.append((c, b))
    rows.sort(key=lambda r: -r[0])
    print(f"{len(rows)} block(s) mention '{args.name}'\n")
    for c, b in rows[:args.limit]:
        print(f"{c:6d}  {header(b)}")


def cmd_find(blocks, args):
    seen = {}
    for b in blocks:
        for key in (b.file, b.name):
            if args.substr.lower() in key.lower():
                seen.setdefault(b.file, []).append(b)
    if not seen:
        print("no matching blueprint")
        return
    for f, bs in sorted(seen.items()):
        tot = sum(len(b.lines) for b in bs)
        print(f"{f}   ({len(bs)} procs, {tot} stmts)")
        for b in bs[:6]:
            print(f"    [{b.kind} @{b.x},{b.y}] {len(b.lines)} stmts "
                  f"links={b.links}")


def cmd_show(blocks, args):
    want = args.blueprint.lower()
    sel = [b for b in blocks
           if want in b.file.lower() or want in b.name.lower()]
    if not sel:
        print("no matching blueprint")
        return
    if args.index is not None:
        sel = [sel[args.index]] if args.index < len(sel) else []
    for b in sel:
        print(f"\n{'='*78}\n=== {header(b)}")
        for i, l in enumerate(b.lines):
            print(f"  {i:5d}  {l}")


def cmd_stats(blocks, args):
    ops = collections.Counter()
    for b in blocks:
        ops.update(first_op(l) for l in b.lines)
    print(f"{len(blocks)} blocks, {sum(ops.values()):,} statements\n")
    for op, c in ops.most_common(args.limit if args.limit != 25 else 60):
        print(f"  {op:18s} {c:10,d}")


def cmd_top(blocks, args):
    rows = sorted(blocks, key=lambda b: -len(b.lines))
    for b in rows[:args.limit]:
        print(f"{len(b.lines):6d}  {header(b)}")


def cmd_links(blocks, args):
    want = args.blueprint.lower()
    for b in blocks:
        if want in b.file.lower() or want in b.name.lower():
            print(f"{b.file} [{b.kind} @{b.x},{b.y}] {len(b.lines)} stmts")
            for i, n in enumerate(b.links):
                print(f"    {i:3d}  {n}")
            print()


# --------------------------------------------------------------------------- #

def main():
    # Shared options are attached to the top level AND to every subparser so
    # they can be written on either side of the command name.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--art", action="store_true", help="include pixel-art generators")
    common.add_argument("--linux", action="store_true", help="include mlogv32 emulator")
    common.add_argument("--full", action="store_true", help="print full code")
    common.add_argument("--context", type=int, default=0)
    common.add_argument("--limit", type=int, default=25)
    common.add_argument("-i", dest="insensitive", action="store_true")
    common.add_argument("--refresh", action="store_true", help="rebuild cache")

    ap = argparse.ArgumentParser(prog="mlogsearch", parents=[common],
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)

    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    p = add("grep"); p.add_argument("pattern"); p.set_defaults(fn=cmd_grep)
    p = add("op"); p.add_argument("opcodes", nargs="+"); p.set_defaults(fn=cmd_op)
    p = add("var"); p.add_argument("name"); p.set_defaults(fn=cmd_var)
    p = add("find"); p.add_argument("substr"); p.set_defaults(fn=cmd_find)
    p = add("show"); p.add_argument("blueprint")
    p.add_argument("index", nargs="?", type=int, default=None); p.set_defaults(fn=cmd_show)
    add("stats").set_defaults(fn=cmd_stats)
    p = add("top"); p.add_argument("n", nargs="?", type=int, default=None)
    p.set_defaults(fn=cmd_top)
    p = add("links"); p.add_argument("blueprint"); p.set_defaults(fn=cmd_links)

    args = ap.parse_args()
    # a positional n (top) or an explicit --limit both land on args.limit
    if getattr(args, "n", None):
        args.limit = args.n
    blocks = load(args.refresh)
    if not args.art:
        blocks = [b for b in blocks if not is_art(b)]
    if not args.linux:
        blocks = [b for b in blocks if not is_linux(b)]
    args.fn(blocks, args)


if __name__ == "__main__":
    main()
