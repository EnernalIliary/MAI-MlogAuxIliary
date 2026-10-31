#!/usr/bin/env python3
"""Inventory the world symbols an mlog program needs.

Language-level instructions run fine on an empty map, but anything that reads
the world -- sensor attributes, units, linked blocks, clock globals -- has to be
*scenario data*. This tool scans programs and reports exactly which symbols they
touch, so a scenario can be written to cover them instead of guessed.

Usage:
  python tools/mlog_needs.py out/*.mlog
  python tools/mlog_needs.py out/probes/*.msch --json out/needs.json
  python tools/mlog_needs.py <dir> --brief

For a .msch the schematic's link table is used to resolve each processing
block's link names to real block types, so the report says "message1 is a
message block", not just "message1 is some variable". A plain .mlog has no link
table, so block-ish arguments are reported as candidates.

Output is JSON: one record per processor plus an aggregate summary. A symbol
counts once per use; block links are counted per instruction.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mlogir  # noqa: E402

# Globals that exist because the game has a world/clock. The engine also has
# @counter/@ipt/@this, which this report records but a scenario does not provide.
WORLD_GLOBALS = {
    "@time", "@tick", "@second", "@waveNumber", "@waveTime",
    "@unitCount", "@itemCount", "@liquidCount", "@mapw", "@maph",
    "@x", "@y", "@thisx", "@thisy", "@links", "@unit", "@this", "@counter", "@ipt",
}
ENGINE_GLOBALS = {"@counter", "@ipt", "@this", "@unit", "@links"}

MEMORY_RE = re.compile(r"^(cell|bank)\d+$")
KEYS = ("sensors", "ubind", "ucontrol", "ulocate", "radar_filters",
        "radar_blocks", "blocks", "links", "globals", "atoms")


def _blank(name: str) -> dict:
    rec = {"name": name, "instructions": 0, "error": None}
    for k in KEYS:
        rec[k] = {}
    return rec


def _record(name: str, code: str, links: dict[str, str] | None = None) -> dict:
    """Scan one program. `links` maps link name -> block type when known."""
    rec = _blank(name)
    try:
        prog = mlogir.parse(code)
    except Exception as exc:  # a broken program should not abort the scan
        rec["error"] = "%s: %s" % (type(exc).__name__, exc)
        return rec
    links = links or {}
    rec["instructions"] = len(prog.stmts)

    def block_of(tok: mlogir.Tok | None) -> str | None:
        """Resolve a block-ish argument to a type: link table first, else None."""
        if tok is None:
            return None
        return links.get(tok.text)

    for st in prog.stmts:
        m = st.mnemonic
        args = st.args

        if m == "sensor" and len(args) >= 3:
            prop = args[2]
            if prop.kind == "builtin":  # attributes/content only, not variables
                rec["sensors"][prop.text] = rec["sensors"].get(prop.text, 0) + 1
        elif m == "ubind" and args:
            tok = args[0]
            if tok.kind in ("builtin", "str"):  # unit types are content literals
                rec["ubind"][tok.text] = rec["ubind"].get(tok.text, 0) + 1
        elif m == "ucontrol" and args:
            rec["ucontrol"][args[0].text] = rec["ucontrol"].get(args[0].text, 0) + 1
        elif m == "ulocate" and args:
            rec["ulocate"][args[0].text] = rec["ulocate"].get(args[0].text, 0) + 1
        elif m == "radar":
            for tok in args[:3]:
                rec["radar_filters"][tok.text] = rec["radar_filters"].get(tok.text, 0) + 1
            if len(args) > 4:
                typ = block_of(args[4])
                key = "%s(%s)" % (args[4].text, typ) if typ else args[4].text
                rec["radar_blocks"][key] = rec["radar_blocks"].get(key, 0) + 1
        elif m == "printflush" and args:
            typ = block_of(args[0])
            key = "%s(%s)" % (args[0].text, typ) if typ else args[0].text
            rec["blocks"][key] = rec["blocks"].get(key, 0) + 1
        elif m == "control" and len(args) >= 2:
            typ = block_of(args[1])
            key = "%s(%s)" % (args[1].text, typ) if typ else args[1].text
            rec["blocks"][key] = rec["blocks"].get(key, 0) + 1
        elif m in ("read", "write") and len(args) >= 2:
            target = args[1]
            if target.text and not MEMORY_RE.match(target.text):
                typ = links.get(target.text)
                key = "%s(%s)" % (target.text, typ) if typ else target.text
                rec["links"][key] = rec["links"].get(key, 0) + 1

        for tok in args:
            if tok.kind == "builtin" and tok.text in WORLD_GLOBALS:
                rec["globals"][tok.text] = rec["globals"].get(tok.text, 0) + 1
            elif tok.kind == "builtin":
                rec["atoms"][tok.text] = rec["atoms"].get(tok.text, 0) + 1
    return rec


def scan_file(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".msch":
        import msch
        schem = msch.parse_file(str(path))
        by_pos = {(t.x, t.y): t.block for t in schem.tiles}
        out = []
        for t in schem.tiles:
            logic = getattr(t, "logic", None)
            if logic is None:
                continue
            link_types = {}
            for lk in logic.links:
                typ = by_pos.get((lk.x, lk.y))
                link_types[lk.name] = typ or "?"
            label = "%s@%d,%d" % (t.block, t.x, t.y)
            out.append(_record(label, logic.code, link_types))
        return out
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return [_record(path.name, text)]


def _merge(into: dict, add: dict) -> None:
    for k, v in add.items():
        into[k] = into.get(k, 0) + v


def _markdown(doc: dict) -> str:
    lines = ["# mlog world needs", "",
             "files: %d, programs: %d, errors: %d" % (doc["files"], doc["programs"], doc["errors"]),
             ""]
    titles = [("ubind", "Unit types bound (`ubind`)"),
              ("sensors", "Sensor attributes read"),
              ("ucontrol", "Unit control subcommands"),
              ("ulocate", "`ulocate` targets"),
              ("blocks", "Blocks driven (`printflush`/`control`)"),
              ("links", "Cross-processor links (`read`/`write`)"),
              ("globals", "World/engine globals")]
    for key, title in titles:
        items = sorted(doc["summary"][key].items(), key=lambda kv: (-kv[1], kv[0]))
        lines.append("## %s" % title)
        lines.append("")
        if not items:
            lines.append("(none)")
        else:
            lines.append("| symbol | uses |")
            lines.append("|---|---|")
            for k, v in items:
                lines.append("| `%s` | %d |" % (k, v))
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Inventory world symbols used by mlog programs")
    ap.add_argument("paths", nargs="+", help="files/directories (.mlog, .msch)")
    ap.add_argument("--json", metavar="FILE", help="write full JSON here")
    ap.add_argument("--md", metavar="FILE", help="write a markdown summary here")
    ap.add_argument("--brief", action="store_true", help="print only aggregate counts")
    ap.add_argument("--stride", type=int, default=1,
                    help="sample every Nth file after sorting (1=all); use on 10k+ corpora")
    ap.add_argument("--limit", type=int, default=0, help="keep only the first N files after --stride")
    ap.add_argument("--no-detail", action="store_true",
                    help="omit programs_detail from the JSON (per-program blobs dominate the "
                         "size on large corpora: 44k programs -> hundreds of MB)")
    args = ap.parse_args(argv)

    files: list[Path] = []
    for p in args.paths:
        q = Path(p)
        if q.is_dir():
            files.extend(sorted(q.rglob("*.mlog")))
            files.extend(sorted(q.rglob("*.msch")))
        elif q.is_file():
            files.append(q)
        else:
            print("missing input: %s" % p, file=sys.stderr)
            return 1

    total_files = len(files)
    if args.stride > 1:
        files = files[::args.stride]
    if args.limit:
        files = files[:args.limit]

    programs = []
    for idx, f in enumerate(files):
        if idx and idx % 2000 == 0:
            # progress on stderr: on a 40k-file corpus this loop runs for minutes
            print("  %d/%d files" % (idx, len(files)), file=sys.stderr, flush=True)
        try:
            recs = scan_file(f)
        except Exception as exc:  # unreadable/corrupt file: report, keep scanning
            rec = _blank(f.name)
            rec["error"] = "%s: %s" % (type(exc).__name__, exc)
            recs = [rec]
        for rec in recs:
            rec["file"] = str(f)
            programs.append(rec)

    summary = {k: {} for k in KEYS}
    for rec in programs:
        for key in summary:
            _merge(summary[key], rec[key])

    doc = {"files": len(files), "programs": len(programs), "files_total": total_files,
           "stride": args.stride, "limit": args.limit,
           "errors": sum(1 for r in programs if r["error"]),
           "summary": summary}
    if not args.no_detail:
        doc["programs_detail"] = programs
    else:
        # keep only the failures: with --no-detail they are the only records a
        # later reader can act on, and they are rare.
        doc["errors_detail"] = [r for r in programs if r["error"]]
    if args.json:
        Path(args.json).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    if args.md:
        Path(args.md).write_text(_markdown(doc), encoding="utf-8")

    if args.brief:
        print("files=%d programs=%d errors=%d%s" % (
            doc["files"], doc["programs"], doc["errors"],
            "" if total_files == len(files) else "   (sampled: %d of %d files)" % (len(files), total_files)))
        for key in KEYS:
            items = sorted(summary[key].items(), key=lambda kv: (-kv[1], kv[0]))
            if key == "atoms":
                continue
            print("%-13s %s" % (key, ", ".join("%s:%d" % kv for kv in items[:20]) or "-"))
    else:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
