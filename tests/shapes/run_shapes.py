#!/usr/bin/env python3
"""Statement-shape invariants for the checker itself.

A check that parses a statement with an anchored regex has a silent failure
mode: the day the statement grows a token the regex stops matching, and the
check does not fail -- it simply never runs again.  That is exactly how
"op select k1 lessThan cu 6 @copper @dormant-cyst" (eight tokens) escaped the
op arity/name check for a whole release cycle: OP_RE allowed at most four
operands and every longer line fell straight through (2026-10-27).

The invariant pinned here: for every declared check, a malformed statement must
still produce its finding after the line is (a) padded with 1, 2 and 5 inert
trailing tokens -- LParser reads the fields it knows and ignores the rest -- and
(b) given a trailing comment.  Nothing else about the program changes, so a
missing finding can only mean the check failed open.

Run:  python tests/shapes/run_shapes.py [-v]
      python tests/shapes/run_shapes.py --module out/_oldchk/mlogcheck_head.py
The second form runs the same invariants against another copy of the checker --
that is how the harness itself is verified: pointed at a bounded-regex version
it must FAIL, otherwise it is not actually testing anything.

Exit: 0 every invariant holds, 1 a check failed open.
"""

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

if "--module" in sys.argv:
    _argv = sys.argv[1:]
    mlogcheck = _argv[_argv.index("--module") + 1]
    del _argv[_argv.index("--module"):_argv.index("--module") + 2]
    _spec = importlib.util.spec_from_file_location("mlogcheck_under_test", mlogcheck)
    mlogcheck = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mlogcheck)
else:
    import mlogcheck

# Each entry: the rule id that must fire, the index of the malformed statement
# inside lines, the program itself, why it is malformed, and (optionally) the
# links the processor has -- default none.
CHECKS = [
    dict(rid="unknown-op", at=1,
         lines=["set cu 3",
                "op select k1 lessThan cu 6 @copper @dormant-cyst",
                "end"],
         why="op name is not a LogicOp; the line is longer than four operands"),
    dict(rid="jump-target-out-of-range", at=1,
         lines=["set a 0", "jump 99 always a 0", "end"],
         why="jump target is past the end of the program"),
    dict(rid="jump-to-jump-chain", at=1,
         lines=["set a 0", "jump 2 always a 1", "jump 1 always a 1", "end"],
         why="unconditional jump into another unconditional jump"),
    dict(rid="self-read-undefined", at=1,
         lines=["set q 1", 'read v @this "nope"', "print v", "end"],
         why="read @this of a variable no statement mentions"),
    dict(rid="self-write-noop", at=0,
         lines=['write 1 @this "nope"', "end"],
         why="write @this of a variable no statement mentions"),
    dict(rid="ulocate-flag-invalid", at=0,
         lines=["ulocate building 0 false copper ox oy found extra", "end"],
         why="ulocate flag operand is 0"),
    dict(rid="link-name-not-bound", at=1,
         lines=["set a 0", "drawflush display1", "end"],
         why="block instruction targets a link the processor does not have"),
]

# Trailing tokens LParser ignores after the fields it reads, plus a comment.
VARIANTS = [("pad1", " 0"), ("pad2", " 0 0"), ("pad5", " 0 0 0 0 0"),
            ("comment", "  # padded")]


def rids(lines, links):
    return {f.rid for f in mlogcheck.analyse(list(lines), links=links)}


def main() -> int:
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    failures = []
    checked = 0
    for chk in CHECKS:
        links = chk.get("links", [])
        base = rids(chk["lines"], links)
        if chk["rid"] not in base:
            failures.append((chk, "base", "expected %s, got %s"
                             % (chk["rid"], sorted(x for x in base if x))))
            continue
        for name, tail in VARIANTS:
            lines = list(chk["lines"])
            lines[chk["at"]] = lines[chk["at"]] + tail
            got = rids(lines, links)
            checked += 1
            if chk["rid"] not in got:
                failures.append((chk, name, "expected %s, got %s"
                                 % (chk["rid"], sorted(x for x in got if x))))
            elif not base <= got:
                failures.append((chk, name, "lost %s"
                                 % sorted(x for x in base - got if x)))
            elif verbose:
                print("ok   %-24s %-8s %s" % (chk["rid"], name, lines[chk["at"]]))
    for chk, name, msg in failures:
        print("FAIL %-24s %-8s %s" % (chk["rid"], name, msg))
        print("     sample: %s" % chk["lines"][chk["at"]])
        print("     why   : %s" % chk["why"])
    bad = len(failures)
    print("%d shape(s) x %d variant(s): %s"
          % (len(CHECKS), len(VARIANTS),
             "FAILED (%d)" % bad if bad else "all still fire"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
