#!/usr/bin/env python
"""
check_ops.py -- static checker for mlog `op` arity and other silent mistakes.

Mindustry's `op` instruction has FIXED arity:
  unary  ops (abs, floor, sin, rand, ...)  -> `op <name> <out> <in>`        2 args
  binary ops (add, max, len, angle, ...)   -> `op <name> <out> <a> <b>`     3 args

Writing a third argument to a unary op is a silent mistake: the extra token is
never consumed, so the line does not do what the author intended.

Also flags:
  * `ucontrol` lines that do not have exactly 5 parameter slots
  * unknown op names
  * jumps to out-of-range instruction indices
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mlogsearch as M

# Mirror of mindustry.logic.LogicOp -- unary entries use an OpLambda1 ctor.
UNARY = {"abs", "sign", "log", "log10", "floor", "ceil", "round",
         "sqrt", "rand", "sin", "cos", "tan", "asin", "acos", "atan", "not"}

BINARY = {"add", "sub", "mul", "div", "idiv", "mod", "emod", "pow",
          "equal", "notEqual", "land", "lessThan", "lessThanEq",
          "greaterThan", "greaterThanEq", "strictEqual",
          "shl", "shr", "ushr", "or", "and", "xor",
          "max", "min", "angle", "angleDiff", "len", "noise", "logn"}

# Mirror of mindustry.logic.LUnitControl -- declared parameter counts.
# Missing trailing parameters are legal; the parser defaults them to "0".
UNIT_CONTROL_PARAMS = {
    "idle": 0, "stop": 0, "move": 2, "approach": 3, "pathfind": 2,
    "autoPathfind": 0, "boost": 1, "target": 3, "targetp": 2,
    "itemDrop": 2, "itemTake": 3, "payDrop": 0, "payTake": 1, "payEnter": 0,
    "mine": 2, "flag": 1, "build": 5, "deconstruct": 2, "getBlock": 5,
    "within": 4, "unbind": 0,
}

# legacy names LParser rewrites before lookup (LParser.java:11)
LEGACY = {"atan2": "angle", "dst": "len"}

OP_RE = re.compile(r"^op\s+(\S+)((?:\s+.*)?)$")


def strip_comment(line: str) -> str:
    """mlog comments start at '#' (LParser.comment()) -- but not inside strings."""
    out, in_str = [], False
    for ch in line:
        if ch == '"':
            in_str = not in_str
        elif ch == "#" and not in_str:
            break
        out.append(ch)
    return "".join(out).strip()



def expected_arity(op: str) -> int | None:
    op = LEGACY.get(op, op)
    if op in UNARY:
        return 2
    if op in BINARY:
        return 3
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--art", action="store_true", help="include pixel-art generators")
    ap.add_argument("--linux", action="store_true", help="include mlogv32 emulator")
    ap.add_argument("--show", type=int, default=20, help="how many offenders to print")
    args = ap.parse_args()

    blocks = M.load()
    if not args.art:
        blocks = [b for b in blocks if not M.is_art(b)]
    if not args.linux:
        blocks = [b for b in blocks if not M.is_linux(b)]

    arity: list[tuple] = []
    unknown: list[tuple] = []
    short_uctl: list[tuple] = []
    unknown_uctl: list[tuple] = []
    bad_jump: list[tuple] = []
    tally = collections.Counter()

    for b in blocks:
        n = len(b.lines)
        for i, raw in enumerate(b.lines):
            line = strip_comment(raw)
            if not line:
                continue

            m = OP_RE.match(line)
            if m:
                op, rest = m.group(1), (m.group(2) or "").split()
                want = expected_arity(op)
                if want is None:
                    unknown.append((b, i, line))
                elif len(rest) != want:
                    arity.append((b, i, line, op, len(rest), want))
                    tally[(op, len(rest), want)] += 1

            if line.startswith("ucontrol "):
                parts = line.split()
                action = parts[1] if len(parts) > 1 else ""
                declared = UNIT_CONTROL_PARAMS.get(action)
                given = len(parts) - 2                     # arguments written
                if declared is None:
                    unknown_uctl.append((b, i, line))
                elif given < declared:
                    # LEGAL: the parser fills the missing trailing params with "0".
                    # The canonical export always writes all 5 slots, so a short
                    # line just means it was hand-written / parsed from source.
                    short_uctl.append((b, i, line, action, given, declared))

            if line.startswith("jump "):
                parts = line.split()
                if len(parts) >= 2 and re.fullmatch(r"-?\d+", parts[1]):
                    tgt = int(parts[1])
                    if tgt != -1 and not (0 <= tgt < n):
                        bad_jump.append((b, i, line, tgt, n))

    def dump(title, rows, fmt, limit):
        print("=" * 78)
        print(f"{title}  ({len(rows)})")
        print("=" * 78)
        for r in rows[:limit]:
            print("  " + fmt(r))
        if len(rows) > limit:
            print(f"  ... and {len(rows) - limit} more")
        print()

    dump("op arity mismatch (extra/missing arguments)",
         sorted(arity, key=lambda r: (-r[5] + r[4], r[3])),
         lambda r: f"{r[0].file} @({r[0].x},{r[0].y}) L{r[1]}: `{r[2]}` "
                   f"(wrote {r[4]}, expects {r[5]})",
         args.show)

    if tally:
        print("  by (op, written, expected):")
        for k, v in tally.most_common():
            print(f"     {k}: {v}")
        print()

    dump("jump target out of range", bad_jump,
         lambda r: f"{r[0].file} @({r[0].x},{r[0].y}) L{r[1]}: `{r[2]}` "
                   f"-> {r[3]} but only {r[4]} statements",
         args.show)

    dump("ucontrol with fewer arguments than declared (legal -- defaults to 0)",
         short_uctl,
         lambda r: f"{r[0].file} @({r[0].x},{r[0].y}) L{r[1]}: `{r[2]}` "
                   f"({r[3]} takes {r[5]}, wrote {r[4]})",
         args.show)

    dump("unknown ucontrol action", unknown_uctl,
         lambda r: f"{r[0].file} @({r[0].x},{r[0].y}) L{r[1]}: `{r[2]}`",
         args.show)

    dump("unknown op name", unknown,
         lambda r: f"{r[0].file} @({r[0].x},{r[0].y}) L{r[1]}: `{r[2]}`",
         args.show)

    total = len(arity) + len(unknown) + len(unknown_uctl) + len(bad_jump)
    print(f"scanned {len(blocks)} real logic blocks -> {total} REAL problem(s)")
    print(f"  op arity mismatches : {len(arity)}   <-- these do not execute as written")
    print(f"  out-of-range jumps  : {len(bad_jump)}")
    print(f"  unknown op / action : {len(unknown) + len(unknown_uctl)}")
    print(f"  (informational) abbreviated ucontrol lines: {len(short_uctl)}")


if __name__ == "__main__":
    main()
