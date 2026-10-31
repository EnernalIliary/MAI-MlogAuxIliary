"""Seed ``rules/*.json`` — one record per thing this project had to learn the hard way.

Why a directory of JSON instead of prose: a rule is only "learned" if it can be
re-run. Each record carries three things that prose cannot:

  * ``match``        — how to find its findings in a corpus (so R2 can count them),
  * ``counter_example`` — a fixture that must NOT fire (so it cannot silently widen),
  * ``source``/``ref``  — who said it and when (a rule with no source is a guess).

``kind`` separates the three kinds of knowledge that look alike in a report:

  ``semantics``  the game does X; verified in source or by the user. Not a style call.
  ``heuristic``  "this usually means a mistake" — a statistical claim, downgradable.
  ``negative``   something that LOOKS wrong but is correct. Recorded so the rule that
                 would have flagged it never comes back. These are the cheapest rules
                 to own and the easiest to lose.

Usage::

    python tools/rule_seed.py            # write rules/*.json (idempotent, keeps edits)
    python tools/rule_seed.py --force    # overwrite existing records
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

RULES_DIR = Path(__file__).resolve().parents[1] / "rules"

# A rule's ``line_regex`` is what makes R2's precision number mean something: it re-checks
# that the source line still looks like the thing the rule claims to detect. Only rules
# whose finding carries the offending LINE in its third field get one. Program-level
# findings (version gate, dead polarity, null guard) carry a token instead, so for those
# precision is honestly unmeasurable rather than falsely 100%.
LINE_RE = {
    "jump-to-jump-chain": r"^jump\b",
    "printflush-clears-buffer": r"^printflush\b",
    "unknown-op": r"^op\b",
    "unary-op-extra-operand": (
        r"^op\s+(floor|ceil|round|abs|sqrt|sin|cos|tan|asin|acos|atan|rand|logn|log10|not|flip|len)\b"
    ),
}

# fmt: off
SEED = [
    {
        "id": "arity-is-not-effective-params",
        "kind": "semantics",
        "severity": "SUPPRESSED",
        "claim": "Slots are not checked by count: `ucontrol move x y` reads a prefix of the "
                 "parser slot count (`arity`), and a short `op` line fills absent fields from "
                 "the statement defaults (LStatements.java:708-711), so neither is a defect.",
        "why": "LExecutor.java:344-352 -- `case move, stop, approach, pathfind` reads x,y and "
               "(only for `approach`) a radius. The `p1..p4` toNumber() calls happen before "
               "dispatch, so `uses` is always 1,2,3 even when nothing reads them. Reporting a "
               "missing/extra slot against `arity` flags correct community code. Same on the "
               "`op` side (2026-10-27, user asked for the WARN to go): `op add r 1`, `op add` and "
               "`op abs` all compile to a real OpI (LStatements.java:708-711 defaults; v160.7 jar: "
               "`op add r 1` -> OpI with r=1), so the old `given N args, expects M` warning was a "
               "false positive and is gone; an unknown operator NAME is still ERR (`unknown-op`).",
        "source": "user",
        "ref": "m14600",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-ok.mlog",
        "match": {"sevs": ["WARN", "INFO"], "msg_any": ["args, expects", "effective arity"]},
        "auto_downgrade": False,
    },
    {
        "id": "unary-op-extra-operand",
        "kind": "heuristic",
        "severity": "INFO",
        "claim": "`op floor a b c` / `op sin a b 90` write an operand a unary operator never "
                 "reads. Harmless at runtime; sometimes reveals a units mistake (sin of degrees).",
        "why": "LogicOp.java: unary ops take one operand; LParser discards the rest. Corpus: "
               "1,121 of these on 9 downloaded maps were 96% of all warnings, so this class "
               "must never outrank a real finding.",
        "source": "corpus",
        "ref": "m14600",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-slow.mlog",
        "match": {"sevs": ["INFO"], "msg_any": ["reads", "extra"], },
        "line_regex": r"^op\s+(floor|ceil|round|abs|sqrt|sin|cos|tan|asin|acos|atan|rand|logn|log10|not|flip|len)\b",
        "auto_downgrade": False,
    },
    {
        "id": "reactor-single-threshold-is-fine",
        "kind": "negative",
        "severity": "SUPPRESSED",
        "claim": "A thorium reactor guard with one threshold and no hysteresis does NOT "
                 "oscillate. Do not report missing hysteresis.",
        "why": "NuclearReactor.java:95-125 — with coolant present `heat -= heat` clears the "
               "whole heat in one tick (steady draw 0.02 liquid/tick). `control enabled` does "
               "not change coolant supply, so there is no feedback loop to oscillate: @heat is "
               "a 'starved last tick' detector, not a thermometer.",
        "source": "user",
        "ref": "m13630",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-ok.mlog",
        "match": {"sevs": [], "msg_any": ["hysteresis"]},
        "auto_downgrade": False,
    },
    {
        "id": "reactor-link-name-proves-nothing",
        "kind": "semantics",
        "severity": "INFO",
        "claim": "A link named `reactor1` may be a thorium reactor or an impact reactor. The "
                 "response-time budget only applies to thorium, so an unproven reactor guard is "
                 "a question, not a verdict.",
        "why": "Impact reactors are not NuclearReactor and have their own heat rules; the names "
               "are shared by users who reuse one template. WARN only when `@thorium-reactor` "
               "appears in the program.",
        "source": "user",
        "ref": "m13856",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-impact.mlog",
        "match": {"sevs": ["WARN", "INFO"], "msg_any": ["reactor guard", "checked once every"]},
        "auto_downgrade": False,
    },
    {
        "id": "reactor-guard-response-time",
        "kind": "heuristic",
        "severity": "WARN",
        "claim": "A thorium reactor control loop that checks @heat less often than the time it "
                 "takes to go from its threshold to 0.999 can explode between two checks.",
        "why": "heating=0.01/tick ⇒ 0→0.999 is 100 ticks at full fuel, 600 ticks at 5/30 fuel. "
               "The check interval is loop-body statements ÷ ipt. 11 of 51 distinct corpus "
               "programs exceed their own window.",
        "source": "source",
        "ref": "m13643",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-ok.mlog",
        "match": {"sevs": ["WARN"], "msg_any": ["reactor guard is too slow"]},
        "auto_downgrade": False,
    },
    {
        "id": "virus-logic",
        "kind": "semantics",
        "severity": "INFO",
        "claim": "`ucontrol build <x> <y> @micro-processor 0 @this` builds a processor whose "
                 "config is this program: self-replication.",
        "why": "Community calls it 病毒逻辑. It is a design, not an error — but it is the first "
               "line worth seeing when you open someone else's map. Corpus: 9,232 programs use "
               "`ucontrol build`, 9,151 of them replicate; only 2 distinct programs.",
        "source": "user",
        "ref": "m14126",
        "since": "2026-10-18",
        "counter_example": "tests/cases/virus-logic-clean.mlog",
        "match": {"sevs": ["INFO"], "msg_any": ["virus logic", "self-replication"]},
        "auto_downgrade": False,
    },
    {
        "id": "surplus-parameter-is-not-an-error",
        "kind": "semantics",
        "severity": "WARN",
        "claim": "A token sitting past the parameters an instruction reads is ignored at "
                 "runtime. Often a hidden signature — never an error.",
        "why": "Same mechanism as unary extra operands, but generalised to every instruction: "
               "`control config unloader4 null 0 0 0sensor` is not a bug in `control`, whose "
               "only read parameters are three.",
        "source": "user",
        "ref": "m07332",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-ok.mlog",
        "match": {"sevs": ["WARN"], "msg_any": ["parameter slot this instruction never reads"]},
        "auto_downgrade": False,
    },
    {
        "id": "noop-means-version-drift",
        "kind": "semantics",
        "severity": "ERR",
        "claim": "`noop` is what a statement becomes when the parser of the installed build "
                 "does not understand it. A program containing noop is usually a program from a "
                 "newer build, and the parts it does not understand do nothing.",
        "why": "InvalidStatement is produced at LParser.java:143-147 (parse error), :150-152 "
               "(privileged statement), :166-168 (mod parser), :171-173 (unknown mnemonic). "
               "Downgraded once to WARN and promoted back after the user pointed out that "
               "opening a newer map on an older client silently kills the program.",
        "source": "user",
        "ref": "m07961",
        "since": "2026-10-18",
        "counter_example": "tests/cases/gate-emod.mlog",
        "match": {"sevs": ["ERR", "WARN"], "msg_any": ["noop"]},
        "auto_downgrade": False,
    },
    {
        "id": "true-false-are-numbers",
        "kind": "semantics",
        "severity": "SUPPRESSED",
        "claim": "`true` and `false` are registered constants (1 and 0), not unassigned "
                 "variables. Never report them as undefined.",
        "why": "GlobalVars registers them via putEntry. Judging built-ins by LAssembler.var() "
               "instead of the registry produced 653 false warnings.",
        "source": "source",
        "ref": "m07790",
        "since": "2026-10-18",
        "counter_example": "tests/cases/labels-check.mlog",
        "match": {"sevs": [], "msg_any": ["`true`", "`false`"]},
        "auto_downgrade": False,
    },
    {
        "id": "printflush-clears-buffer",
        "kind": "semantics",
        "severity": "INFO",
        "claim": "`printflush <anything>` clears the print buffer unconditionally, so an "
                 "unquoted token there is a deliberate flush, not a typo.",
        "why": "LExecutor.java:1174-1178 — `exec.textBuffer.setLength(0)` sits outside the "
               "`if(target ...)` branch, so the clear happens even with no message block linked.",
        "source": "source",
        "ref": "m07990",
        "since": "2026-10-18",
        "counter_example": "tests/cases/virus-logic.mlog",
        "match": {"sevs": ["INFO"], "msg_any": ["clears the print buffer"]},
        "auto_downgrade": False,
    },
    {
        "id": "jump-to-jump-chain",
        "kind": "heuristic",
        "severity": "INFO",
        "claim": "A `jump` whose target is another `jump` is legal and common: it is how "
                 "hand-written state machines and `@counter` dispatch tables skip empty arms.",
        "why": "The game follows the chain one hop at a time; no cost beyond the extra hop. "
               "Reported only so a reader can tell a skip chain from a dispatch table.",
        "source": "corpus",
        "ref": "m14738",
        "since": "2026-10-18",
        "counter_example": "tests/cases/virus-logic-clean.mlog",
        "match": {"sevs": ["INFO"], "msg_any": ["lands on another unconditional jump"]},
        "auto_downgrade": False,
    },
    {
        "id": "version-gate",
        "kind": "semantics",
        "severity": "INFO",
        "claim": "A statement newer than the v7 baseline (build 146) needs a build to exist: "
                 "on anything older the line becomes noop and does nothing.",
        "why": "BUILDS.md records the floors: format 147, select/emod/ushr 150, bullet/getblock "
               "157, music 159. Silent at or below the 146 baseline by design — every program "
               "uses set/print/end and restating that is noise.",
        "source": "source",
        "ref": "m08356",
        "since": "2026-10-18",
        "counter_example": "tests/cases/labels-check.mlog",
        "match": {"sevs": ["INFO"], "msg_any": ["version gate: uses"]},
        "auto_downgrade": False,
    },
    {
        "id": "link-name-not-bound",
        "kind": "semantics",
        "severity": "WARN",
        "claim": "A name shaped like a block link (`pad1`, `reactor3`) that the processor does "
                 "not actually link to resolves to a variable that is never assigned.",
        "why": "LogicBlock binds link names from its own link table (LogicBlock.java), so a "
               "processor copied without its links keeps the code and loses the targets. "
               "Corpus: 87 blocks on my blueprint library.",
        "source": "source",
        "ref": "m14738",
        "since": "2026-10-18",
        "counter_example": "tests/cases/virus-logic-clean.mlog",
        "match": {"sevs": ["WARN"], "msg_any": ["shaped like a block link"]},
        "auto_downgrade": False,
    },
    {
        "id": "dead-polarity",
        "kind": "semantics",
        "severity": "SUSPECT",
        "claim": "`@dead` is 1 when the unit is dead AND when there is no unit at all, so a "
                 "loop that continues while `notEqual dead 1` also exits when the unit vanishes.",
        "why": "LAccess `dead` returns 1 for a null unit (`UnitComp` `!isValid() -> null` path "
               "yields 0, and `equal null 0` is true). The intent is usually 'continue while "
               "alive'; the code cannot tell 'died' from 'never had one'.",
        "source": "user",
        "ref": "m01190",
        "since": "2026-10-18",
        "counter_example": "tests/cases/labels-check.mlog",
        "match": {"sevs": ["SUSPECT"], "msg_any": ["holds @dead"]},
        "auto_downgrade": False,
    },
    {
        "id": "spin-deadlock",
        "kind": "semantics",
        "severity": "ERR",
        "claim": "A back edge guarded on `X @this` where the loop body binds or controls a "
                 "unit AFTER reading X can never exit: the claim makes the guard true forever.",
        "why": "UnitControlI.checkLogicAI sets `la.controller = this processor` on every "
               "`ubind`/`ucontrol`, so `@controller == @this` holds from the claim onward. "
               "Read-then-claim is the correct order (that is the anti-steal idiom) and must "
               "not be reported.",
        "source": "source",
        "ref": "m11039",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-ok.mlog",
        "match": {"sevs": ["ERR"], "msg_any": ["this loop can never leave"]},
        "auto_downgrade": False,
    },
    {
        "id": "undefined-var-read",
        "kind": "semantics",
        "severity": "ERR",
        "claim": "Reading a variable that is never assigned (and is not a link or built-in) "
                 "yields null. What happens next is decided by the SLOT: a numeric or boolean "
                 "slot reads null as 0/false (INFO), any other slot (content, unit, block, "
                 "link, or the payload of set/print) receives the null OBJECT (ERR).",
        "why": "The editor lists every referenced name, so authors sign their work with a bare "
               "identifier (`print by:NOSB(hghg`); counting those as errors produced 883 hard "
               "errors on a community pack, so signature and comparison cases stay INFO "
               "(print-only, printflush without a message block, a set destination never read, "
               "punctuation-only names; a null comparison is claimed by constant-null-guard). "
               "The rest splits on the slot because LExecutor reads number slots with numi() / "
               "bool() (null -> 0, LExecutor.java:634) but hands obj() to every other slot, "
               "which then either skips itself (instanceof guard) or writes null onward.",
        "source": "source",
        "ref": "m07644",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-ok.mlog",
        "match": {"sevs": ["ERR", "WARN", "INFO"],
                  "msg_any": ["is never assigned", "is mentioned (so it is declared",
                              "signature only"]},
        "auto_downgrade": False,
    },
    {
        "id": "jump-target-out-of-range",
        "kind": "semantics",
        "severity": "ERR",
        "claim": "A `jump` target outside `0..n-1` is a jump to a line that does not exist.",
        "why": "LParser resolves labels to statement indices and stores -1 for an unknown one; "
               "JumpI guards against -1, so the jump silently falls through instead of looping.",
        "source": "source",
        "ref": "m14738",
        "since": "2026-10-18",
        "counter_example": "tests/cases/labels-check.mlog",
        "match": {"sevs": ["ERR"], "msg_any": ["jump target", "out of range"]},
        "auto_downgrade": False,
    },
    {
        "id": "unknown-op",
        "kind": "semantics",
        "severity": "ERR",
        "claim": "An `op` whose operator name is not in the 45-operator table does not exist in "
                 "this build; at best it becomes noop.",
        "why": "LogicOp is a fixed enum; LAssembler looks the name up and produces "
                 "InvalidStatement on a miss. Also the drift detector for renamed operators "
                 "(the editor shows `not` for `flip`).",
        "source": "source",
        "ref": "m14750",
        "since": "2026-10-18",
        "counter_example": "tests/cases/virus-logic-clean.mlog",
        "match": {"sevs": ["ERR"], "msg_any": ["unknown op"]},
        "auto_downgrade": False,
    },
    {
        "id": "ulocate-flag-invalid",
        "kind": "semantics",
        "severity": "ERR",
        "claim": "`ulocate`'s second argument must be a real BlockFlag; `0` makes the whole "
                 "instruction invalid, including the `found` output.",
        "why": "LAssembler resolves it through `BlockFlag.valueOf`; a miss yields "
                 "InvalidStatement, so the line prints nothing and writes nothing.",
        "source": "source",
        "ref": "m14750",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-ok.mlog",
        "match": {"sevs": ["ERR"], "msg_any": ["must be a valid BlockFlag"]},
        "auto_downgrade": False,
    },
    {
        "id": "bool-param-constantly-zero",
        "kind": "heuristic",
        "severity": "WARN",
        "claim": "An unassigned variable in a boolean parameter slot is constantly 0 (false), "
                 "so the instruction never does the thing it looks like it does.",
        "why": "mlog has no bool type: `p1.bool()` is `num() != 0`, and an unset variable is 0. "
               "Harmless when it is a signature, fatal when it was meant to be a switch.",
        "source": "source",
        "ref": "m14750",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-ok.mlog",
        "match": {"sevs": ["WARN"], "msg_any": ["read as a boolean"]},
        "auto_downgrade": False,
    },
    {
        "id": "constant-null-guard",
        "kind": "heuristic",
        "severity": "WARN",
        "claim": "`equal X null` where X is never assigned is constant: an unset variable is 0 "
                 "and null reads as 0, so the branch always takes the same side.",
        "why": "Only `===` (strictEqual) compares object identity and can tell null from 0. "
               "Corpus: 166 occurrences; one made ~550 statements of a world processor dead.",
        "source": "source",
        "ref": "m07332",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-ok.mlog",
        "match": {"sevs": ["WARN"], "msg_any": ["this comparison is constant"]},
        "auto_downgrade": False,
    },
    {
        "id": "ubind-missing",
        "kind": "semantics",
        "severity": "ERR",
        "claim": "A `ucontrol`/`ulocate` without a preceding `ubind` operates on a null unit "
                 "and silently does nothing.",
        "why": "Ubinding is what sets `exec.unit`; every unit instruction reads it. In a shared "
               "processor the loop must rebind after the unit dies.",
        "source": "source",
        "ref": "m14750",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-ok.mlog",
        "match": {"sevs": ["ERR"], "msg_any": ["used but there is no"]},
        "auto_downgrade": False,
    },
    {
        "id": "cross-processor-slot-undeclared",
        "kind": "semantics",
        "severity": "WARN",
        "claim": "`read`/`write` of a named slot only works if the other processor declares "
                 "that variable; otherwise the read returns null and the write lands nowhere.",
        "why": "LogicBlock.read looks the name up in the target's own variable table; there is "
               "no dynamic symbol creation across processors.",
        "source": "source",
        "ref": "m08520",
        "since": "2026-10-18",
        "counter_example": "tests/cases/labels-check.mlog",
        "match": {"sevs": ["WARN"], "msg_any": ["cross-processor"]},
        "auto_downgrade": False,
    },
    {
        "id": "impact-reactor-cap-is-not-protection",
        "kind": "semantics",
        "severity": "WARN",
        "claim": "Cutting an impact reactor at a heat threshold protects nothing: on an impact "
                 "reactor `@heat` is warmup and power output, not a countdown to a meltdown.",
        "why": "Only NuclearReactor heats toward 0.999 and kills itself. A template reused for "
               "both reactor types produces a loop that looks protective and does not protect.",
        "source": "source",
        "ref": "m13856",
        "since": "2026-10-18",
        "counter_example": "tests/cases/reactor-guard-slow.mlog",
        "match": {"sevs": ["WARN"], "msg_any": ["capping an impact reactor"]},
        "auto_downgrade": False,
    },
]
# fmt: on


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="overwrite records that already exist")
    args = ap.parse_args(argv)

    RULES_DIR.mkdir(exist_ok=True)
    written = kept = 0
    for rec in SEED:
        if rec["id"] in LINE_RE:
            rec.setdefault("line_regex", LINE_RE[rec["id"]])
        path = RULES_DIR / f"{rec['id']}.json"
        if path.exists() and not args.force:
            kept += 1
            continue
        path.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written += 1
    print(f"rules: {written} written, {kept} kept ({len(SEED)} in seed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
