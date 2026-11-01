"""Cross-processor memory: what `read x P "name"` actually sees.

`read`/`write` address a *processor's variables by name*, so two programs can
share a slot:

    set  count 42               # processor A declares its own variable
    read n     proc1 "count"    # processor B reads A's variable

Note there is no `write` in that example: the readable value of `(P, name)` is
P's **own** variable, which is exactly why the game requires it to be declared
there first (`mlogcheck` warns when a read names a slot that processor never
assigns). An explicit `write <value> <link> "<name>"` from somewhere else adds
another possible value for that slot.

Processors run at different times, so the honest answer is the join over
everything that can store into the slot. This module computes it and grades each
read:

  * `never assigned` -- the target processor never assigns that name (always null)
  * `always null`    -- every writer stores null
  * `constant`       -- every writer stores the same value (branch folding
                        becomes possible; informational)
  * `written, never read` -- an explicit `write` nobody reads
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import mlogir as _ir
import mlogval as _val

CROSS = re.compile(r'^(read|write)\s+(\S+)\s+(\S+)\s+"([^"]*)"\s*$')


@dataclass
class Unit:
    """One processor: its program plus its links resolved to absolute tiles."""
    label: str
    pos: tuple[int, int]
    code: list[str]
    links: dict[str, tuple[int, int]] = field(default_factory=dict)


@dataclass
class Read:
    unit: str
    line: int
    name: str
    target: tuple[int, int]
    local: str = ""


@dataclass
class Slot:
    target: tuple[int, int]
    name: str
    own: "_val.V | None" = None          # the target's own variable, if assigned
    writes: list[tuple[str, int, "_val.V"]] = field(default_factory=list)
    readers: list[Read] = field(default_factory=list)

    def incoming(self) -> "_val.V | None":
        vals = ([self.own] if self.own is not None else []) + [v for _, _, v in self.writes]
        if not vals:
            return None
        v = vals[0]
        for w in vals[1:]:
            v = _val.join(v, w)
        return v


@dataclass
class Report:
    slots: dict[tuple[tuple[int, int], str], Slot] = field(default_factory=dict)
    cross_reads: int = 0
    never_written: list[Read] = field(default_factory=list)
    always_null: list[Read] = field(default_factory=list)
    exact: list[tuple[Read, str]] = field(default_factory=list)
    dead_writes: list[Slot] = field(default_factory=list)
    unresolved: int = 0


def _own_values(prog: "_ir.Program", state: "_val.Analysis") -> dict[str, "_val.V"]:
    """Join over every assignment to each local variable.

    A reader can sample this program at any point in its loop, so the join is
    the only sound single value; if every assignment writes the same constant
    the join stays exact.
    """
    own: dict[str, _val.V] = {}
    for i, st in enumerate(prog.stmts):
        after = state.after.get(i) or {}
        for d in st.defs:
            if d.startswith("@"):
                continue
            v = after.get(d, _val.TOP_V)
            own[d] = v if d not in own else _val.join(own[d], v)
    return own


def _consumed(prog: "_ir.Program") -> set[str]:
    """Names the program actually reads.

    This matters for `dead-write`: a slot written from outside is *live* when
    the target uses the variable directly (`print count`), not only when
    somebody has an explicit `read ... "count"`. Counting only `read` statements
    turned every externally-fed variable into a false "written, never read".
    """
    seen: set[str] = set()
    for st in prog.stmts:
        seen |= {u for u in st.uses if not u.startswith("@")}
    return seen


def analyze_units(units: list[Unit]) -> Report:
    rep = Report()
    by_pos = {u.pos: u for u in units}
    progs: dict[tuple[int, int], "_ir.Program"] = {}
    state: dict[tuple[int, int], "_val.Analysis"] = {}
    consumed: dict[tuple[int, int], set[str]] = {}
    for u in units:
        prog = _ir.parse(u.code)
        progs[u.pos] = prog
        state[u.pos] = _val.analyze(prog)
        consumed[u.pos] = _consumed(prog)

    # every processor publishes its own variables; that is what `read` fetches
    for u in units:
        for name, v in _own_values(progs[u.pos], state[u.pos]).items():
            slot = rep.slots.setdefault((u.pos, name), Slot(u.pos, name))
            slot.own = v

    for u in units:
        prog = progs[u.pos]
        after = state[u.pos].after
        for i, st in enumerate(prog.stmts):
            m = CROSS.match(st.raw)
            if not m:
                continue
            kind, value, link, name = m.groups()
            pos = u.links.get(link)
            if pos is None or pos not in by_pos:
                rep.unresolved += 1
                continue
            slot = rep.slots.setdefault((pos, name), Slot(pos, name))
            if kind == "write":
                slot.writes.append((u.label, st.line,
                                    (after.get(i) or {}).get(value, _val.TOP_V)))
            else:
                slot.readers.append(Read(u.label, st.line, name, pos, value))

    for slot in rep.slots.values():
        for r in slot.readers:
            rep.cross_reads += 1
            v = slot.incoming()
            if v is None:
                rep.never_written.append(r)
            elif v.kind == "na":
                rep.always_null.append(r)
            elif v.exact() is not None:
                rep.exact.append((r, str(v)))
        if (slot.writes and not slot.readers
                and slot.name not in consumed.get(slot.target, set())):
            rep.dead_writes.append(slot)
    return rep


def units_from_schematic(sch) -> list[Unit]:
    """Resolve every logic block's links into absolute tile coordinates.

    Only links that land on *another logic block* are kept: a link to a memory
    cell or a display is a different namespace (`read x cell1 0` is a cell slot,
    not a variable name), and mixing them would invent cross-processor edges.
    """
    logic_pos = {(t.x, t.y) for t in sch.logic_tiles()}
    units: list[Unit] = []
    for t in sch.logic_tiles():
        if t.logic is None:
            continue
        links = {}
        for l in t.logic.links:
            pos = (t.x + l.x, t.y + l.y)
            if pos in logic_pos:
                links[l.name] = pos
        units.append(Unit(
            label=f"{t.block} @({t.x},{t.y})",
            pos=(t.x, t.y),
            code=t.logic.code.split("\n"),
            links=links))
    return units


def slot_values(rep: "Report",
                units: "list[Unit]") -> dict[tuple[int, int], dict[tuple[str, str], "_val.V"]]:
    """Per-reader map `(link name, slot name) -> value`, for `mlogval`.

    This is the bridge that makes `values` useful on a multi-processor map: a
    `read` of a slot the other processor assigns a constant to is no longer an
    unknown writer.

    A slot nobody ever assigns resolves to **null**, not 0, because
    `LogicBlock.read()` falls back to `output.setobj(optionalLink(varName))`
    when `optionalVar()` returns null -- an undeclared name is a *link* lookup,
    and there is no such link. That is also why `write` to an undeclared name
    silently does nothing (`at == null -> return`).

    Keyed by the reader's **tile position**, not its label: labels are display
    strings, and coupling analysis to their format is how the first wiring of
    this silently returned nothing.
    """
    by_label = {u.label: u for u in units}
    out: dict[tuple[int, int], dict[tuple[str, str], "_val.V"]] = {}
    for (pos, name), slot in rep.slots.items():
        value = slot.incoming()
        known = value if value is not None else _val.NA
        for r in slot.readers:
            u = by_label.get(r.unit)
            if u is None:
                continue
            link = next((t for t, p in u.links.items() if p == pos), None)
            if link is None:
                continue
            out.setdefault(u.pos, {})[(link, name)] = known
    return out


def format_report(rep: Report, label: str = "") -> str:
    out = [f"=== {label}" if label else "==="]
    out.append(f"  cross reads     : {rep.cross_reads}   "
               f"(slots: {len(rep.slots)}, links that are not processors: "
               f"{rep.unresolved})")
    if rep.cross_reads or any(s.writes for s in rep.slots.values()):
        out.append("  note            : name-addressing a processor or a message "
                   "block is a v7 (build 146) feature -- this blueprint cannot "
                   "run on v6")
    if not rep.cross_reads:
        out.append("  (no processor-to-processor read found)")
    if rep.never_written:
        out.append(f"  !! never assigned : {len(rep.never_written)}")
        for r in rep.never_written[:10]:
            out.append(f"     {r.unit} L{r.line}: reads \"{r.name}\" from "
                       f"@({r.target[0]},{r.target[1]}), which never assigns it "
                       f"-- always null (fine as a 'no config yet' default; if a "
                       f"user is meant to create this slot, the spelling must "
                       f"match exactly)")
    if rep.always_null:
        out.append(f"  !! always null    : {len(rep.always_null)}")
        for r in rep.always_null[:10]:
            out.append(f"     {r.unit} L{r.line}: \"{r.name}\" from "
                       f"@({r.target[0]},{r.target[1]}) -- every writer stores null")
    if rep.exact:
        out.append(f"  constant          : {len(rep.exact)}")
        for r, s in rep.exact[:10]:
            out.append(f"     {r.unit} L{r.line}: \"{r.name}\" = {s}")
    if rep.dead_writes:
        out.append(f"  written, never read: {len(rep.dead_writes)}")
        for slot in rep.dead_writes[:10]:
            out.append(f"     \"{slot.name}\" on @({slot.target[0]},{slot.target[1]})"
                       f" -- {len(slot.writes)} writer(s), 0 reader(s)")
    return "\n".join(out)
