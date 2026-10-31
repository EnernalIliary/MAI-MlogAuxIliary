#!/usr/bin/env python3
"""mlogagent -- one command, every analysis, on one logic program.

`mlog.py` gives you one view at a time (check / ir / values / pack). Real
triage is always the same sequence: does it even parse, does it have a bug,
is anything dead, is anything provably null, is a packed field too small.
This runs that sequence and ends with a verdict line per program, so you do
not have to read five separate reports to decide where to look first.

    python mlogagent.py report  logic.mlog
    python mlogagent.py report  --msch "blueprints/无线超.msch"
    python mlogagent.py report  --msch dir/*.msch --brief
    python mlogagent.py report  logic.mlog --only ir,values
    python mlogagent.py report  logic.mlog --skip cost --trace n
    python mlogagent.py info                       # toolchain + data inventory
    python mlogagent.py info --verify              # check MANIFEST.sha256

Exit status
-----------
1 if `check` found an ERR or SUSPECT finding, else 0. The advisory sections
(`ir`, `values`, `pack`) never change the exit status: they report things a
human must judge, and `ir` in particular reports ~0.65 dead stores per block
across the real corpus, only some of which are mistakes.

Boundaries (measured, not guessed -- see SKILL.md)
--------------------------------------------------
* Nothing here simulates the game. There is no second implementation of mlog
  semantics to check against, so every claim traces back to a specific line of
  `LExecutor.java` / `LVar.java` / `LParser.java`, or it is not claimed.
* `1v1.msch`-style runtime trickery (unit state, `@unit` swaps) is modelled
  as "unknown", never as a value.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import mlog as M            # noqa: E402  (loads cmd_index/item_ids/... from HERE)
import msch                 # noqa: E402

_check, _ir, _val, _pk = M._check, M._ir, M._val, M._pk
_cx = M._cx

SECTION_HELP = {
    "check": "static findings (the only section that can fail the exit code)",
    "cost": "instruction budget: worst-case path, loop bounds, `wait` ticks",
    "ir": "CFG + liveness: unreachable code, dead stores (four tiers)",
    "values": "abstract values: branches decided at compile time, provable nulls",
    "pack": "packed configs: field layout and how many ids fit the low field",
    "cross": "cross-processor read/write: what the reader really gets",
}
ALL_SECTIONS = list(SECTION_HELP)


# --------------------------------------------------------------------------- #
# targets: a .mlog file is one target, a .msch is one target per logic block
# --------------------------------------------------------------------------- #

def _add_blueprint(out, p: str) -> None:
    # Accepts both the binary .msch the game writes and the base64 text you copy
    # off a blueprint site; mlog._load_schematic sniffs the magic.
    sch = M._load_schematic(p)
    for t in sch.logic_tiles():
        assert t.logic is not None
        out.append((f"{p} [{t.block} @({t.x},{t.y})]",
                    t.logic.code.split("\n"),
                    [l.name for l in t.logic.links]))


def _targets(args) -> list[tuple[str, list[str], list[str] | None]]:
    out: list[tuple[str, list[str], list[str] | None]] = []
        # A positional argument may be either kind; dispatch on the suffix,
    # otherwise a binary blueprint would be decoded as utf-8 text.
    for p in args.files:
        if p.lower().endswith(".msch"):
            _add_blueprint(out, p)
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                out.append((p, fh.read().split("\n"), None))
        except UnicodeDecodeError:
            raise SystemExit(
                f"!! {p}: not UTF-8 text.  If this is a blueprint (.msch), the "
                f"suffix decides the parser -- rename it or pass it via --msch.")
    for p in args.msch:
        _add_blueprint(out, p)
    return out


# --------------------------------------------------------------------------- #
# measurements -- computed for the verdict line; the printing is delegated so
# each section keeps exactly the format `mlog.py` produces (one source of truth)
# --------------------------------------------------------------------------- #

def _measure(lines: list[str], links: list[str] | None) -> dict:
    findings = _check.analyse(lines, links)
    prog = _ir.parse(lines)
    st = _val.analyze(prog)
    sites = _pk.find_pack_sites(prog)
    tiers: dict[str, int] = {}
    for i, v, kind in prog.classify_dead_stores():
        if M._ANNOTATION.match(v):
            continue
        tiers[M._ir_tier(prog, i, v, kind)] = \
            tiers.get(M._ir_tier(prog, i, v, kind), 0) + 1
    return {
        "stmts": len(prog),
        "unknown": sorted(prog.unknown_mnemonics),
        "errs": len([f for f in findings if f.sev in ("ERR", "SUSPECT")]),
        "warns": len([f for f in findings if f.sev == "WARN"]),
        "findings": findings,
        "prog": prog,
        "dead": len(prog.dead()),
        "reported": tiers.get("reported", 0),
        "hidden": sum(v for k, v in tiers.items() if k != "reported"),
        "branches": len(st.branches),
        "nulls": len(st.nulls),
        "rounds": st.rounds,
        "stable": st.stable,
        "packs": len(sites),
        "pack_notes": sum(len(s.overlaps) for s in sites),
        "pack_ok": sum(len(s.masked) for s in sites),
        "build": _check.build_floor(_check.prepare(lines))[0]
        if _check.build_floor(_check.prepare(lines))[0] > _check.BASELINE_BUILD else 0,
    }


def _verdict(m: dict) -> str:
    bits = [f"check {m['errs']}E/{m['warns']}W",
            f"dead-code {m['dead']}",
            f"dead-store {m['reported']}(+{m['hidden']} hidden)",
            f"branches {m['branches']}",
            f"nulls {m['nulls']}",
            f"packs {m['packs']}"]
    if m.get("build"):
        bits.append(f"build >={m['build']}")
    return "  | ".join(bits)


def _first_stop(m: dict) -> str:
    """What a human should open first, in the order the evidence gets weaker."""
    if m["errs"]:
        return "check 报错 -- 先看这一步,后面几节都可能是它的后果"
    if m["reported"]:
        return "死存储 -- 赋值后没人读,通常是半交换写反 / 跳转落点错 / 守卫没人用"
    if m["dead"]:
        return "不可达代码 -- 有一批语句永远执行不到"
    if m["branches"]:
        return "常量分支 -- 某个 if 的判断在编译期就定了,那条路白写"
    if m["nulls"]:
        return "可证 null -- 某条语句读的变量此刻必定是 null"
    if m["packs"]:
        return "打包配置 -- 确认低位字段装得下所有 id"
    if m["unknown"]:
        return "未知助记符 -- 先确认拼写,或它来自非原版"
    return "没问题 -- 这份逻辑的静态层面是干净的"


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def cmd_report(args) -> int:
    only = [s.strip() for s in (args.only or "").split(",") if s.strip()]
    skip = [s.strip() for s in (args.skip or "").split(",") if s.strip()]
    bad = [s for s in only + skip if s not in ALL_SECTIONS]
    if bad:
        print(f"unknown section(s): {', '.join(bad)}\n"
              f"  choose from: {', '.join(ALL_SECTIONS)}", file=sys.stderr)
        return 2
    wanted = [s for s in ALL_SECTIONS
              if (not only or s in only) and s not in skip]

    targets = _targets(args)
    if not targets:
        print("no logic found (give a .mlog file, or --msch for a blueprint)",
              file=sys.stderr)
        return 2

    rc = 0
    verdicts: list[tuple[str, dict]] = []
    for label, lines, links in targets:
        if not any(l.strip() for l in lines):
            continue
        m = _measure(lines, links)
        verdicts.append((label, m))
        rc |= 1 if m["errs"] else 0
        if args.brief:
            continue

        print(f"\n{'=' * 74}\n{label}\n{'=' * 74}")
        if "check" in wanted:
            M._report(label, m["findings"])
        if "cost" in wanted:
            print("--- cost " + "-" * 66)
            M._cost(lines, links)
        if "ir" in wanted:
            print("--- ir " + "-" * 68)
            M._ir_report(label, lines, args.all)
        if "values" in wanted:
            print("--- values " + "-" * 64)
            M._val_report(label, lines, list(args.trace or []))
        if "pack" in wanted:
            print("--- pack " + "-" * 66)
            print(_pk.format_report(m["prog"], item_count=len(M.ITEM_IDS) or None,
                                    label=label))

    if not verdicts:
        print("every target was empty", file=sys.stderr)
        return 2

    if "cross" in wanted and not args.brief:
        # Schematic-level, not per block: the value a `read` sees comes from
        # another program, so it can only be resolved for the whole blueprint.
        for p in list(getattr(args, "files", None) or []) + \
                list(getattr(args, "msch", None) or []):
            if not str(p).lower().endswith(".msch"):
                continue
            try:
                sch = msch.parse_file(p)
            except Exception as exc:  # noqa: BLE001
                print(f"!! {p}: {exc}", file=sys.stderr)
                continue
            print("--- cross " + "-" * 65)
            print(_cx.format_report(
                _cx.analyze_units(_cx.units_from_schematic(sch)), str(p)))

    print(f"\n{'=' * 74}\nVERDICT\n{'=' * 74}")
    width = max(len(l) for l, _ in verdicts)
    for label, m in verdicts:
        print(f"  {label:<{width}}  {_verdict(m)}")
    print()
    for label, m in verdicts:
        print(f"  {label}\n    -> {_first_stop(m)}")
    return rc


# --------------------------------------------------------------------------- #
# info
# --------------------------------------------------------------------------- #

_DATA = ["wiki_block_ids.json", "block_sizes.json", "cmd_index.json",
         "item_ids.json", "instr_semantics.json", "content_names.json",
         "unit_attrs.json", "block_attrs.json", "sensor_attrs.json", "status_names.json"]
# aigenerated.py is a runtime dependency of mlog.py (:87 imports it) even
# though nothing else here imports it; leaving it out shipped a stale copy.
# rule_ids.py is imported by mlogcheck.py to name findings; a copy without it
# still analyses, but every Finding.rid comes back None.
_MODULES = ["mlog.py", "aigenerated.py", "mlogcheck.py", "mlogir.py",
            "mlogval.py", "mlogpack.py", "mlogcross.py", "msch.py",
            "mschgen.py", "mlogagent.py", "rule_ids.py"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cmd_info(args) -> int:
    print(f"mlogagent at {HERE}")
    print(f"python     : {sys.version.split()[0]}")
    print(f"modules    : {len(_MODULES)}")
    missing = [n for n in _MODULES if not (HERE / n).exists()]
    print(f"  missing  : {', '.join(missing) or 'none'}")
    print(f"data files : {len(_DATA)}")
    for n in _DATA:
        p = HERE / n
        print(f"  {n:<24} {'-' if not p.exists() else f'{p.stat().st_size:>7} B'}")
    sem = M.SEMANTICS or {}
    print(f"  blocks   : {len(M.BLOCK_IDS)}   items: {len(M.ITEM_IDS)}   "
          f"cmd docs: {len(M.CMD_INDEX)}   "
          f"instructions: {len(sem.get('instructions') or {})}   "
          f"ops: {len(sem.get('ops') or {})}")
    print(f"  builtin @vars: {len(sem.get('builtin_readonly_vars') or [])}   "
          f"source: {sem.get('_source', '?')} @ {sem.get('git_head', '?')}")
    dis = ((sem.get("docs_crosscheck") or {}).get("disagreements") or [])
    if dis:
        print(f"  docs wrong : {len(dis)} instruction(s) documented in a "
              f"different parameter order than the source")
        print("               run: mlog.py explain <mnemonic>  to see each one")

    man = HERE / "MANIFEST.sha256"
    if args.verify:
        print()
        if not man.exists():
            print("no MANIFEST.sha256 beside this script -- nothing to verify")
            return 0
        bad = 0
        for line in man.read_text("utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            want, name = line.split(None, 1)
            p = HERE / name.strip()
            got = _sha(p) if p.exists() else "-"
            if got != want:
                bad += 1
                print(f"  MISMATCH {name.strip()}")
        print("manifest  : all files match" if not bad
              else f"manifest  : {bad} file(s) differ from MANIFEST.sha256")
        return 1 if bad else 0
    return 0


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="mlogagent", description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="sections: " + "; ".join(f"{k} = {v}" for k, v in
                                        SECTION_HELP.items()))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("report", help="run every analysis, end with a verdict")
    p.add_argument("files", nargs="*", help=".mlog source file(s)")
    p.add_argument("--msch", action="append", default=[],
                   help=".msch blueprint (repeatable); each logic block is a target")
    p.add_argument("--only", help="comma list: " + ",".join(ALL_SECTIONS))
    p.add_argument("--skip", help="comma list to leave out")
    p.add_argument("--brief", action="store_true",
                   help="verdict table only, no section bodies")
    p.add_argument("--all", action="store_true",
                   help="ir: also list forced output slots and literal declarations")
    p.add_argument("--trace", action="append", default=[],
                   help="values: print the abstract value after every write (repeatable)")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("info", help="toolchain + data inventory")
    p.add_argument("--verify", action="store_true",
                   help="recompute SHA256 and compare with MANIFEST.sha256")
    p.set_defaults(fn=cmd_info)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
