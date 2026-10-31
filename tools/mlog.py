#!/usr/bin/env python
"""
mlog.py -- Mindustry blueprint (.msch) toolkit: read, write, validate.

One entry point for everything this skill does.

Usage
-----
  python mlog.py parse  <file.msch>            # dump a blueprint's blocks + logic
  python mlog.py text   <file.msch...> [--plain] # what the message block (信息板) says
  python mlog.py gen    <logic.mlog> [opts]    # build a .msch (or paste code)
  python mlog.py check  <logic.mlog | --msch F> # static analysis of mlog
  python mlog.py ir     <logic.mlog | --msch F> # CFG: unreachable code, dead stores
                                               #   (--all also shows forced output
                                               #    slots and literal declarations)
  python mlog.py values <logic.mlog | --msch F> # abstract values: constant branches,
                                               #   provable nulls (--trace VAR)
  python mlog.py pack   <logic.mlog | --msch F> # packed configs: field layout + how
python mlog.py cross  <blueprint.msch>        # cross-processor read/write: what the reader gets
                                               #   many ids still fit the low field
  python mlog.py id     [name]                 # block content ids / sizes
python mlog.py sensor [pattern]         # what @property names sensor can read
                                         #   e.g. `sensor heat`, `sensor --settable`
  python mlog.py item   [name] [--unpack N]    # item content ids / packed configs
  python mlog.py unit   [name]                 # unit stats: speed, hp, payload
  python mlog.py block  [name]                 # recipe, capacity, power
  python mlog.py find   <words>                # corpus blueprints that solve this job
python mlog.py style <words>           # how the corpus writes it: naming, shape, cost
  python mlog.py map    <save.msav>            # every processor in a save:
                                               #   code + links, then the
                                               #   same checks as `check`
  python mlog.py cmd    [pattern]              # mlog instruction reference
  python mlog.py explain <mnemonic> [sub]      # one instruction: syntax + which
                                               #   positions are read / written
  python mlog.py selftest                      # round-trip every real blueprint

Examples
--------
  # blueprint -> readable dump (logic extracted verbatim)
  python mlog.py parse "schem/无线超.msch"

  # emit a paste-ready blueprint code, with the target block placed
  # (coordinates are offsets from the processor; copy straight from the map
  #  and add --abs instead)
  python mlog.py gen logic.mlog --name "示例" \
      --link dome1@2,1 --with-block overdrive-dome@2,1=248

  # check logic before generating
  python mlog.py check logic.mlog
  python mlog.py check --msch out.msch

  # "which argument of this instruction is the output?"
  python mlog.py explain ulocate
  python mlog.py explain op add

Notes
-----
* A schematic stores a block's CENTER tile, not its corner (verified 2 ways).
* Logic is stored as PLAIN TEXT inside the blueprint -- no decompilation needed.
* Content ids are NOT serialized in a schematic; the game resolves blocks by
  NAME on load. Ids matter only for building content values inside a config.
"""
from __future__ import annotations

import argparse
import base64
import collections
import importlib
import json
import os
import re
import sys
import textwrap
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mlogcheck as _check          # noqa: E402
import mlogir as _ir                # noqa: E402
import mlogpack as _pk              # noqa: E402
import mlogcross as _cx
import mlogval as _val              # noqa: E402
import msch                          # noqa: E402
import mschgen as _gen              # noqa: E402
import aigenerated as _sig          # noqa: E402

# Windows consoles default to cp936; every report below is UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                     # pragma: no cover - non-TTY pipes
    pass


def _load_json(name: str) -> dict:
    p = os.path.join(HERE, name)
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


BLOCK_IDS = _load_json("wiki_block_ids.json").get("blocks", {})
BLOCK_SIZES = _load_json("block_sizes.json")
CMD_INDEX = _load_json("cmd_index.json")
ITEM_IDS = _load_json("item_ids.json").get("items", {})
SENSOR_ATTRS = _load_json("sensor_attrs.json").get("access", {})
ITEM_NAME_TO_ID = {v: int(k) for k, v in ITEM_IDS.items()}
UNIT_ATTRS = _load_json("unit_attrs.json")
BLOCK_ATTRS = _load_json("block_attrs.json")


# --------------------------------------------------------------------------- #
# loading: a blueprint can arrive in two shapes with the same suffix
# --------------------------------------------------------------------------- #

def _load_schematic(path: str):
    """Read a blueprint from either the binary .msch the game writes or the
    base64 text you copy off a blueprint site / the in-game clipboard. The
    magic tells them apart, so the suffix does not have to be trusted."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:4] == b"msch":
        return msch.parse_bytes(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(
            f"!! {path}: not a .msch binary and not base64 text ({exc})")
    try:
        return msch.parse_base64(text)
    except Exception as exc:                      # noqa: BLE001 - report, don't trace
        raise SystemExit(f"!! {path}: not a .msch binary and not a base64 "
                         f"blueprint ({exc})")


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #

def cmd_parse(args) -> int:
    sch = _load_schematic(args.file)
    print(f"name  : {sch.name}")
    if sch.description:
        print(f"desc  : {sch.description}")
    if sch.labels:
        print(f"labels: {sch.labels}")
    print(f"size  : {sch.width} x {sch.height}")
    print(f"tiles : {len(sch.tiles)}")
    print()

    for t in sch.tiles:
        sz = _gen.block_size(t.block)
        half = (sz - 1) // 2
        span = (f"({t.x-half},{t.y-half})..({t.x-half+sz-1},{t.y-half+sz-1})"
                if sz > 1 else f"({t.x},{t.y})")
        extra = ""
        if t.logic:
            stmts = len(t.logic.statement_lines())
            links = ", ".join(f"{l.name}@({l.x},{l.y})" for l in t.logic.links)
            extra = f"  [{stmts} stmts]" + (f"  links: {links}" if links else "")
        print(f"  {t.block:22} center@({t.x},{t.y}) span {span}{extra}")

    logic = sch.logic_tiles()

    # Message blocks (信息板) are where authors explain what the blueprint is
    # for -- far more often than in the description tag (360 of 626 files here
    # carry one, versus 92 with a non-empty description). Printing it next to
    # the block list is usually what makes a dump readable at all.
    if not getattr(args, "no_text", False):
        texts = [t for t in sch.tiles
                 if t.block in _STRING_BLOCKS and isinstance(t.config, str)
                 and t.config.strip()]
        if texts:
            print()
            print("=" * 70)
            for t in texts:
                raw = t.config or ""
                body = _plain(raw) if getattr(args, "plain", False) else raw
                if getattr(args, "strip", False):
                    body = "\n".join(l for l in body.split("\n") if l.strip())
                icons = len(_PUA.findall(raw))
                head = f"--- {t.block} @({t.x},{t.y})"
                if icons:
                    head += f"  [{icons} icon(s) in the private-use area]"
                print(head)
                print(body)
                print()

    if logic:
        print()
        print("=" * 70)
        for t in logic:
            assert t.logic is not None
            print(f"--- {t.block} @({t.x},{t.y}) ---")
            print(t.logic.code.rstrip("\n"))
            print()
    return 0


# --------------------------------------------------------------------------- #
# gen
# --------------------------------------------------------------------------- #

def cmd_gen(args) -> int:
    if args.logic:
        code = args.logic
    elif args.source:
        # utf-8-sig: PowerShell's Set-Content writes a BOM, and a BOM-prefixed
        # first line compiles to a Noop (the statement silently disappears).
        with open(args.source, encoding="utf-8-sig") as fh:
            code = fh.read()
    else:
        code = sys.stdin.read()

    if not args.no_sign:
        # auto-sign what we are about to emit: an AI-written program should not
        # depend on a human remembering a second command.
        if args.source:
            try:
                msg = _sig.sign(Path(args.source), args.author, force=False)
            except _sig.SignError:
                msg = _sig.sign(Path(args.source), args.author, force=True)
            with open(args.source, encoding="utf-8-sig") as fh:
                code = fh.read()
            print(f"# {msg}", file=sys.stderr)
        else:
            try:
                code, pid = _sig.sign_text(code, args.author)
            except _sig.SignError:
                code, pid = _sig.sign_text(code, args.author, force=True)
            print(f"# signed id={pid} author={args.author}", file=sys.stderr)

    try:
        px, py = (int(v) for v in args.at.split(","))
    except ValueError:
        print("--at must look like x,y", file=sys.stderr)
        return 2

    try:
        links = [_gen.parse_link(s) for s in args.link]
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    def _to_rel(dx: int, dy: int) -> tuple[int, int]:
        # Absolute map tiles (the frame --at uses) vs offsets from the
        # processor. Copying coordinates straight out of a blueprint or the
        # map gives absolute tiles, which used to be read as offsets and
        # pushed the block #dx tiles too far.
        return (dx - px, dy - py) if args.abs else (dx, dy)

    # A quiet trap: with --at 3,0 and --with-block overdrive-dome@5,1 a user
    # means the tile at (5,1) on the map, but relative offsets would be (2,1).
    if not args.abs and (px or py):
        look_abs = [(l.name, l.dx, l.dy) for l in links
                    if l.dx >= px and l.dy >= py and (l.dx, l.dy) != (0, 0)]
        if look_abs:
            print(f"# note: {look_abs[0][0]}@{look_abs[0][1]},{look_abs[0][2]} "
                  f"looks absolute (>= --at {px},{py}); pass --abs to use map tiles",
                  file=sys.stderr)

    links = [_gen.Link(l.name, *_to_rel(l.dx, l.dy)) for l in links]

    bp = _gen.Blueprint(args.name, args.description, args.label)
    bp.add_logic(code, px, py, args.block, args.rotation, links)

    placed = []
    for spec in args.with_block:
        try:
            blk, dx, dy, cid = _gen.parse_with_block(spec)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if not args.abs and (px or py) and dx >= px and dy >= py:
            print(f"# note: {blk}@{dx},{dy} looks absolute "
                  f"(>= --at {px},{py}); pass --abs to use map tiles",
                  file=sys.stderr)
        rdx, rdy = _to_rel(dx, dy)
        bp.add_block(blk, px + rdx, py + rdy, 0, None, cid)
        placed.append(f"{blk} center@({px+rdx},{py+rdy})"
                      + (f" id={cid}" if cid is not None else ""))

    data = bp.build()

    # never emit something that does not read back
    back = msch.parse_bytes(data)
    got = back.logic_tiles()
    if not (got and got[0].logic and got[0].logic.code.strip() == code.strip()
            and back.name == args.name):
        print("VERIFY FAILED -- not emitting", file=sys.stderr)
        return 1

    stmts = len([l for l in code.split("\n") if l.strip()])
    w, h = bp.bounds()
    print(f"# {args.block} @({px},{py})  {stmts} stmts  {w}x{h}  "
          f"links={[f'{l.name}@{l.dx},{l.dy}' for l in links] or '(none)'}",
          file=sys.stderr)
    for p in placed:
        print(f"# placed: {p}", file=sys.stderr)
    for msg in bp.link_report():
        print(f"# NOTE: {msg}", file=sys.stderr)
    if args.auto_link:
        for lk in links:
            print(f"# link name check: {lk.name}", file=sys.stderr)

    if args.out:
        b64 = base64.b64encode(data).decode()
        with open(args.out, "w", encoding="ascii") as fh:
            fh.write(b64 + "\n")
        print(f"# wrote paste code -> {args.out}", file=sys.stderr)
    if args.binary:
        bp.write(args.binary)
        print(f"# wrote binary -> {args.binary} ({len(data)} bytes)", file=sys.stderr)
    if args.quiet:
        return 0
    print(base64.b64encode(data).decode())
    return 0


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #

def _report(label: str, findings: list, show_rule: bool = False) -> int:
    print(f"=== {label}")
    for f in findings:
        rule = f"  <{f.rid}>" if show_rule and getattr(f, "rid", None) else ""
        print(f"{f}{rule}")
    errs = [f for f in findings if f.sev in ("ERR", "SUSPECT")]
    if not findings:
        print("  no issues found")
    print(f"  -> {len(errs)} error/suspect, "
          f"{len([f for f in findings if f.sev == 'WARN'])} warn")
    print()
    return 1 if errs else 0


def _cost(lines: list[str], links: list[str] | None = None) -> None:
    """Print the reachability / loop-bound / instruction-cost summary."""
    print(_check.cost_report(_check.prepare(lines), links))
    print()


def _split_targets(args):
    """Positional paths split by suffix: `.msch` is a blueprint, the rest is text.

    Typing `mlog.py check foo.msch` used to open the schematic as UTF-8 and die
    with a decode error; the suffix is unambiguous, so route it instead of
    making the user remember `--msch`.
    """
    text, mascs = [], []
    for q in (args.files or []):
        (mascs if str(q).lower().endswith(".msch") else text).append(q)
    mascs += list(getattr(args, "msch", None) or [])
    return text, mascs


def cmd_check(args) -> int:
    rc = 0
    _text_targets, _msch_targets = _split_targets(args)
    for p in _text_targets:
        with open(p, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        rc |= _report(p, _check.analyse(lines), args.rid)
        if args.cost:
            _cost(lines)
    for p in _msch_targets:
        sch = msch.parse_file(p)
        for t in sch.logic_tiles():
            assert t.logic is not None
            lines = t.logic.code.split("\n")
            names = [l.name for l in t.logic.links]
            rc |= _report(f"{p} [{t.block} @({t.x},{t.y})]",
                          _check.analyse(lines, names), args.rid)
            if args.cost:
                _cost(lines, names)
    return rc


# --------------------------------------------------------------------------- #
# ir -- structural / dataflow view
# --------------------------------------------------------------------------- #

# Names authors use purely as in-editor annotations. The game really does
# execute `set // foo` (only `#` starts a comment -- LParser.java:98-99), so
# these are technically dead stores, but reporting them is pure noise.
_ANNOTATION = re.compile(r"^[^A-Za-z0-9_@\"]*$")

# A def that is *forced by syntax*: these statements have several output
# slots and the author must name every one, so an unread slot is not a
# mistake, it is unavoidable. Measured over the corpus: 0.267/block for
# `ulocate` alone -- reporting those would bury the real findings.
_SLOT_FORCED = frozenset({
    "ulocate", "ucontrol", "radar", "uradar", "getlink", "getblock",
    "lookup", "weathersense", "spawn", "bullet", "status",
})

# `set x 3` with nothing reading x is how mlog authors "declare" a variable
# or leave a note. 0.99 per block across the corpus, so hidden by default.
_LITERAL = re.compile(r"^(?:-?\d+(?:\.\d+)?|true|false|@\S+|\"[^\"]*\")$")

# A *fullwidth punctuation* character inside a name is nearly always a typo'd
# `!` or `(` typed with a Chinese IME, and it silently creates a brand-new
# variable (`cy！` vs `cy!`). Pure Chinese names like `关闭时长` are perfectly
# legitimate, so the test is the fullwidth punctuation block only -- not
# "non-ASCII".
_FULLWIDTH = re.compile(
    "[\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff5e\u3000-\u303f]")


def _text(t) -> str:
    """A token's text, whichever shape `mlogir` hands us (Tok or str)."""
    return getattr(t, "text", t)


def odd_names(prog) -> dict[str, list[int]]:
    """Variables whose name contains fullwidth punctuation, with their lines.

    Exposed as a function (not just a print block) so `tests/run_tests.py` can
    assert on it: the *finding* is what matters, not the wording around it.
    """
    weird: dict[str, list[int]] = {}
    for s in prog.stmts:
        for v in sorted(s.defs | s.uses):
            if v.startswith("@") or not _FULLWIDTH.search(v):
                continue
            weird.setdefault(v, []).append(s.line)
    return weird


def _ir_tier(prog, i: int, v: str, kind: str) -> str:
    """`reported`, `slot` (forced by syntax), `declaration` or `annotation`."""
    # A name made only of punctuation (`//`, `--`, `===`) is a note the author
    # typed into the code, taken as a variable name because mlog has no
    # end-of-line comment (only `#` starts one, `LParser.java:98-99`). 222 of
    # these across the corpus -- the single largest noise source after `set`.
    if _ANNOTATION.match(v):
        return "annotation"
    # `stale-write` (written 2+ times, read nowhere) outranks the slot and
    # declaration tiers on purpose: a forced output slot written *once* is just
    # the syntax making you name a variable you don't need, but writing the
    # same slot twice and never reading it means the first write is provably
    # discarded -- the `found`/`found` bug in
    # 17十七硫幽灵四熔毁四油海啸组.msch, which the slot tier used to hide.
    if kind in ("overwritten", "stale-write"):
        return "reported"
    s = prog.stmts[i]
    if s.mnemonic in _SLOT_FORCED:
        return "slot"
    if s.mnemonic == "set" and all(_LITERAL.match(_text(a))
                                   for a in s.args[1:]):
        return "declaration"
    return "reported"


def _ir_report(label: str, lines: list[str], show_all: bool = False) -> int:
    prog = _ir.parse(lines)
    if not len(prog):
        return 0
    dead = prog.dead()
    graded = [(i, v, k) for i, v, k in prog.classify_dead_stores()
              if not _ANNOTATION.match(v)]
    tiers = [(i, v, k, _ir_tier(prog, i, v, k)) for i, v, k in graded]
    shown = [t for t in tiers if show_all or t[3] == "reported"]
    hidden: dict[str, int] = {}
    for t in tiers:
        if t not in shown:
            hidden[t[3]] = hidden.get(t[3], 0) + 1

    reads = {v for s in prog.stmts for v in s.uses}
    writes = {v for s in prog.stmts for v in s.defs}
    ops: dict[str, int] = {}
    for s in prog.stmts:
        ops[s.mnemonic] = ops.get(s.mnemonic, 0) + 1

    print(f"=== {label}")
    printed_dead = [] if prog.writes_counter() else dead
    print(f"  statements      : {len(prog)}"
          + (f"   ({len(printed_dead)} unreachable)" if printed_dead else ""))
    print("  unknown         : "
          + (", ".join(sorted(prog.unknown_mnemonics)) or "none"))
    print(f"  variables       : {len(reads)} read, {len(writes)} written")
    if printed_dead:
        print(f"  UNREACHABLE     : {len(printed_dead)} statement(s) can never run: "
              + ", ".join(f"L{prog.stmts[i].line}" for i in printed_dead[:12])
              + (" ..." if len(printed_dead) > 12 else ""))
    elif dead and prog.writes_counter():
        print("  UNREACHABLE     : not reported -- this program writes @counter "
              "(dispatch table), so static reachability is meaningless")
    if shown:
        print(f"  DEAD STORES     : {len(shown)} worth a look:")
        for i, v, kind, _tier in shown[:20]:
            why = {"overwritten": "value overwritten before any read",
                   "stale-write": "written more than once, read nowhere -- "
                                  "every write after the first is wasted",
                   }.get(kind, "computed, never read")
            print(f"      L{prog.stmts[i].line:<5} {v:<22} {why}")
            print(f"      {'':<5} {'':<22} {prog.stmts[i].raw.strip()[:70]}")
        if len(shown) > 20:
            print(f"      ... and {len(shown) - 20} more")
    else:
        print("  dead stores     : none")
    if hidden:
        bits = []
        if hidden.get("slot"):
            bits.append(f"{hidden['slot']} forced output slot(s)")
        if hidden.get("declaration"):
            bits.append(f"{hidden['declaration']} literal declaration(s)")
        print("  (hidden)        : " + ", ".join(bits)
              + "   -- rerun with --all to list them")
    weird = odd_names(_ir.parse(lines))
    if weird:
        print("  ODD NAMES       : fullwidth punctuation in a variable name "
              "(usually an IME typo, which silently "
              "creates a different variable):")
        for v, lns in list(weird.items())[:8]:
            print(f"      {v:<22} at "
                  + ", ".join(f"L{n}" for n in lns[:6])
                  + (" ..." if len(lns) > 6 else ""))
    print("  opcodes         : "
          + ", ".join(f"{k}x{v}" for k, v in
                      sorted(ops.items(), key=lambda kv: -kv[1])[:10]))
    print()
    return 0


def cmd_ir(args) -> int:
    rc = 0
    _text_targets, _msch_targets = _split_targets(args)
    show_all = bool(getattr(args, "all", False))
    for p in _text_targets:
        with open(p, encoding="utf-8") as fh:
            rc |= _ir_report(p, fh.read().split("\n"), show_all)
    for p in _msch_targets:
        sch = msch.parse_file(p)
        tiles = list(sch.logic_tiles())
        for t in tiles:
            assert t.logic is not None
            rc |= _ir_report(f"{p} [{t.block} @({t.x},{t.y})]",
                             t.logic.code.split("\n"), show_all)
        # Cross-processor `read`/`write` addresses a NAMED slot in another
        # processor, and that slot must already exist there. Needs the whole
        # schematic, so it cannot live inside the per-tile report.
        blocks = [(f"{p} [{t.block} @({t.x},{t.y})]", t.logic.code.split("\n"))
                  for t in tiles if t.logic is not None]
        for f in _check.cross_processor(blocks):
            print(f"[{f.sev}] L{f.line} {f.code}")
            print(f"          {f.msg}")
            rc |= 1 if f.sev == "ERR" else 0
    return rc


def _val_report(label: str, lines: list[str],
                traces: list[str] | None = None,
                slots: dict | None = None) -> int:
    prog = _ir.parse(lines)
    st = _val.analyze(prog, slots=slots)
    print(_val.format_report(label, prog, st))
    if slots:
        print(f"  cross knowledge : {st.cross_folded} read statement(s) resolved "
              f"against {len(slots)} readable slot(s) on other processors")
        print("                    (a null here means no processor in THIS "
              "schematic ever assigns that slot -- a processor outside the "
              "blueprint cannot be seen)")
    for v in traces or []:
        rows = _val.trace(prog, st, v)
        print(f"  -- trace {v} --")
        for r in rows:
            print(r)
        if not rows:
            print("      (never written)")
    return 0


def cmd_values(args) -> int:
    rc = 0
    _text_targets, _msch_targets = _split_targets(args)
    traces = list(getattr(args, "trace", None) or [])
    for p in _text_targets:
        with open(p, encoding="utf-8") as fh:
            rc |= _val_report(p, fh.read().split("\n"), traces)
    for p in _msch_targets:
        sch = msch.parse_file(p)
        # Two passes: pass 1 learns what each processor holds, pass 2 re-runs
        # every program with that knowledge. Deliberately *one* iteration --
        # pass 1 itself assumed TOP for its own cross reads, so iterating to a
        # fixpoint would report confidence the model does not have.
        units = _cx.units_from_schematic(sch)
        known = _cx.slot_values(_cx.analyze_units(units), units) if units else {}
        for t in sch.logic_tiles():
            assert t.logic is not None
            label = f"{p} [{t.block} @({t.x},{t.y})]"
            rc |= _val_report(label, t.logic.code.split("\n"), traces,
                              known.get((t.x, t.y)))
    return rc


def cmd_cross(args) -> int:
    """Cross-processor `read`/`write`: resolve each read against its writers.

    Needs the whole schematic (the value comes from another program), so this
    only accepts blueprints -- a lone .mlog has no peers to read from.
    """
    paths = [p for p in (args.files or []) if str(p).lower().endswith(".msch")]
    skipped = [p for p in (args.files or []) if not str(p).lower().endswith(".msch")]
    paths += [p for p in (args.msch or []) if p not in paths]
    for p in skipped:
        print(f"!! {p}: not a .msch -- cross-processor needs the whole schematic")
    for p in paths:
        sch = msch.parse_file(p)
        rep = _cx.analyze_units(_cx.units_from_schematic(sch))
        print(_cx.format_report(rep, p))
    return 0


def cmd_pack(args) -> int:
    """Recognise packed-config encode/decode and report field capacity."""
    rc = 0
    _text_targets, _msch_targets = _split_targets(args)
    item_count = len(ITEM_IDS) or None
    for p in _text_targets:
        with open(p, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        print(_pk.format_report(_ir.parse(lines), item_count=item_count,
                                label=p))
        rc = 0
    for p in _msch_targets:
        sch = msch.parse_file(p)
        for t in sch.logic_tiles():
            assert t.logic is not None
            print(_pk.format_report(
                _ir.parse(t.logic.code.split("\n")), item_count=item_count,
                label=f"{p} [{t.block} @({t.x},{t.y})]"))
    return rc


# --------------------------------------------------------------------------- #
# id / cmd reference
# --------------------------------------------------------------------------- #

def cmd_id(args) -> int:
    if args.name:
        hits = [(k, v) for k, v in BLOCK_IDS.items() if args.name.lower() in v.lower()]
        if not hits:
            print(f"no block matching {args.name!r} in the id table")
            return 1
        for k, v in sorted(hits, key=lambda kv: int(kv[0])):
            print(f"  id={k:>4}  size={_gen.block_size(v):<2}  {v}")
        return 0

    print(f"{len(BLOCK_IDS)} blocks known (vanilla id table)")
    print()
    print("id / size / name")
    for k in sorted(BLOCK_IDS, key=int):
        v = BLOCK_IDS[k]
        print(f"  {k:>4}  {_gen.block_size(v):<2}  {v}")
    return 0


def cmd_sensor(args) -> int:
    """List what `sensor` can read: one property per line, with what it means."""
    if not SENSOR_ATTRS:
        print("!! sensor_attrs.json is missing; run tools/sensor_attrs_from_jar.py",
              file=sys.stderr)
        return 1
    pat = (args.pattern or "").lower()
    rows = []
    for name, e in SENSOR_ATTRS.items():
        hay = f"{name} {e.get('desc', '')} {e.get('target', '')}".lower()
        if pat and pat not in hay:
            continue
        if args.settable and not e.get("params"):
            continue
        if args.on and args.on not in e.get("target", ""):
            continue
        rows.append((name, e))
    if not rows:
        print(f"no property matches {pat!r}"
              + (" (with --settable)" if args.settable else ""))
        print(f"  {len(SENSOR_ATTRS)} properties in sensor_attrs.json; try `mlog.py sensor` with no filter")
        return 1
    print(f"{len(rows)} propert{'y' if len(rows) == 1 else 'ies'}"
          f"  (source: sensor_attrs.json / v160.7 jar LAccess)")
    for name, e in rows:
        tag = "  [object]" if e.get("obj") else ""
        tgt = f"  ({e.get('target')})" if e.get("target") else ""
        print(f"  @{name:<18}{tag}{tgt}")
        print(f"  {'':<2}{e.get('desc', '')}")
        if e.get("params"):
            print(f"  {'':<2}settable: control <block> {name} "
                  + " ".join(f"<{p}>" for p in e["params"]))
    if not args.pattern and not args.settable and not args.on:
        print("\n  filter by name or meaning: `mlog.py sensor heat`")
        print("  only the writable ones:       `mlog.py sensor --settable`")
    return 0


def cmd_item(args) -> int:
    """Item content ids, plus decoding of the common packed-config idiom."""
    if args.unpack:
        # The "portable config" idiom seen in the wild:
        #   data = status * 1e8 + outputID * 1e6 + inputMask
        # Decoding outputID is only valid when status == 0, because
        # idiv(data, 1e6) folds status into the quotient.
        data = int(args.unpack)
        status, rest = divmod(data, 1_000_000_000)
        output_id, mask = divmod(rest, 1_000_000)
        print(f"packed  : {data}  (0x{data:X})")
        print(f"status  : {status}")
        out_name = ITEM_IDS.get(str(output_id), f"<unknown id {output_id}>")
        print(f"output  : {output_id} = {out_name}"
              + ("   ⚠ status != 0: this field is NOT just the output id"
                 if status else ""))
        bits = [i for i in range(32) if mask >> i & 1]
        print(f"mask    : {mask}  (0x{mask:X}) -> bit positions {bits}")
        for b in bits:
            print(f"  bit {b:>2} -> {ITEM_IDS.get(str(b), f'<unknown id {b}>')}")
        if mask >= 1_000_000:
            print("  ⚠ mask overflows the 6-digit field")
        if max(bits, default=-1) >= 20:
            print(f"  ⚠ bit {max(bits)} >= 20: outside what this 20-bit slot "
                  f"holds safely")
        return 0

    if args.name:
        q = args.name.strip().lower()
        hits = []
        for k, v in ITEM_IDS.items():
            if q in v.lower() or q == k:
                hits.append((int(k), v))
        if not hits:
            print(f"no item matching {args.name!r} in the id table")
            return 1
        for i, v in sorted(hits):
            print(f"  id={i:>3}  {v}")
        return 0

    print(f"{len(ITEM_IDS)} items known (vanilla id = Items.java construction order)")
    print()
    for k in sorted(ITEM_IDS, key=int):
        print(f"  {k:>3}  {ITEM_IDS[k]}")
    print()
    print("tip: mlog.py item --unpack <number>  decodes "
          "status*1e8 + outputID*1e6 + inputMask")
    return 0


def cmd_unit(args) -> int:
    """Unit stats from `UnitTypes.java`: the first decision when writing logic."""
    units = UNIT_ATTRS.get("units") or {}
    dflt = UNIT_ATTRS.get("defaults") or {}
    if not units:
        print("unit_attrs.json missing (run tools/units_from_source.py)",
              file=sys.stderr)
        return 1
    q = (args.name or "").strip().lower()
    hits = sorted(n for n in units if not q or q in n)
    if q in units:
        # 精确名优先:`oct` 否则会同时命中 `spiroct`,`unit oct` 就没法看细节
        hits = [q]
    if not hits:
        print(f"no unit matching {args.name!r}")
        return 1
    if not q:
        print(f"{len(units)} units known  (fields are only the ones the source "
              f"sets; `(default ...)` comes from UnitType.java)")
        print()
    for n in hits:
        rec = units[n]
        bits = []
        for f in ("speed", "health", "armor", "hitSize", "itemCapacity",
                  "mineSpeed", "mineTier", "buildSpeed", "range", "flying",
                  "rotateSpeed", "omniMovement", "canBoost", "accel", "drag"):
            if f in rec:
                bits.append(f"{f} {rec[f]}")
            elif f in dflt:
                bits.append(f"{f} (default {dflt[f]})")
        w = rec.get("weapons") or {}
        if w.get("count"):
            wtxt = f"{w['count']} weapon(s)"
            if w.get("reload"):
                wtxt += f", reload {w['reload']}t"
            if w.get("bullet_damage"):
                sp = f"{w['bullet_speed']}sp/" if w.get("bullet_speed") else ""
                wtxt += f", {w['bullet']} {sp}{w['bullet_damage']}dmg"
            bits.append(wtxt)
        print(f"  {n:<14} " + "; ".join(bits))
        if len(hits) == 1:
            for i, wp in enumerate(w.get("list") or [], 1):
                line = f"{wp.get('bullet', '?')}"
                if wp.get("damage"):
                    line += f"  {wp['speed']} speed / {wp['damage']} damage"
                if wp.get("reload"):
                    line += f"  reload {wp['reload']}t"
                if wp.get("shots"):
                    line += f"  {wp['shots']} shot(s)"
                if wp.get("lifetime"):
                    line += f"  life {wp['lifetime']}t"
                if wp.get("inaccuracy"):
                    line += f"  inacc {wp['inaccuracy']}"
                print(f"               weapon {i}: {line}")
            for ab in rec.get("abilities") or []:
                args = ", ".join(ab.get("args") or [])
                print(f"               ability: {ab['ability']}"
                      + (f"({args})" if args else ""))
            if rec.get("immune_to"):
                print(f"               immune to: {', '.join(rec['immune_to'])}")
    return 0


def cmd_block(args) -> int:
    """What a block accepts, produces and holds -- recipes straight from source."""
    blocks = BLOCK_ATTRS.get("blocks") or {}
    classes = BLOCK_ATTRS.get("classes") or {}
    if not blocks:
        print("block_attrs.json missing (run tools/blocks_from_source.py)",
              file=sys.stderr)
        return 1
    q = (args.name or "").strip().lower()
    want = (getattr(args, "accepts", None) or "").strip().lower()
    if want:
        hits = sorted(n for n, r in blocks.items()
                      if r.get("accepts") in ("any", "custom")
                      or want in (r.get("ammo") or [])
                      or want in [i.split(":")[0] for i in
                                  r.get("inputs") or []])
        if not hits:
            print(f"no block accepts {want!r}")
            return 1
        buckets: dict[str, list[str]] = {"recipe": [], "ammo": [], "any": [],
                                         "custom": []}
        for n in hits:
            acc = blocks[n].get("accepts", "")
            if acc in buckets:
                buckets[acc].append(n)
        print(f"  blocks that accept {want}: {len(hits)}")
        heads = (("recipe", "uses it as a recipe input (always accepted)"),
                 ("ammo", "uses it as turret ammo"),
                 ("any", "accepts any item (capacity-bound)"),
                 ("custom", "accepts it only under some runtime condition"))
        for key, label in heads:
            names = buckets[key]
            if not names:
                continue
            print(f"    {label} -- {len(names)}:")
            line = "      "
            for n in names:
                if len(line) + len(n) + 2 > 96:
                    print(line.rstrip())
                    line = "      "
                line += n + ", "
            print(line.rstrip().rstrip(","))
        return 0
    hits = sorted(n for n in blocks if not q or q in n)
    if not hits:
        print(f"no block with captured behaviour matching {args.name!r}")
        return 1
    for n in hits:
        rec = blocks[n]
        ids = [k for k, v in BLOCK_IDS.items() if v == n]
        head = f"  {n:<22}"
        if ids:
            head += f" id={ids[0]:>4}"
        head += f"  size={rec.get('size', '?')}  ({classes.get(n, '?')})"
        print(head)
        build = rec.get("build") or {}
        if build.get("cost"):
            print(f"      build   {' + '.join(build['cost'])}"
                  f"  ({build.get('category', '?')})")
        acc = rec.get("accepts", "?")
        if acc == "any":
            print("      accepts any item (capacity-bound)")
        elif acc == "custom":
            print("      accepts any item, but routes it per config")
        elif acc == "ammo":
            print(f"      accepts only as ammo: {', '.join(rec.get('ammo') or [])}")
        elif acc == "none":
            print("      accepts nothing")
        elif rec.get("inputs"):
            print(f"      accepts only {' + '.join(rec['inputs'])}")
        if rec.get("inputs") and rec.get("outputItem"):
            t = f" craftTime={rec['craftTime']}t" if rec.get("craftTime") else ""
            print(f"      recipe  {' + '.join(rec['inputs'])} -> "
                  f"{rec['outputItem']}{t}")
        elif rec.get("outputItems"):
            print(f"      outputs {' + '.join(rec['outputItems'])}")
        elif rec.get("inputs") and acc != "recipe":
            print(f"      consumes {' + '.join(rec['inputs'])}")
        liq = rec.get("liquids") or []
        out_liq = rec.get("outputLiquids") or []
        if liq or out_liq:
            arrow = f" -> {' + '.join(out_liq)}" if out_liq else ""
            print(f"      liquids {(' + '.join(liq)) or '--'}{arrow}")
        if rec.get("booster"):
            print(f"      booster {' + '.join(rec['booster'])}"
                  "  (optional; speeds the block up)")
        if rec.get("coolant"):
            print(f"      coolant {rec['coolant']}")
        extra = []
        if rec.get("itemCapacity"):
            extra.append(f"itemCapacity {rec['itemCapacity']}")
        if rec.get("liquidCapacity"):
            extra.append(f"liquidCapacity {rec['liquidCapacity']}")
        if rec.get("power"):
            extra.append(f"power {rec['power']}")
        if rec.get("hasItems"):
            extra.append("hasItems")
        if rec.get("flammable"):
            extra.append("burns flammables")
        if rec.get("explosive"):
            extra.append("burns explosives")
        if rec.get("radioactive"):
            extra.append("burns radioactive items (no fixed item list)")
        if rec.get("explodes"):
            extra.append("explodes on explosive inputs")
        if extra:
            print("      " + "; ".join(extra))
    return 0


def _cheap_profile(code: list[str]) -> tuple[collections.Counter, set[str], str]:
    """Opcodes, identifiers and quoted text, without building the IR.

    Searching does not need a real parse: `mlogir.parse` builds Tok/Stmt objects
    and costs seconds per thousand statements, which made a corpus-wide search
    take minutes for no gain -- the search only needs "which words appear here".
    """
    ops: collections.Counter = collections.Counter()
    names: set[str] = set()
    strings: list[str] = []
    for raw in code:
        line = _check.strip_comment(raw).strip()
        if not line:
            continue
        parts = line.split()
        mn = parts[0].lower()
        if mn.endswith(":"):
            continue                      # jump label, not a statement
        ops[mn] += 1
        for tok in parts[1:]:
            if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
                strings.append(tok[1:-1])
            elif not _check.is_number(tok) and tok not in ("true", "false", "null"):
                names.add(tok.lower())
    return ops, names, " ".join(strings).lower()


def _find_cache_path() -> str:
    return os.path.join(HERE, ".find_cache.json")


def _load_find_cache() -> dict:
    try:
        with open(_find_cache_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _store_find_cache(cache: dict) -> None:
    try:
        with open(_find_cache_path(), "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False)
    except Exception:
        pass


def _corpus_dir() -> str:
    return os.environ.get("MLOG_CORPUS") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming", "Mindustry", "schematics")


def cmd_find(args) -> int:
    """Find the corpus blueprint that looks most like a described job.

    This is the step that was missing when I wrote wireless-overdrive logic: the
    correct move was "go read how someone else solved this", and nothing in the
    toolset could do it. Scoring is deliberately crude and explainable -- the
    score breakdown is printed so a bad match is visible, not mysterious.
    """
    terms = [t for t in (args.query or "").lower().split() if t]
    if not terms:
        print("give at least one search term", file=sys.stderr)
        return 2
    root = args.corpus or _corpus_dir()
    if not os.path.isdir(root):
        print(f"corpus not found: {root}  (set MLOG_CORPUS or pass --corpus)",
              file=sys.stderr)
        return 2
    limit = args.limit or 5
    max_stmts = args.max_stmts
    max_bytes = args.max_bytes
    rows = []
    skipped = 0
    big_files = 0
    cache = {} if args.rebuild else _load_find_cache()
    fresh: dict = {}
    for path in sorted(os.listdir(root)):
        if not path.lower().endswith(".msch"):
            continue
        full = os.path.join(root, path)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size > max_bytes:
            big_files += 1
            continue
        stamp = f"{size}:{int(os.path.getmtime(full))}"
        entry = cache.get(path)
        if entry and entry.get("stamp") == stamp:
            blocks = entry.get("blocks") or []
            texts = entry.get("texts", "")
        else:
            try:
                sch = msch.parse_file(full)
            except Exception:
                continue
            texts = " ".join([sch.name or "", sch.description or ""]).lower()
            blocks = []
            for t in sch.logic_tiles():
                if t.logic is None:
                    continue
                src = t.logic.code
                # Generated art (BADAPPLE!! is 564k statements and takes 24s to
                # parse) is not reference logic; searching it costs minutes and
                # never produces a useful hit. Test the raw string first --
                # split() on half a megabyte of lines costs more than the whole
                # rest of the scan.
                if len(src) > max_stmts * 32:
                    skipped += 1
                    continue
                code = src.split("\n")
                if len(code) > max_stmts:
                    skipped += 1
                    continue
                ops, names, strings = _cheap_profile(code)
                blocks.append([f"{t.block} @({t.x},{t.y})", sum(ops.values()),
                               dict(ops), sorted(names), strings])
            entry = {"stamp": stamp, "texts": texts, "blocks": blocks}
        fresh[path] = entry
        for label, n, ops, names, strings in blocks:
            names = set(names)
            hay = f"{texts} {strings}"
            name_hit = sum(1 for t2 in terms if t2 in hay)
            var_hit = sum(1 for t2 in terms if t2 in names or any(t2 in x for x in names))
            op_hit = sum(1 for t2 in terms if t2 in ops)
            score = 10 * name_hit + 4 * var_hit + 6 * op_hit
            if score <= 0:
                continue
            rows.append((score, path, label, n, collections.Counter(ops),
                         strings, (name_hit, var_hit, op_hit)))
    _store_find_cache(fresh)
    if not rows:
        print(f"nothing in {root} matches {' '.join(terms)!r}")
        return 1
    rows.sort(key=lambda r: (-r[0], -r[3]))
    print(f"{len(rows)} candidate(s) matching {' '.join(terms)!r}, "
          f"top {min(limit, len(rows))}:")
    if skipped:
        print(f"({skipped} generated art block(s) over {max_stmts} statements "
              f"skipped -- raise --max-stmts to search them)")
    if big_files:
        print(f"({big_files} blueprint file(s) over {max_bytes // 1000}k "
              f"skipped: 16MB pixel art / emulator dumps, not reference logic "
              f"-- raise --max-bytes to include them)")
    for score, path, label, n, ops, strings, why in rows[:limit]:
        print()
        print(f"=== {path}  [{label}]  {n} statements  "
              f"score {score}  (text {why[0]}, name {why[1]}, opcode {why[2]})")
        top = ", ".join(f"{k}x{v}" for k, v in ops.most_common(6))
        print(f"    opcodes : {top}")
        if strings.strip():
            print(f"    text    : {strings.strip()[:160]}")
        print(f"    read it : python mlog.py ir --msch \"{full_path(root, path)}\"")
    return 0


# Mnemonics whose first argument is a keyword, never a variable.
_STYLE_ENUM1 = {"op", "ucontrol", "draw", "control", "ulocate", "getblock",
                "lookup", "radar", "uradar", "status", "fetch", "setblock",
                "setprop", "cutscene", "effect", "setmarker", "setrule"}
_STYLE_LINK = re.compile(r"^[a-z][a-z-]*\d+$")
_STYLE_WORD = re.compile(r"[A-Za-z_\u4e00-\u9fff]")


def _style_names(code: list[str]) -> collections.Counter:
    """Variable names, weighted by how often they appear, minus the non-names.

    Link names (`cell1`, `pad3`), literals, `@builtins` and punctuation-only
    "names" (the `//` convention) are all excluded -- what is left is the
    author's private vocabulary, which is the thing worth imitating.
    """
    counts: collections.Counter = collections.Counter()
    for raw in code:
        line = _check.strip_comment(raw).strip()
        if not line:
            continue
        parts = line.split()
        mn = parts[0].lower()
        if mn.endswith(":"):
            continue
        # Enum slots are not variables: `op <name>`, `jump <addr> <cond>`,
        # `ucontrol <sub>`, `draw <sub>`, `control <type>` ... Counting them made
        # every "naming" line read "strictEqual, always, greaterThan, add".
        skip = set()
        if mn in _STYLE_ENUM1:
            skip.add(1)
        if mn == "jump" and len(parts) > 2:
            skip.add(2)
            if parts[2] == "always":
                skip |= {3, 4}          # `jump N always x false` filler slots
        for i, tok in enumerate(parts[1:], start=1):
            if i in skip:
                continue
            if not tok or tok[0] in '"@':
                continue
            if tok in ("true", "false", "null") or _check.is_number(tok):
                continue
            if _STYLE_LINK.match(tok) or not _STYLE_WORD.search(tok):
                continue
            counts[tok] += 1
    return counts


def _style_shape(code: list[str]) -> list[str]:
    """The habits that make a block recognisable, as short phrases."""
    body = [ln for ln in (_check.strip_comment(x).strip() for x in code) if ln]
    labels = [ln[:-1] for ln in body if ln.endswith(":") and len(ln.split()) == 1]
    ops = collections.Counter(ln.split()[0].lower() for ln in body
                              if not ln.endswith(":"))

    def has(pat: str) -> bool:
        return any(re.search(pat, ln) for ln in body)

    notes: list[str] = []
    if has(r"^op\s+add\s+@counter\s+@counter"):
        notes.append("dispatch table (op add @counter @counter)")
    if has(r"^select\b"):
        notes.append("select instead of branches")
    if has(r"^wait\b"):
        notes.append("wait pacing")
    if has(r'^(read|write)\s+\S+\s+\S+\s+"'):
        notes.append("named cross-processor slot")
    if has(r"^(read|write)\s+\S+\s+(cell|bank)\d"):
        notes.append("memory cell")
    if has(r"^ubind\b"):
        notes.append("ubind")
    if has(r"^ucontrol\b"):
        notes.append("ucontrol")
    if has(r"^(radar|uradar)\b"):
        notes.append("radar")
    if has(r"^draw\b"):
        notes.append("draw")
    if has(r"^printflush\b"):
        notes.append("message board")
    if has(r"^sensor\b.*@(unit|this|dead|controller)"):
        notes.append("sensor on @unit/@this")
    if has(r"^control\b"):
        notes.append("control enabled/config")
    if labels:
        notes.append(f"{len(labels)} label(s): " + ", ".join(labels[:6]))
    if ops.get("jump"):
        notes.append(f"{ops['jump']} jump(s)")
    if ops.get("set") and ops.get("jump") and ops["set"] > 2 * max(ops["jump"], 1):
        notes.append("set-heavy (data-first, few branches)")
    return notes


def _style_cost(code: list[str], links: list[str]) -> str:
    """The two lines of `--cost` worth quoting: cycle length and processor ticks."""
    try:
        rep = _check.cost_report(_check.prepare(code), links)
    except Exception:
        return "(cost unavailable)"
    keep = []
    for ln in rep.splitlines():
        t = ln.strip()
        if t.startswith("worst-case path") or t.startswith("micro-processor"):
            keep.append(t)
    return " ; ".join(keep) if keep else "(no cost lines)"


def _style_block(root: str, path: str, label: str, code: list[str],
                 links: list[str], score: int, dump: int) -> list[str]:
    names = _style_names(code)
    body = [ln for ln in (_check.strip_comment(x).strip() for x in code) if ln]
    out = [f"=== {path}  [{label}]  {len(body)} statements  score {score}"]
    if names:
        top = ", ".join(f"{k}x{v}" for k, v in names.most_common(10))
        out.append(f"    naming  : {top}")
        dotted = sum(1 for k in names if "." in k)
        cjk = sum(1 for k in names if re.search(r"[\u4e00-\u9fff]", k))
        snake = sum(1 for k in names if "_" in k and "." not in k)
        out.append(f"    flavour : {dotted} dotted, {snake} underscored, "
                   f"{cjk} Chinese, {len(names) - dotted - snake - cjk} plain")
    shape = _style_shape(code)
    if shape:
        out.append(f"    shape   : {', '.join(shape)}")
    out.append(f"    cost    : {_style_cost(code, links)}")
    if links:
        out.append(f"    links   : {', '.join(links[:10])}"
                   f"{' …' if len(links) > 10 else ''}")
    if body:
        out.append(f"    opens   : {body[0]}")
        if len(body) > 1:
            out.append(f"              {body[1]}")
        if len(body) > 2:
            out.append(f"    closes  : … {body[-2]}")
        out.append(f"              {body[-1]}")
    if dump:
        out.append("    --- source ---")
        for ln in code[:dump]:
            out.append("      " + ln)
        if len(code) > dump:
            out.append(f"      … ({len(code) - dump} more lines)")
    out.append(f'    read it : python mlog.py ir --msch "{full_path(root, path)}"')
    return out


def cmd_style(args) -> int:
    """Show how the corpus writes a job, before I write it myself.

    `find` answers "which blueprint does this"; `style` answers "how does the
    author of that blueprint actually write" -- the naming vocabulary, the shape
    of the control flow, the closing idiom, the cycle cost. Those are the things
    that make generated logic read like a beginner's.
    """
    terms = [t for t in " ".join(args.query).lower().split() if t]
    if not terms:
        print("give at least one search term", file=sys.stderr)
        return 2
    root = args.corpus or _corpus_dir()
    if not os.path.isdir(root):
        print(f"corpus not found: {root}  (set MLOG_CORPUS or pass --corpus)",
              file=sys.stderr)
        return 2
    limit = args.limit or 3
    rows = []
    cache = {} if args.rebuild else _load_find_cache()
    fresh: dict = {}
    for path in sorted(os.listdir(root)):
        if not path.lower().endswith(".msch"):
            continue
        full = os.path.join(root, path)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size > args.max_bytes:
            continue
        stamp = f"{size}:{int(os.path.getmtime(full))}"
        entry = cache.get(path)
        if not (entry and entry.get("stamp") == stamp):
            try:
                sch = msch.parse_file(full)
            except Exception:
                continue
            texts = " ".join([sch.name or "", sch.description or ""]).lower()
            blocks = []
            for t in sch.logic_tiles():
                if t.logic is None:
                    continue
                src = t.logic.code
                if len(src) > args.max_stmts * 32:
                    continue
                code = src.split("\n")
                if len(code) > args.max_stmts:
                    continue
                ops, names, strings = _cheap_profile(code)
                blocks.append([f"{t.block} @({t.x},{t.y})", sum(ops.values()),
                               dict(ops), sorted(names), strings])
            entry = {"stamp": stamp, "texts": texts, "blocks": blocks}
        fresh[path] = entry
        # File names in this corpus are Chinese descriptions ("11-12双液体大超着陆
        # 合金气旋"), so the path itself is part of the searchable text -- without
        # it a query like "钍反 防爆" matches nothing at all.
        hay_name = (entry.get("texts", "") + " " + path.lower())
        for label, n, ops, names, strings in entry.get("blocks") or []:
            hay = hay_name + " " + strings
            name_hit = sum(1 for t2 in terms if t2 in hay)
            var_hit = sum(1 for t2 in terms
                          if t2 in names or any(t2 in x for x in names))
            op_hit = sum(1 for t2 in terms if t2 in ops)
            score = 10 * name_hit + 4 * var_hit + 6 * op_hit
            if score > 0:
                rows.append((score, path, label, n))
    _store_find_cache(fresh)
    if not rows:
        print(f"nothing in {root} matches {' '.join(terms)!r}")
        return 1
    rows.sort(key=lambda r: (-r[0], -r[3]))
    # One file often has the same program on several processors (Phase-Bridge 15
    # Landing Pads has two identical ones). Show the file once and say how many
    # blocks matched, otherwise the "top 3" can be one blueprint three times.
    per_file: dict[str, int] = {}
    for _s, path, _l, _n in rows:
        per_file[path] = per_file.get(path, 0) + 1
    dedup: list[tuple[int, str, str, int]] = []
    seen_path: set[str] = set()
    for row in rows:
        if row[1] in seen_path:
            continue
        seen_path.add(row[1])
        dedup.append(row)
    print(f"{len(rows)} match(es) in {len(dedup)} file(s) for {' '.join(terms)!r}; "
          f"showing the top {min(limit, len(dedup))} as style references:")
    shown = 0
    shape_tally: collections.Counter = collections.Counter()
    for score, path, label, _n in dedup[:limit]:
        extra = per_file.get(path, 1) - 1
        if extra > 0:
            label = f"{label}  (+{extra} more block(s) in this file)"
        try:
            sch = msch.parse_file(os.path.join(root, path))
        except Exception:
            continue
        tile = next((t for t in sch.logic_tiles()
                     if f"{t.block} @({t.x},{t.y})" == label), None)
        if tile is None or tile.logic is None:
            continue
        code = tile.logic.code.split("\n")
        links = [l.name for l in tile.logic.links]
        print()
        for ln in _style_block(root, path, label, code, links, score, args.code):
            print(ln)
        shape_tally.update(_style_shape(code))
        shown += 1
    if shown and shown > 1:
        print()
        print(f"common across these {shown}:")
        for k, v in shape_tally.most_common(8):
            if v > 1:
                print(f"    {v}x {k}")
    return 0


def full_path(root: str, name: str) -> str:
    return os.path.join(root, name)


def cmd_cmd(args) -> int:
    if not CMD_INDEX:
        print("cmd_index.json missing", file=sys.stderr)
        return 1
    pat = (args.pattern or "").lower()
    shown = 0
    for c in CMD_INDEX:
        blob = (c.get("title", "") + " " + c.get("format", "")).lower()
        if pat and pat not in blob:
            continue
        shown += 1
        print(f"{c.get('num','?'):>3}  {c.get('title','')}")
        fmt = (c.get("format") or "").strip()
        if fmt:
            for line in fmt.split("\n")[:3]:
                print(f"        {line}")
        for note in (c.get("notes") or [])[:3]:
            print(f"        - {note[:110]}")
        print()
    if not shown:
        print(f"no command matching {pat!r}")
        return 1
    return 0


# --------------------------------------------------------------------------- #
# explain -- syntax from cmd_index.json merged with semantics from LExecutor.java
# --------------------------------------------------------------------------- #

SEMANTICS = _load_json("instr_semantics.json")
_INSTR = {k.lower(): (k, v)
          for k, v in (SEMANTICS.get("instructions") or {}).items()}
_DISAGREE = [d for d in ((SEMANTICS.get("docs_crosscheck") or {})
                         .get("disagreements") or []) if isinstance(d, dict)]


def _kv(label: str, text: str, indent: int = 13) -> None:
    """`  label   : text`, wrapped so continuation lines stay aligned."""
    print(textwrap.fill(str(text), width=94,
                        initial_indent=f"  {label:<{indent}} ",
                        subsequent_indent=" " * (indent + 4)))


def _slots(idxs, params) -> str:
    if not idxs:
        return "-"
    out = []
    for i in idxs:
        nm = params[i] if i < len(params) else f"arg{i}"
        out.append(f"{i}={nm}")
    return ", ".join(out)


def _bullet(text: str, indent: int = 18) -> None:
    print(textwrap.fill(str(text), width=94,
                        initial_indent=" " * indent + "- ",
                        subsequent_indent=" " * (indent + 2)))


def _explain_ops(args) -> int:
    meta = SEMANTICS.get("ops_meta") or {}
    ops = SEMANTICS.get("ops") or {}
    want = (args.sub or "").strip().lower()
    for k, v in meta.items():
        if k == "legacy_aliases":
            _kv("aliases", ", ".join(f"{a} -> {b}" for a, b in v.items()))
        else:
            _kv(k, v)
    print()
    print(f"  {'op':<15}{'n':>2}  {'symbol':<9}build")
    for name, spec in ops.items():
        if want and want not in name.lower():
            continue
        since = spec.get("min_build")
        # Most operators go back to v6; `emod` and `ushr` are v150 additions, so
        # the version gate has to look at the operator and not just at `op`.
        mark = f">= {since}" if since else ""
        print(f"  {name:<15}{spec.get('operands', '?'):>2}  "
              f"{spec.get('symbol', ''):<9}{mark}")
    if want and not any(want in n.lower() for n in ops):
        print(f"  (no operator matching {args.sub!r})")
        return 1
    return 0


def cmd_explain(args) -> int:
    key = args.name.strip().lower()
    if key in ("op", "ops"):
        return _explain_ops(args)

    hit = _INSTR.get(key)

    def _mnemonic_of(c) -> str:
        """First token of the documented format, lowercased.

        Matching on the format rather than on the title matters, because some
        titles begin with a *different* instruction: `ucontrol getBlock ...`
        would otherwise be listed as documentation for `getblock`.
        """
        toks = (c.get("format") or "").strip().split()
        return toks[0].lower() if toks else ""

    docs = [c for c in CMD_INDEX if _mnemonic_of(c) == key]
    if not docs:                       # some sections have no format line
        docs = [c for c in CMD_INDEX
                if (c.get("title") or "").strip().lower().startswith(key)]
    if args.sub:
        s = args.sub.lower()
        docs = [c for c in docs if s in (c.get("title") or "").lower()
                or s in (c.get("format") or "").lower()]
    if hit is None and not docs:
        print(f"unknown instruction {args.name!r}", file=sys.stderr)
        print(f"  try: python mlog.py cmd {key}", file=sys.stderr)
        return 1

    print(f"=== {args.name}")
    params: list = []
    if hit:
        canon, spec = hit
        params = spec.get("params") or []
        arity = spec.get("arity")
        _kv("arity", f"{arity}   (statement indices run 1..{arity}; "
                     f"index 0 is the mnemonic itself)")
        _kv("text", " ".join([canon] + [f"<{p}>" for p in params]))
        if spec.get("min_build"):
            note = spec.get("min_build_note")
            extra = f"   ({note})" if note else ""
            _kv("since build", f"{spec['min_build']}   (vanilla){extra}")
        elif spec.get("min_build_note"):
            _kv("version", f"?   ({spec['min_build_note']})")
        toks = spec.get("tokens") or {}
        for i, p in enumerate(params):
            tok = toks.get(str(i))
            tail = f"    [fixed token: {tok}]" if tok else ""
            print(f"  {'params' if i == 0 else '':<13} {i}  {p:<12}{tail}")
        _kv("writes", _slots(spec.get("defs") or [], params))
        _kv("reads", _slots(spec.get("uses") or [], params))
        if spec.get("maybe_uses"):
            _kv("maybe reads", _slots(spec["maybe_uses"], params))
        if spec.get("defs_conditional"):
            _kv("cond. writes", _slots(spec["defs_conditional"], params))
        if spec.get("always_evaluated"):
            _kv("always eval.",
                "positions " + ", ".join(str(i) for i in
                                         spec["always_evaluated"]))
        if spec.get("note"):
            _kv("semantics", spec["note"])

        subs = spec.get("subcommands") or {}
        if subs:
            print(f"  {'subcommands':<13} {len(subs)}: {', '.join(subs)}")
            for nm, s in subs.items():
                if args.sub and args.sub.lower() != nm.lower():
                    continue
                # `uses` is what the executor converts; `semantic_uses` is what the
                # subcommand actually reads. `ucontrol move x y` reads two numbers
                # and is four fields long, so report the effective size as well.
                reads = s.get("semantic_uses") or s.get("uses") or []
                eff = 2 + len(s["args"]) if s.get("args") else None
                tail = f"   effective arity {eff}" if eff else ""
                print(f"  {'':<13} {nm:<10} reads {_slots(reads, params)}"
                      f"   writes {_slots(s.get('defs') or [], params)}{tail}")
                if s.get("note"):
                    print(textwrap.fill(s["note"], width=94,
                                        initial_indent=" " * 16,
                                        subsequent_indent=" " * 16))

    for c in docs:
        print(f"  {'docs':<13} {c.get('num', '?')}  {c.get('title', '')}"
              f"   (cmd_index.json:{c.get('line', '?')})")
        fmt = [l.strip() for l in (c.get("format") or "").split("\n") if l.strip()]
        for n, line in enumerate(fmt):
            print(f"  {'format' if n == 0 else '':<13} {line}")
        for pname, pdesc in (c.get("params") or []):
            print(textwrap.fill(f"{pname} : {pdesc}", width=94,
                                initial_indent=" " * 18,
                                subsequent_indent=" " * 22))
        for note in (c.get("notes") or []):
            _bullet(note)

    for d in _DISAGREE:
        if (d.get("instruction") or "").lower() != key:
            continue
        print(f"  {'!! DOCS WRONG':<13} the doc above is not what the game parses")
        _kv("docs say", d.get("docs_format", "?"))
        _kv("actually", d.get("source_order", "?"))
        _kv("impact", d.get("impact", ""))
        _kv("evidence", d.get("evidence", ""))
    return 0


# --------------------------------------------------------------------------- #
# text: read what a message block (信息板) actually says
# --------------------------------------------------------------------------- #

_MARKUP = re.compile(r"\[[^\]]{1,24}\]")
# Mindustry's icon font lives in the Private Use Area; in a console it is
# unreadable noise, but stripping it silently would lose information -- so it is
# only removed by --plain, and reported by count.
_PUA = re.compile("[\ue000-\uf8ff]")
_STRING_BLOCKS = ("message", "world-message", "world-label")


def _plain(s: str) -> str:
    return _PUA.sub("", _MARKUP.sub("", s)).strip()


def cmd_text(args) -> int:
    files = list(args.files)
    rc = 0
    for path in files:
        try:
            sch = _load_schematic(path)
        except (SystemExit, OSError) as exc:
            print(str(exc) if not isinstance(exc, SystemExit) else exc,
                  file=sys.stderr)
            rc = 1
            continue
        hits = [t for t in sch.tiles
                if t.block in _STRING_BLOCKS and isinstance(t.config, str)]
        if not hits and not (args.all and sch.description):
            print(f"--- {path}: no message block")
            continue
        print("=" * 70)
        print(f"{path}  ({sch.name})")
        if args.all and sch.description:
            print("--- description ---")
            print(_plain(sch.description) if args.plain else sch.description)
        for t in hits:
            text = t.config or ""
            body = _plain(text) if args.plain else text
            icons = len(_PUA.findall(text))
            head = f"--- {t.block} @({t.x},{t.y})"
            if icons:
                head += f"  [{icons} icon(s) in the private-use area]"
            print(head)
            if args.strip:
                body = "\n".join(l for l in body.split("\n") if l.strip())
            print(body)
    return rc


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# map -- every logic processor inside a .msav save
# --------------------------------------------------------------------------- #

_IPT = {"micro-processor": 2, "logic-processor": 8,
        "hyper-processor": 25, "world-processor": 8,
        "world-cell": 8, "world-message": 8}


def cmd_map(args) -> int:
    """Pull each processor out of a save and run the same checks as `check`.

    A save is a zlib stream of framed regions; map_logic.py locates the map
    region by parsing it exactly to its own end instead of trusting the region
    order, then finds each processor by scanning its building payload for a
    deflate blob that validates as the logic config format.
    """
    import map_logic as _map

    rc = 0
    total_proc = total_lines = total_errs = 0
    for path in args.saves:
        try:
            info = _map.read_save(path)
        except Exception as exc:
            print(f"{path}: FAILED -- {type(exc).__name__}: {exc}")
            rc = 1
            continue
        tiles = info["logic"]
        print(f"{path}  v{info['version']}  {info['width']}x{info['height']}  "
              f"{len(tiles)} logic processor(s)")
        if not tiles:
            print("  (no logic in this save)")
            continue
        for t in tiles:
            code = t["code"]
            where = f"({t['x']},{t['y']})"
            if code is None:
                print(f"  !! {where} {t['block']}: {t.get('error', 'unreadable')}")
                rc = 1
                continue
            lines = code.split("\n")
            names = [l["name"] for l in t["links"]]
            # per-block instruction budget: the guard-latency rule needs the real
            # rate, and a micro-processor (2/tick) is the slow case
            findings = _check.analyse(lines, names, _IPT.get(t["block"], 2))
            errs = [f for f in findings if f.sev in ("ERR", "SUSPECT")]
            warns = [f for f in findings if f.sev == "WARN"]
            stmts = _check.prepare(lines)
            total_proc += 1
            total_lines += len(stmts)
            total_errs += len(errs)
            head = (f"  {where} {t['block']:<15} {len(stmts):>5} stmt  "
                    f"{len(t['links'])} link")
            if errs or warns:
                print(f"{head}  {len(errs)} error/suspect, {len(warns)} warn")
            elif args.verbose:
                print(f"{head}  clean")
            for f in (errs + warns)[: args.show]:
                print(f"        L{f.line} [{f.sev}] {f.code}: {f.msg}")
            if args.code:
                for n, line in enumerate(lines[: args.code], 1):
                    print(f"        {n:>4} {line}")
        print()

    if len(args.saves) > 1 or total_proc:
        print(f"{total_proc} processor(s), {total_lines} statement(s), "
              f"{total_errs} error/suspect")
    return rc


def _tool(name: str):
    return importlib.import_module(name)


def cmd_sim(args) -> int:
    return _tool("mlogsim").main(args.sim_args)


def cmd_ranges(args) -> int:
    return _tool("mlogrange").cmd_ranges(args.file, args.var)


def cmd_jumps(args) -> int:
    return _tool("mlogrange").cmd_jumps(args.file, args.max, args.var)


def cmd_post(args) -> int:
    import aigenerated
    from pathlib import Path
    if args.output and len(args.files) > 1:
        sys.exit("-o takes a single file")
    out = Path(args.output) if args.output else None
    for f in args.files:
        print(aigenerated.post(Path(f), args.author or aigenerated.DEFAULT_AUTHOR, out))
    return 0


def cmd_labelize(args) -> int:
    argv = [args.file, "--prefix", args.prefix]
    if args.output:
        argv += ["-o", args.output]
    if args.check:
        argv.append("--check")
    if args.sign:
        argv.append("--sign")
    return _tool("mlog_labelize").main(argv)


def cmd_doctor(_args) -> int:
    """First-run orientation: environment, entry point, five commands."""
    import shutil

    root = Path(HERE).parent
    jar = root / "out" / "_server160.jar"
    jre = root / "out" / "jre21" / "jdk-21.0.5+11-jre" / "bin" / "java.exe"
    py = "%d.%d.%d" % sys.version_info[:3]
    ok = lambda b: "yes" if b else "no"
    print("== mlog doctor ==")
    print("repo root : %s" % root)
    print("entry     : %s   (run from the repo root; do NOT cd into .dsh/skills)" % (root / "tools" / "mlog.py"))
    print("python    : %s" % py)
    print("stdout    : utf-8 (safe to read directly; do not redirect to a file)")
    print("server jar: %s  %s" % (ok(jar.exists()), jar))
    print("jre21     : %s  %s" % (ok(jre.exists()), jre))
    jpath = shutil.which("java")
    print("java PATH : %s  %s%s" % (ok(jpath), jpath or "-",
          "  (Java 8 stub -- mlogsim uses the jre21 below)" if jpath else ""))
    print()
    print("== five commands (repo root) ==")
    print("  python tools/mlog.py check  Logic/foo.mlog --cost")
    print("  python tools/mlog.py ir     Logic/foo.mlog")
    print("  python tools/mlog.py values Logic/foo.mlog")
    print("  python tools/mlog.py gen    --source Logic/foo.mlog --name foo")
    print("  python tools/mlogsim.py     Logic/foo.mlog --ticks 120 --update all")
    print()
    print("tips: multi-file sim -> add --daemon once, then --remote FILE;")
    print("      sim needs Java 21 + out/_server160.jar (set MLOGSIM_JAVA/MLOGSIM_JAR);")
    print("      docs: mindustry-blueprint/SKILL.md (start at '60 秒上手').")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="mlog", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=False)

    p = sub.add_parser("doctor", help="environment + first-run cheat sheet (start here)")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("parse", help="dump a .msch blueprint")
    p.add_argument("file")
    p.add_argument("--plain", action="store_true",
                   help="strip [colour] markup and private-use icon glyphs")
    p.add_argument("--strip", action="store_true",
                   help="drop blank lines from message text")
    p.add_argument("--no-text", action="store_true",
                   help="do not print message-block (信息板) contents")
    p.set_defaults(fn=cmd_parse)

    p = sub.add_parser("text", help="read message-block (信息板) contents")
    p.add_argument("files", nargs="+")
    p.add_argument("--plain", action="store_true",
                   help="strip [colour] markup and private-use icon glyphs")
    p.add_argument("--strip", action="store_true",
                   help="drop blank lines (markup kept unless --plain)")
    p.add_argument("--all", action="store_true",
                   help="also print the blueprint description")
    p.set_defaults(fn=cmd_text)

    p = sub.add_parser("gen", help="build a blueprint / paste code from mlog")
    p.add_argument("source", nargs="?", help=".mlog file (default: stdin)")
    p.add_argument("--logic", help="inline mlog code")
    p.add_argument("--name", default="blueprint")
    p.add_argument("--description", default="")
    p.add_argument("--label", action="append", default=[])
    p.add_argument("--block", default="micro-processor")
    p.add_argument("--at", default="0,0",
                   help="processor center in the input frame; the emitted "
                        "schematic is normalised to (0,0)")
    p.add_argument("--abs", action="store_true",
                   help="--link/--with-block coordinates are absolute map tiles "
                        "(same frame as --at), not offsets from the processor")
    p.add_argument("--rotation", type=int, default=0)
    p.add_argument("--link", action="append", default=[],
                   help="name@dx,dy (relative to the processor)")
    p.add_argument("--with-block", action="append", default=[],
                   help="block@dx,dy[=contentId] -- dx,dy is the block CENTER, "
                        "relative to the processor (same convention as --link); "
                        "also places the link target")
    p.add_argument("--auto-link", action="store_true",
                   help="report the game link-name for each --with-block")
    p.add_argument("--no-sign", action="store_true",
                   help="do not auto-attach the @aigenerated signature")
    p.add_argument("--author", default=_sig.DEFAULT_AUTHOR,
                   help="signature author (default: 猪猪鱼)")
    p.add_argument("--out", help="write the paste code (base64) to this file")
    p.add_argument("--binary", help="write a .msch binary to this path")
    p.add_argument("--quiet", action="store_true", help="do not print the paste code")
    p.set_defaults(fn=cmd_gen)

    p = sub.add_parser("check", help="static analysis of mlog")
    p.add_argument("files", nargs="*", help=".mlog files")
    p.add_argument("--msch", action="append", default=[],
                   help="also check logic inside a .msch")
    p.add_argument("--cost", action="store_true",
                   help="also report statement count, unreachable code, loop "
                        "bounds and worst-case instruction cost per cycle")
    p.add_argument("--rid", action="store_true",
                   help="tag each finding with the rule id from rules/*.json "
                        "that claims it (blank when no rule owns the message)")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("ir", help="structural view: dead code, dead stores")
    p.add_argument("files", nargs="*", metavar="logic.mlog")
    p.add_argument("--msch", action="append", default=[],
                   help="take the logic from a .msch instead")
    p.add_argument("--all", action="store_true",
                   help="also list forced output slots and literal "
                        "declarations (noisy: ~1.2 per block)")
    p.set_defaults(fn=cmd_ir)

    p = sub.add_parser("values",
                       help="abstract values: constant branches, provable nulls")
    p.add_argument("files", nargs="*", metavar="logic.mlog")
    p.add_argument("--msch", action="append", default=[],
                   help="take the logic from a .msch instead")
    p.add_argument("--trace", action="append", metavar="VAR",
                   help="trace one variable (repeatable)")
    p.set_defaults(fn=cmd_values)

    p = sub.add_parser("cross",
                       help="cross-processor read/write: what the reader really gets")
    p.add_argument("files", nargs="*", metavar="blueprint.msch")
    p.add_argument("--msch", action="append", default=[],
                   help="same, spelled explicitly")
    p.set_defaults(fn=cmd_cross)

    p = sub.add_parser("pack",
                       help="packed configs: field layout + how many ids fit")
    p.add_argument("files", nargs="*", metavar="logic.mlog")
    p.add_argument("--msch", action="append", default=[],
                   help="take the logic from a .msch instead")
    p.set_defaults(fn=cmd_pack)

    p = sub.add_parser("id", help="look up block content ids / sizes")
    p.add_argument("name", nargs="?", help="substring; omit to list all")
    p.set_defaults(fn=cmd_id)

    p = sub.add_parser("sensor", help="what @property names sensor can read")
    p.add_argument("pattern", nargs="?", help="substring of a name or meaning")
    p.add_argument("--settable", action="store_true",
                   help="only the ones `control` can also write")
    p.add_argument("--on", choices=["building", "unit", "content", "any"],
                   help="only properties that read from this kind of target")
    p.set_defaults(fn=cmd_sensor)
    p = sub.add_parser("item", help="item content ids; decode packed configs")
    p.add_argument("name", nargs="?", help="name or id; omit to list all")
    p.add_argument("--unpack", metavar="N",
                   help="decode status*1e8 + outputID*1e6 + inputMask")
    p.set_defaults(fn=cmd_item)

    p = sub.add_parser("unit", help="unit stats: speed, hp, payload, weapons")
    p.add_argument("name", nargs="?", help="substring; omit to list all")
    p.set_defaults(fn=cmd_unit)

    p = sub.add_parser("block", help="block behaviour: recipe, capacity, power")
    p.add_argument("name", nargs="?", help="substring; omit to list all")
    p.add_argument("--accepts", metavar="ITEM",
                   help="list blocks that accept this item (any / routed / recipe)")
    p.set_defaults(fn=cmd_block)

    p = sub.add_parser("find", help="find corpus blueprints that solve a job")
    p.add_argument("query", help="words to look for, e.g. 'wireless overdrive'")
    p.add_argument("--corpus", help="schematic folder (default: game save dir)")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--max-stmts", type=int, default=5000,
                   help="skip blocks longer than this (generated art); 0 = no cap")
    p.add_argument("--max-bytes", type=int, default=2_000_000,
                   help="skip blueprint files larger than this (pixel art)")
    p.add_argument("--rebuild", action="store_true",
                   help="ignore the search cache and re-read every blueprint")
    p.set_defaults(fn=cmd_find)

    p = sub.add_parser("style", help="how the corpus writes this job: naming, "
                                    "shape, cycle cost")
    p.add_argument("query", nargs="+",
                   help="task words, e.g. `thorium reactor` (no quotes needed)")
    p.add_argument("--corpus", help="schematic folder (default: the game's)")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--max-stmts", type=int, default=5000)
    p.add_argument("--max-bytes", type=int, default=2_000_000)
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--code", type=int, default=0,
                   help="also print the first N source lines")
    p.set_defaults(fn=cmd_style)

    p = sub.add_parser("map", help="every logic processor inside a .msav save")
    p.add_argument("saves", nargs="+", help=".msav save file(s)")
    p.add_argument("--code", type=int, default=0,
                   help="also print the first N source lines of each processor")
    p.add_argument("--show", type=int, default=6,
                   help="how many findings to print per processor")
    p.add_argument("--verbose", action="store_true",
                   help="also list processors with no findings")
    p.set_defaults(fn=cmd_map)

    p = sub.add_parser("cmd", help="mlog instruction reference")
    p.add_argument("pattern", nargs="?", help="substring filter")
    p.set_defaults(fn=cmd_cmd)

    p = sub.add_parser("explain", help="one instruction: syntax + what it reads/writes")
    p.add_argument("name", help="mnemonic, e.g. ulocate | op | select | getblock")
    p.add_argument("sub", nargs="?",
                   help="subcommand or operator filter, e.g. `explain ulocate ore`")
    p.set_defaults(fn=cmd_explain)

    p = sub.add_parser("selftest", help="round-trip every blueprint in the game dir")
    p.add_argument("--jobs", type=int, default=None,
                   help="worker processes (default: min(8, cpu_count); 1 = serial)")
    p.set_defaults(fn=lambda a: _gen.selftest(jobs=a.jobs))

    p = sub.add_parser("sim", help="run logic on the real executor (forwards to mlogsim.py)")
    p.add_argument("sim_args", nargs=argparse.REMAINDER,
                   help="everything after `sim` goes to mlogsim.py, e.g. --ticks 120 x.mlog")
    p.set_defaults(fn=cmd_sim)

    p = sub.add_parser("ranges", help="possible value range of each variable per statement")
    p.add_argument("file")
    p.add_argument("--var")
    p.set_defaults(fn=cmd_ranges)

    p = sub.add_parser("jumps", help="expand `op add @counter ...` into a jump table")
    p.add_argument("file")
    p.add_argument("--var")
    p.add_argument("--max", type=int, default=64)
    p.set_defaults(fn=cmd_jumps)

    p = sub.add_parser("post", help="labels -> numbers, then apply the hidden signature")
    p.add_argument("files", nargs="+")
    p.add_argument("-o", "--output")
    p.add_argument("--author", default=None)
    p.set_defaults(fn=cmd_post)

    p = sub.add_parser("labelize", help="numeric jump targets -> jump labels (for editing)")
    p.add_argument("file")
    p.add_argument("-o", "--output")
    p.add_argument("--prefix", default="L")
    p.add_argument("--check", action="store_true")
    p.add_argument("--sign", action="store_true")
    p.set_defaults(fn=cmd_labelize)

    args = ap.parse_args()
    if not getattr(args, "cmd", None):
        return cmd_doctor(args)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
