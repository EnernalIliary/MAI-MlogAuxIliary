#!/usr/bin/env python3
"""Regression tests for the mlog toolchain.

Why this exists
---------------
`mlog.py selftest` only proves the *parser* round-trips the corpus (429/429).
It says nothing about the five analyzers: `check`, `ir`, `values`, `pack`,
`cross` and the version gate were all validated by eyeballing output against
hand-computed numbers. That is exactly how every false positive in this project
got in -- `rand` modelled as NaN, `defs_of` missing `getblock`, dead-write
counting only explicit `read`. Each was found by luck, on real data, after the
fact.

So: golden digests per case. A digest is a **normalised, wording-free** summary
of what the analyzer concluded (`(severity, line)` pairs, statement counts,
branch verdicts, capacity bits...). Wording can be rewritten freely; if a
conclusion changes, a test fails.

Usage
-----
    python tests/run_tests.py            # compare (exit 1 on any mismatch)
    python tests/run_tests.py --write    # regenerate tests/expected.json
    python tests/run_tests.py -v         # show every digest
    python tests/run_tests.py --case val # run a subset by name substring

After changing an analyzer on purpose, run `--write`, then **read the diff** on
`tests/expected.json` before committing. The diff is the review.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
EXPECTED = HERE / "expected.json"
# 公开仓只发一份 tools/（工作区里的三份副本由 gate.py 的 mirror 步守着），
# 所以这里直接指向本仓的 tools/。
TOOLS = HERE.parent / "tools"

ROOT = HERE.parent
sys.path.insert(0, str(TOOLS))


def _mirror_stale_reason() -> str | None:
    """Why is this suite about to grade stale code?  None = fine.

    Upstream this suite grades the ``mindustry-blueprint/tools`` mirror, because a
    stale mirror keeps the suite green on old code (that failure hid the
    status-slot mistake on 2026-10-25).  This public tree ships a single
    ``tools/``, so there is no mirror to compare against and this always
    returns None.
    """
    script = ROOT / "tools" / "sync_mlogagent.py"
    if not script.is_file() or not TOOLS.is_dir():
        return None
    try:
        cp = subprocess.run(
            [sys.executable, str(script), "--mirror", "all", "--check"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
    except Exception as exc:                                  # noqa: BLE001
        return f"could not run the mirror check: {type(exc).__name__}: {exc}"
    if cp.returncode == 0:
        return None
    tail = [ln for ln in (cp.stdout or "").splitlines() if ln.strip()][-6:]
    return "\n".join(tail)

import mlogcheck as C          # noqa: E402
import mlogir as I             # noqa: E402
import mlogpack as P           # noqa: E402
import mlogval as V            # noqa: E402


def _lines(name: str) -> list[str]:
    return (CASES / name).read_text("utf-8").splitlines()


def _prepared(name: str) -> list[tuple[int, str]]:
    return C.prepare(_lines(name))


# --------------------------------------------------------------------------- #
# Each digest_* returns JSON-able data. Keep them small and semantic: if a
# human can't tell what changed from the diff, the digest is too coarse.
# --------------------------------------------------------------------------- #

def digest_check(name: str) -> list:
    st = _prepared(name)
    return [[f.sev, f.line] for f in C.analyse([c for _, c in st])]


def digest_check_msg(name: str) -> list:
    """First 40 chars of each message -- catches a finding changing its *kind*
    (signature vs error) without pinning the whole wording."""
    st = _prepared(name)
    return [[f.sev, f.line, f.msg[:40]] for f in C.analyse([c for _, c in st])]


def digest_check_rid(name: str) -> list:
    """(severity, statement line, rule id) -- pins the *classification*, not wording.

    A finding's id comes from tools/rule_ids.py, which picks the rule with the
    longest matching message fragment.  That is exactly how the fragment-link
    INFO once got attributed to unary-op-extra-operand (see
    out/takeover-20261024/24-anon-corpus.md section 10.2), so the id is worth pinning.
    """
    st = _prepared(name)
    return [[f.sev, f.line, f.rid] for f in C.analyse([c for _, c in st])]


def digest_check_nolinks(name: str) -> list:
    """Same as digest_check_rid, but links=[] -- the corpus case.

    check_corpus.py passes the links a blueprint actually carries; an empty list
    means "we know this program has no links", which is what downgrades a
    block-shaped name to the fragment INFO.  analyse(lines) alone passes
    links=None ("unknown"), so it cannot reach that branch.
    """
    st = _prepared(name)
    return [[f.sev, f.line, f.rid] for f in C.analyse([c for _, c in st], links=[])]


def digest_ir(name: str) -> dict:
    """Dead code / dead stores, reported as **1-based file lines**.

    `mlogir` hands back statement indices; converting here keeps the golden
    file readable next to the fixture (a reviewer can count lines in the file
    and compare, instead of re-deriving the offset).
    """
    prog = I.parse(_lines(name))
    line = lambda i: prog.stmts[i].line           # noqa: E731
    return {"stmts": len(prog.stmts),
            "dead": [line(i) for i in sorted(prog.dead())],
            "stores": [[line(i), v] for i, v in sorted(prog.dead_stores())],
            "kinds": [[line(i), v, k] for i, v, k in
                      sorted(prog.classify_dead_stores())],
            "labels": prog.labels,
            "writes_counter": prog.writes_counter()}


def digest_values(name: str) -> dict:
    prog = I.parse(_lines(name))
    a = V.analyze(prog)
    return {"rounds": a.rounds, "stable": a.stable,
            "branches": [[ln, verdict, str(v)] for ln, verdict, v in a.branches],
            "nulls": sorted(a.nulls)}


def digest_pack(name: str) -> dict:
    prog = I.parse(_lines(name))
    sites = P.find_pack_sites(prog)
    peels = P.find_bit_peels(prog)
    return {"sites": [
        {"low_exp": s.low_exp, "cap": s.capacity_bits,
         "fields": [[f.kind, f.exp, f.dst] for f in s.fields],
         "overlaps": len(s.overlaps), "masked": len(s.masked)}
        for s in sites],
        "peels": [[p.line, p.value, p.lowbit] for p in peels]}


def digest_gate(name: str) -> dict:
    floor, users = C.build_floor(_prepared(name))
    return {"floor": floor, "users": users, "baseline": C.BASELINE_BUILD,
            "reports": floor > C.BASELINE_BUILD}


def digest_cost(name: str) -> dict:
    """Pull the numbers a human actually checks; the prose can change."""
    text = C.cost_report(_prepared(name), None)
    out = {}
    unreachable = []
    in_unreachable = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("UNREACHABLE"):
            in_unreachable = True
            continue
        if s.startswith(("opcodes", "loop heads", "worst-case", "⏸", "reachability")):
            in_unreachable = False
        if s.startswith("worst-case path"):
            out["worst"] = s.split(":", 1)[1].strip()
        elif s.startswith("⏸ blocking"):
            out["blocking"] = s.split(":", 1)[1].strip()
        elif in_unreachable and re.match(r"L\d+:", s):
            unreachable.append(int(s[1:s.index(":")]))
    if unreachable:
        out["unreachable"] = unreachable
    return out


def digest_cross_values() -> dict:
    """`values` fed by `cross`: a read of a constant slot folds to that constant.

    The `jump done equal y z` case is the point: without cross knowledge `y` is
    TOP and the branch cannot be resolved; with it `y` is null (0) and `z` is 3,
    so the comparison is provably never taken.
    """
    import mlogcross
    reader = ['read x w1 "v"', 'print x', 'read y w1 "ghost"', 'print y',
              'set z 3', 'jump done equal y z', 'print "no"', 'done:', 'end']
    units = [mlogcross.Unit("writer", (0, 0), ["set v 42", "end"], {}),
             mlogcross.Unit("reader", (1, 0), reader, {"w1": (0, 0)})]
    known = mlogcross.slot_values(mlogcross.analyze_units(units), units)
    prog = I.parse(reader)
    bare = V.analyze(prog)
    fed = V.analyze(prog, slots=known.get((1, 0), {}))
    return {"bare": {"folded": bare.cross_folded,
                     "branches": [[ln, m] for ln, m, _ in bare.branches],
                     "nulls": sorted(bare.nulls)},
            "fed": {"folded": fed.cross_folded,
                    "branches": [[ln, m] for ln, m, _ in fed.branches],
                    "nulls": sorted(fed.nulls),
                    "x": str(fed.after.get(0, {}).get("x")),
                    "y": str(fed.after.get(2, {}).get("y"))}}


def digest_odd() -> dict:
    """Fullwidth punctuation in a name (the IME-typo check, `ir`-level)."""
    import mlog
    prog = I.parse(_lines("oddname.mlog"))
    return {"odd": {k: v for k, v in mlog.odd_names(prog).items()}}


def digest_cross_unit() -> dict:
    """Cross-processor analysis on hand-built units -- no .msch needed.

    Covers the four verdicts: a constant slot, a slot nobody assigns, and a
    write nobody reads (the false positive that started it).
    """
    import mlogcross
    units = [
        mlogcross.Unit("writer", (0, 0),
                       'set v 42\nwrite 7 reader1 "orphan"\nend',
                       {"reader1": (1, 0)}),
        mlogcross.Unit("reader", (1, 0),
                       'read x w1 "v"\nread y w1 "ghost"\nprint x\nprint y\nend',
                       {"w1": (0, 0)}),
    ]
    rep = mlogcross.analyze_units(units)
    return {"cross_reads": rep.cross_reads,
            "never_assigned": sorted((r.name, r.line) for r in rep.never_written),
            "constant": sorted((r.name, str(v)) for r, v in rep.exact),
            "dead_writes": sorted(s.name for s in rep.dead_writes)}


def digest_selftest() -> dict:
    """The corpus round-trip is *not* duplicated here.

    `mlog.py selftest` already reports `429/429 exact round-trip`; re-running it
    from the test suite doubled the runtime without adding a distinct signal.
    The suite covers the five analyzers, which is where the false positives came
    from.
    """
    return {}


# name -> (kind, file-or-None, digest fn)
SUITE: list[tuple[str, str, str | None]] = [
    ("gate/noop-drift",        "gate",    "gate1.mlog"),
    ("gate/baseline-silent",   "gate",    "baseline.mlog"),
    ("gate/op-emod",           "gate",    "gate-emod.mlog"),
    # Corpus-driven classification: an index-only variable is summarised into
    # ONE finding (not one per statement), and it must keep its own rule id.
    ("check/index-only-summary", "check_rid", "index-only-summary.mlog"),
    # 0 links + >200 statements = a fragment cropped out of a larger base: the
    # block-shaped name is INFO (link-name-unbound-fragment), not WARN.
    ("check/link-unbound-fragment", "check_nolinks", "link-unbound-fragment.mlog"),
    # A never-assigned name in a numeric slot reads as 0 (INFO); the same name
    # in an object slot hands null to the instruction (ERR) -- ref m07644.
    ("check/undefined-var-num-slot", "check_rid", "undefined-var-num-slot.mlog"),
    # radar/uradar take no count: the 6th operand is a sort direction, so a
    # number > 1 there is the shape the old doc taught (radar-takes-no-count).
    ("check/radar-count-misuse", "check_rid", "radar-count-misuse.mlog"),
    ("check/radar-count-ok", "check_rid", "radar-count-ok.mlog"),
    ("ir/labels-resolved",     "ir",      "label.mlog"),
    ("ir/odd-name",            "odd",     None),
    ("ir/stale-write",        "ir",      "stale.mlog"),
    ("check/labels-blank",     "check",   "labels-check.mlog"),
    ("check/reactor-slow",     "check",   "reactor-guard-slow.mlog"),
    ("check/reactor-ok",       "check",   "reactor-guard-ok.mlog"),
    ("check/reactor-unknown",  "check",   "reactor-guard-unknown.mlog"),
    ("check/reactor-impact",   "check",   "reactor-guard-impact.mlog"),
    ("check/reactor-impact-ok", "check",  "reactor-guard-impact-ok.mlog"),
    # A finding's line number is the STATEMENT index -- the same number
    # `jump` uses -- not the line you would count in the file. The label
    # on line 1 of this fixture is not a statement, so a raw-line number
    # would report 1 and 2 instead of 0 and 1.
    ("check/label-index", "check", "label-index.mlog"),
    ("check/virus",           "check",   "virus-logic.mlog"),
    # Privileged built-ins are INFO, not WARN: the same file may legitimately be
    # pasted into a world processor. The point is the line number and severity.
    ("check/privileged-var",  "check",   "privileged-var.mlog"),
    # read/write @this "name" addresses a processor VARIABLE by string; an
    # undeclared name reads null / writes nothing. INFO, because both are
    # legal -- and the second one is the silent no-op worth seeing.
    ("check/self-readwrite",  "check",   "self-readwrite.mlog"),
    ("check/link-hyphen",     "check",   "link-hyphen.mlog"),
    # A hyphenated name the program assigns is a legal variable; a hyphen
    # inside a quoted string is not a name at all. Neither may be reported.
    ("check/link-hyphen-ok",  "check",   "link-hyphen-ok.mlog"),
    # `sensor` reads LAccess attributes; an item/block name there reads null.
    ("check/sensor-not-prop", "check",   "sensor-not-prop.mlog"),
    # The effect slot of `status` is a literal content name, not a variable
    # read (LExecutor.java:1743 -> ContentLoader.getByName). The bare spelling
    # is the working one; `@unmoving` is the spelling that does nothing.
    ("check/status-effect-slot", "check", "status-effect-slot.mlog"),
    ("check/status-bare-ok",     "check", "status-bare-ok.mlog"),
    ("check/spin-deadlock",   "check",   "spin.mlog"),
    ("check/spin-handshake",  "check",   "spin-ok.mlog"),
    ("pack/three-fields",      "pack",    "pack1.mlog"),
    ("values/const-branch",    "values",  "val1.mlog"),
    ("values/arithmetic",      "values",  "val2.mlog"),
    ("values/unknown-input",   "values",  "val3.mlog"),
    ("cost/wait-single",       "cost",    "wait1.mlog"),
    ("cost/wait-nested",       "cost",    "wait2.mlog"),
    ("cost/counter-op",        "cost",    "counter-op.mlog"),
    ("cost/counter-set",       "cost",    "counter-set.mlog"),
    ("cost/counter-unknown",   "cost",    "counter-unknown.mlog"),
    ("check/declared-by-mention", "check", "declared-by-mention.mlog"),
    # The operator NAME inside `op` has to be validated even when the line is
    # long. OP_RE stopped matching after four tokens, so
    # `op select k1 lessThan cu 6 @copper @dormant-cyst` was skipped whole and
    # the checker reported "no issues" -- in game LParser.java:142-147 replaces
    # the line with an InvalidStatement (a noop), the destination variable stays
    # null and nothing says why. Eight-token line, one ERR, nothing else.
    ("check/op-long-unknown", "check_msg", "op-long-unknown.mlog"),
    # v0.13.2: a statement no jump can reach is a finding now, not just a line
    # in the cost report. The positive fixture has `jump loop always` in front of
    # a `control shootp ... null 0`, i.e. the statement that turns a turret off;
    # the counter-example does the same loop with labels and stays silent.
    ("check/unreachable",      "check",   "unreachable-statement.mlog"),
    ("check/loop-labels-ok",   "check",   "loop-labels-ok.mlog"),
    ("tool/aigenerated",       "aigenerated", None),
    ("gen/normalized",         "gen",     None),
    ("tool/mcp",               "mcp",     None),
    ("cross/unit-verdicts",    "cross",   None),
    ("values/cross-fold",     "crossvalues", None),
]

def digest_aigenerated() -> dict:
    """Sign, verify, tamper, re-sign, strip -- on a scratch copy."""
    tool = HERE.parent / "tools" / "aigenerated.py"
    out: dict = {}
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.mlog"
        shutil.copyfile(CASES / "aigenerated.mlog", p)

        def run(*a: str) -> tuple[int, str]:
            r = subprocess.run([sys.executable, str(tool), *a],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            return r.returncode, r.stdout + r.stderr

        out["sign"], _ = run("sign", str(p))
        out["verify"], _ = run("verify", str(p))
        body = p.read_text(encoding="utf-8")
        out["token"] = bool(re.search(r'"aigenerated:[^"]+"\s*$', body))
        p.write_text(body.replace("set x 1", "set x 2"), "utf-8", newline="\n")
        out["verify_tampered"], _ = run("verify", str(p))
        out["resign_refused"], msg = run("sign", str(p))
        out["refusal_msg"] = "body changed since signing" in msg
        out["resign_forced"], _ = run("sign", "--force", str(p))
        out["verify_forced"], _ = run("verify", str(p))
        out["strip"], _ = run("strip", str(p))

        # placement: reuse the compare slot of an existing unconditional jump.
        # v0.14.2: the `end` behind such a jump can never run, so signing drops it
        # (mlogcheck would report it as an unreachable statement) -- the token line
        # is therefore the LAST line now, and no `end` survives.
        j = Path(d) / "slot.mlog"
        j.write_text("set x 1\njump 27 always 0\nend\n", "utf-8", newline="\n")
        out["slot_sign"], _ = run("sign", str(j))
        sl = j.read_text(encoding="utf-8").splitlines()
        out["slot_line"] = sl[-1]
        out["slot_end_kept"] = any(l.strip() == "end" for l in sl)
        # placement: no end -> append terminal jump 0
        t = Path(d) / "term.mlog"
        t.write_text("set x 1\nprint x\n", "utf-8", newline="\n")
        out["terminal_sign"], _ = run("sign", str(t))
        out["terminal_line"] = t.read_text(encoding="utf-8").splitlines()[-1]

        # comments travel inside the signature payload, with original lines
        c = Path(d) / "c.mlog"
        c.write_text("# head\nset x 1\n# mid\nprint \"hi\"\nend\n",
                     "utf-8", newline="\n")
        out["comments_sign"], _ = run("sign", "--force", str(c))
        out["comments_body_has_none"] = not any(
            l.strip().startswith("#")
            for l in c.read_text(encoding="utf-8").splitlines()[:-1])
        _, txt = run("extract", str(c))
        out["comments_extract"] = [l for l in txt.splitlines() if l.startswith("L")]
        out["comments_restore"], _ = run("restore", str(c))
        out["comments_lines"] = [
            i + 1 for i, l in enumerate(c.read_text(encoding="utf-8").splitlines())
            if l.strip().startswith("#")]
        out["comments_verify"], _ = run("verify", str(c))

        # `mlog.py gen` must sign both the source and the emitted blueprint.
        import msch
        mlog_tool = HERE.parent / "tools" / "mlog.py"
        for label, extra in (("gen", []), ("gen_nosign", ["--no-sign"])):
            src = Path(d) / f"{label}.mlog"
            shutil.copyfile(CASES / "aigenerated.mlog", src)
            bp = Path(d) / f"{label}.msch"
            r = subprocess.run(
                [sys.executable, str(mlog_tool), "gen", str(src),
                 "--name", "t", "--binary", str(bp), "--quiet", *extra],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            code = msch.parse_file(bp).logic_tiles()[0].logic.code
            out[f"{label}_rc"] = r.returncode
            out[f"{label}_signed"] = "aigenerated:" in code
            out[f"{label}_source_signed"] = "aigenerated:" in src.read_text(
                encoding="utf-8")
    return out


def digest_gen() -> dict:
    """`gen` must normalise schematic coordinates and strip a source BOM.

    Regression: `--at 20,20` used to be written raw, so a 4x1 build claimed a
    24x21 canvas and pasted 20 tiles away from the cursor; a BOM-prefixed first
    line compiled to a Noop and silently vanished.
    """
    import msch
    out: dict = {}
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "g.mlog"
        # deliberately BOM-prefixed, like PowerShell's Set-Content
        src.write_text("\ufeffwrite 42 cell1 0\nread r cell1 0\nprint r\nend\n",
                       encoding="utf-8", newline="\n")
        bp = Path(d) / "g.msch"
        r = subprocess.run(
            [sys.executable, str(HERE.parent / "tools" / "mlog.py"), "gen", str(src),
             "--name", "g", "--at", "20,20", "--link", "cell1@3,0",
             "--with-block", "memory-cell@3,0", "--binary", str(bp),
             "--no-sign", "--quiet"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        sch = msch.parse_file(bp)
        out["rc"] = r.returncode
        out["size"] = [sch.width, sch.height]
        out["tiles"] = [[t.block, t.x, t.y] for t in sch.tiles]
        out["links"] = [[l.name, l.x, l.y]
                        for t in sch.tiles if t.logic for l in t.logic.links]
        out["first_line"] = sch.logic_tiles()[0].logic.code.splitlines()[0]
    return out


def digest_mcp() -> dict:
    """The MCP server must hand out the tool list and serve real results."""
    out: dict = {}
    server = HERE.parent / "tools" / "mlog_mcp.py"
    p = subprocess.Popen([sys.executable, str(server)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")

    def rpc(obj):
        p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())

    try:
        hello = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        out["server"] = hello["result"]["serverInfo"]["name"]
        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        out["tools"] = [t["name"] for t in tools]
        chk = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "mlog_check",
                              "arguments": {"path": str(CASES / "link-hyphen-ok.mlog")}}})
        out["check"] = json.loads(chk["result"]["content"][0]["text"])[0]["counts"]
        out["has_sim"] = "mlog_sim" in out["tools"]
    finally:
        p.stdin.close()
        p.terminate()
        p.wait(timeout=10)
    return out


DIGESTS = {
    "gate": digest_gate, "check": digest_check, "check_msg": digest_check_msg,
    "check_rid": digest_check_rid, "check_nolinks": digest_check_nolinks,
    "ir": digest_ir, "values": digest_values, "pack": digest_pack,
    "cost": digest_cost, "cross": lambda _f: digest_cross_unit(),
    "crossvalues": lambda _f: digest_cross_values(),
    "aigenerated": lambda _f: digest_aigenerated(),
    "gen": lambda _f: digest_gen(),
    "mcp": lambda _f: digest_mcp(),
    "odd": lambda _f: digest_odd(),
}


def run(only: str | None) -> dict:
    out = {}
    for label, kind, case in SUITE:
        if only and only.lower() not in label.lower():
            continue
        try:
            d = DIGESTS[kind](case) if case else DIGESTS[kind](None)
            # Round-trip through JSON so tuples and lists compare equal: the
            # golden file is JSON, and `(a, b) != [a, b]` would make every
            # tuple-containing digest fail for no reason.
            out[label] = {"kind": kind, "case": case,
                          "digest": json.loads(json.dumps(d, ensure_ascii=False))}
        except Exception as exc:                      # noqa: BLE001
            out[label] = {"kind": kind, "case": case,
                          "error": f"{type(exc).__name__}: {exc}"}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="regenerate expected.json")
    ap.add_argument("--case", help="only run labels containing this substring")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-mirror-check", action="store_true",
                    help="skip the tools/ vs mindustry-blueprint/tools check")
    args = ap.parse_args()

    if not args.no_mirror_check:
        why = _mirror_stale_reason()
        if why:
            print("refusing to run: the analyzers under test would be the stale COPY\n"
                  f"  {TOOLS.relative_to(ROOT)}\n"
                  "  ...not tools/.  Run: python tools/sync_mlogagent.py --mirror all\n"
                  "  (--no-mirror-check skips this when the tree has no mirror)",
                  file=sys.stderr)
            print(why, file=sys.stderr)
            return 2

    got = run(args.case)
    if args.write:
        EXPECTED.write_text(json.dumps(got, indent=2, ensure_ascii=False) + "\n",
                            "utf-8")
        print(f"wrote {len(got)} digest(s) to {EXPECTED.relative_to(HERE.parent)}")
        print("read the diff before committing -- the diff is the review")
        return 0

    if not EXPECTED.exists():
        print("no expected.json yet -- run with --write first", file=sys.stderr)
        return 2
    want = json.loads(EXPECTED.read_text("utf-8"))

    bad = 0
    for label, entry in got.items():
        exp = want.get(label)
        if exp is None:
            print(f"NEW   {label}  (not in expected.json)")
            bad += 1
            continue
        if entry.get("digest") == exp.get("digest") and "error" not in entry:
            if args.verbose:
                print(f"ok    {label}")
            continue
        bad += 1
        print(f"FAIL  {label}")
        if "error" in entry:
            print(f"      raised {entry['error']}")
            continue
        a, b = exp.get("digest"), entry.get("digest")
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                if a.get(k) != b.get(k):
                    print(f"      {k}: expected {a.get(k)!r}")
                    print(f"      {k}:      got {b.get(k)!r}")
        else:
            print(f"      expected {a!r}")
            print(f"      got      {b!r}")

    # A case that disappeared from the suite is a failure, not a note: otherwise
    # "delete the case that broke" is a green path past the whole gate.
    missing = sorted(set(want) - set(got))
    for label in missing:
        bad += 1
        print(f"STALE {label}  (in expected.json, no longer in the suite)")
    if missing:
        print("      restoring the case or dropping its expected.json entry are the"
              " only two honest fixes")

    total = len(got)
    print(f"\n{total - bad}/{total} passed" + ("" if not missing else
                                               f", {len(missing)} stale"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
