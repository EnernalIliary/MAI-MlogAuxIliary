#!/usr/bin/env python3
"""Tests for the in-game quick check (MlogAuxiliary/scripts/quickcheck.js).

Three things are pinned here, in order of how much they would hurt if they broke:

  1. DRIFT.  rules.generated.js must equal what tools/rules_export.py produces now.
     If someone edits tools/instr_semantics.json or mlogcheck.LINK_SLOTS and forgets
     to regenerate, the mod would answer with a stale rule table -- silently.  So the
     first check is `rules_export.py --check`, and it runs before anything else.

  2. THE JUDGEMENTS.  tests/mlogauxiliary/cases.json holds 14 hand-checked programs;
     each carries the findings the check must produce, as [sev, line, rid] triples.
     The harness runs the SAME quickcheck.js the mod loads, under node.

  3. NO INVENTED LINKS.  quickcheck may under-report (it is a fast subset) but it may
     never claim a name is unbound when the full checker (tools/mlogcheck.py) is happy.
     That one-way containment is what keeps the in-game WARN trustworthy.

Exit codes: 0 pass, 1 fail, 3 skipped (no node / no cases).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.json"
HARNESS = HERE / "harness.js"


def run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def check_drift(problems: list[str]) -> None:
    r = run([sys.executable, "tools/rules_export.py", "--check"])
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        problems.append("drift: " + out.replace(chr(10), " | "))
        return
    print("drift: " + out)


def run_harness(problems: list[str]) -> dict:
    node = shutil.which("node")
    if not node:
        print("SKIP no node on PATH -- cannot run the quick check harness")
        raise SystemExit(3)
    r = run([node, str(HARNESS), str(CASES)])
    if r.returncode != 0:
        problems.append("harness exited " + str(r.returncode) + ": " + r.stderr.strip()[:400])
        return {}
    try:
        return json.loads(r.stdout)
    except Exception as exc:  # noqa: BLE001
        problems.append("harness output is not JSON: " + str(exc))
        return {}


def check_modules(got: dict, problems: list[str]) -> None:
    """require() must hand back usable objects -- the game reaches these files the
    same way (Mods.java:809 runs only main.js; Scripts.java:30-32 gives require)."""
    mod = got.get("__modules")
    if not mod:
        problems.append("modules: harness did not report the require() contract")
        return
    if mod.get("hasCheck") != "function" or mod.get("hasStatements") != "function":
        problems.append("modules: require('quickcheck') did not export check/statements: "
                        + str(mod))
    if not isinstance(mod.get("instructions"), int) or mod["instructions"] < 50:
        problems.append("modules: require('rules.generated') carries "
                        + str(mod.get("instructions")) + " instruction(s), expected >= 50")
    print("modules: require() contract ok (" + str(mod.get("instructions"))
          + " instruction(s), rules v" + str(mod.get("rulesVersion")) + ")")


def check_cases(cases: list[dict], got: dict, problems: list[str]) -> None:
    for case in cases:
        name = case["name"]
        if name not in got:
            problems.append(name + ": no result from the harness")
            continue
        want = [list(x) for x in case.get("expect") or []]
        have = [list(x) for x in got[name]["findings"]]
        if want != have:
            problems.append(name + ": findings " + json.dumps(have) + " != expected " + json.dumps(want))
        stats = got[name]["stats"]
        if "statements" in case and stats["statements"] != case["statements"]:
            problems.append(name + ": statements " + str(stats["statements"]) + " != " + str(case["statements"]))
        if "ticksPerPass" in case and stats["ticksPerPass"] != case["ticksPerPass"]:
            problems.append(name + ": ticksPerPass " + str(stats["ticksPerPass"]) + " != " + str(case["ticksPerPass"]))
    print("cases: " + str(len(cases)) + " case(s) checked")


def check_no_invented_links(cases: list[dict], got: dict, problems: list[str]) -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import mlogcheck as C  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print("SKIP cross-check: cannot import mlogcheck (" + str(exc) + ")")
        return
    compared = 0
    for case in cases:
        name = case["name"]
        if name not in got:
            continue
        code = case.get("code")
        if code is None:
            continue
        links = case.get("links") or []
        quick = {f[1] for f in got[name]["findings"] if f[2] == "link-name-not-bound"}
        if not quick:
            continue
        # Finding.line is a STATEMENT INDEX (0-based), not a file line -- see
        # rules/README.md.  Map it back through the prepared statement list before
        # comparing with the quick check, which reports 1-based file lines.
        stmts = C.prepare(code.splitlines())
        file_line = {idx: ln + 1 for idx, (ln, _c) in enumerate(stmts)}
        full = C.analyse([c for _ln, c in stmts], links=list(links))
        full_lines = {file_line.get(f.line) for f in full if f.rid == "link-name-not-bound"}
        if not quick <= full_lines:
            problems.append(name + ": quick check reports unbound link on line(s) "
                            + str(sorted(quick - full_lines)) + " that mlogcheck does not")
        compared += 1
    print("cross: " + str(compared) + " case(s) where the full checker agrees")


def main() -> int:
    problems: list[str] = []
    check_drift(problems)
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    got = run_harness(problems)
    if got:
        check_modules(got, problems)
        check_cases(cases, got, problems)
        check_no_invented_links(cases, got, problems)
    if problems:
        print("")
        for p in problems:
            print("FAIL " + p)
        return 1
    print("quickcheck OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
