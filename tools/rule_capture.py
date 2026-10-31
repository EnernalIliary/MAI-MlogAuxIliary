"""R1 — rule capture: turn "the user just corrected me" into a file that outlives the chat.

I have no weight memory. A correction that does not become a record is lost the moment the
session ends, and I will make the same mistake in three months. This tool is the smallest
thing that prevents that:

    python tools/rule_capture.py add --id reactor-link-name-proves-nothing \
        --claim "..." --why "..." --source user --ref m13856 --severity INFO \
        --counter-example tests/cases/reactor-guard-impact.mlog

    python tools/rule_capture.py lint          # schema + "counter example must not fire"
    python tools/rule_capture.py list

``lint`` is the part that actually earns its keep. A rule is only trustworthy if its
``counter_example`` fixture runs and produces **no** finding matching that rule. That is a
machine-checkable claim about the negative evidence, which is exactly the claim I get wrong
when a predicate is too wide. A rule with no counter example is reported as unguarded --
allowed, but visible.

Rule kinds (see rules/README.md):

    semantics   the game does X. Verified in source or by a user. Never downgraded by audit.
    heuristic   "this usually means a mistake". Statistical; R2 may downgrade it.
    negative    looks wrong but is correct. Kept so the tempting rule cannot come back.

Usage:
    python tools/rule_capture.py add --id ... --claim ... --why ... [--counter-example PATH]
    python tools/rule_capture.py lint [--verbose]
    python tools/rule_capture.py list
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mindustry-blueprint" / "tools"))

import mlogcheck as C  # noqa: E402

RULES_DIR = ROOT / "rules"
REQUIRED = ("id", "kind", "severity", "claim", "why", "source", "since", "match")
KINDS = {"semantics", "heuristic", "negative"}
SEVES = {"ERR", "WARN", "INFO", "SUSPECT", "SUPPRESSED"}
SOURCES = {"user", "source", "corpus", "reasoning"}


def load_rules() -> list[dict]:
    out = []
    for path in sorted(RULES_DIR.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            out.append({"_path": path, "_broken": str(exc)})
            continue
        rec["_path"] = path
        out.append(rec)
    return out


def matches(rec: dict, sev: str, msg: str) -> bool:
    m = rec.get("match") or {}
    if sev not in (m.get("sevs") or []):
        return False
    keys = [k.lower() for k in (m.get("msg_any") or [])]
    if not keys:
        return False
    low = msg.lower()
    return any(k in low for k in keys)


def cmd_add(args: argparse.Namespace) -> int:
    if args.id in {r.get("id") for r in load_rules()}:
        print(f"rule '{args.id}' already exists -- edit {RULES_DIR / (args.id + '.json')} instead")
        return 1
    rec = {
        "id": args.id,
        "kind": args.kind,
        "severity": args.severity,
        "claim": args.claim,
        "why": args.why,
        "source": args.source,
        "ref": args.ref,
        "since": args.since or date.today().isoformat(),
        "counter_example": args.counter_example,
        "match": {
            "sevs": [] if args.severity == "SUPPRESSED" else [args.severity],
            "msg_any": [s for s in (args.msg or [])],
        },
        "auto_downgrade": bool(args.auto_downgrade),
    }
    if args.line_regex:
        rec["line_regex"] = args.line_regex
    RULES_DIR.mkdir(exist_ok=True)
    path = RULES_DIR / f"{args.id}.json"
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")
    print("next: fill match.msg_any (the substring that identifies this finding) and run lint")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    rules = load_rules()
    if not rules:
        print(f"no rules in {RULES_DIR}")
        return 1
    print(f"{len(rules)} rule(s) in {RULES_DIR.relative_to(ROOT)}:")
    for r in sorted(rules, key=lambda x: (x.get("kind", ""), x.get("id", ""))):
        if r.get("_broken"):
            print(f"  !! {r['_path'].name}: {r['_broken']}")
            continue
        obs = r.get("observed") or {}
        seen = f"  observed {obs['hits']:,}x" if obs else ""
        guard = "" if r.get("counter_example") else "  (unguarded)"
        print(f"  {r['kind']:<9} {r['severity']:<10} {r['id']}{seen}{guard}")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    rules = load_rules()
    if not rules:
        print(f"no rules in {RULES_DIR}")
        return 1
    problems = 0
    guarded = unguarded = 0
    for rec in rules:
        where = rec["_path"].name
        if rec.get("_broken"):
            print(f"  !! {where}: not valid JSON -- {rec['_broken']}")
            problems += 1
            continue
        missing = [k for k in REQUIRED if not rec.get(k)]
        if missing:
            print(f"  !! {where}: missing {', '.join(missing)}")
            problems += 1
        if rec.get("kind") not in KINDS:
            print(f"  !! {where}: kind '{rec.get('kind')}' not in {sorted(KINDS)}")
            problems += 1
        if rec.get("severity") not in SEVES:
            print(f"  !! {where}: severity '{rec.get('severity')}' not in {sorted(SEVES)}")
            problems += 1
        if rec.get("source") not in SOURCES:
            print(f"  !! {where}: source '{rec.get('source')}' not in {sorted(SOURCES)}")
            problems += 1
        if rec.get("kind") == "semantics" and rec.get("auto_downgrade"):
            print(f"  !! {where}: a 'semantics' rule may not auto-downgrade "
                  "(that is a verdict, not a statistic)")
            problems += 1
        if not (rec.get("match") or {}).get("msg_any") and rec.get("severity") != "SUPPRESSED":
            print(f"  !! {where}: match.msg_any is empty, so R2 can never count this rule")
            problems += 1

        fixture = rec.get("counter_example")
        if not fixture:
            unguarded += 1
            if args.verbose:
                print(f"  ~  {where}: no counter_example")
            continue
        path = ROOT / fixture
        if not path.exists():
            print(f"  !! {where}: counter_example {fixture} does not exist")
            problems += 1
            continue
        try:
            findings = C.analyse(path.read_text(encoding="utf-8").splitlines(),
                                 [re.sub(r"\d+$", "", m) + "1" for m in
                                  re.findall(r"\b([a-z][a-z-]*\d+)\b", path.read_text(encoding="utf-8"))])
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {where}: counter_example {fixture} failed to analyse -- {exc}")
            problems += 1
            continue
        fired = [f for f in findings if matches(rec, f.sev, f.msg)]
        if fired:
            guarded += 1
            print(f"  !! {where}: counter_example {fixture} DOES fire this rule "
                  f"({len(fired)} finding(s), first: {fired[0].msg[:90]})")
            problems += 1
        else:
            guarded += 1
            if args.verbose:
                print(f"  ok {where}: counter_example {fixture} stays silent "
                      f"({len(findings)} other finding(s))")

    print(f"\n{len(rules)} rule(s): {guarded} checked against a counter example, "
          f"{unguarded} unguarded, {problems} problem(s)")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="write a new rule record")
    a.add_argument("--id", required=True, help="kebab-case, stable forever")
    a.add_argument("--claim", required=True, help="one sentence: what is true")
    a.add_argument("--why", required=True, help="the evidence -- source lines, numbers, or who said it")
    a.add_argument("--kind", default="semantics", choices=sorted(KINDS))
    a.add_argument("--severity", default="WARN", choices=sorted(SEVES))
    a.add_argument("--source", default="user", choices=sorted(SOURCES))
    a.add_argument("--ref", default="", help="message ref or URL, e.g. m13856")
    a.add_argument("--since", default="")
    a.add_argument("--counter-example", default="", help="fixture that must NOT fire")
    a.add_argument("--msg", action="append", help="substring identifying the finding (repeatable)")
    a.add_argument("--line-regex", default="")
    a.add_argument("--auto-downgrade", action="store_true")
    a.set_defaults(fn=cmd_add)

    b = sub.add_parser("lint", help="schema + counter-example check")
    b.add_argument("--verbose", action="store_true")
    b.set_defaults(fn=cmd_lint)

    c = sub.add_parser("list", help="one line per rule")
    c.set_defaults(fn=cmd_list)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
