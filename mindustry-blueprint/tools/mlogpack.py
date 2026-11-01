#!/usr/bin/env python
"""
mlogpack.py -- recognise *packed config* encode/decode idioms in mlog.

Why this exists
---------------
A whole family of real blueprints stores several fields in one number:

    data = status * 1e8 + outputID * 1e6 + inputMask

and then peels it apart again with `op idiv` / `op mod`.  Nothing in the
token stream says "this is a packed struct" -- but the *shape* is
unmistakable once you look for decimal power-of-ten scaling, and the shape
carries hard limits that silently corrupt data when exceeded:

    mask < 1e6   =>  mask can be at most 999999
                  =>  highest usable bit index is 19
                  =>  only item/block ids 0..19 fit in the low field

Vanilla has 22 items (0..21), so `fissile-matter` (20) and
`dormant-cyst` (21) already do NOT fit -- an id of 20 silently lands in
the `outputID` field instead.  That is the kind of bug nobody notices
until a factory builds the wrong thing.

What this module does NOT do
----------------------------
It does not reconstruct expressions, so it cannot tell whether the packed
value was assembled correctly.  It recognises the *decode/encode sites*
and reports the field layout and its capacity.  That is enough to answer
"how many ids can this hold" and "do these two fields overlap".

Usage
-----
    import mlogir, mlogpack
    prog = mlogir.parse(source)
    print(mlogpack.format_report(prog, item_count=22))
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import mlogir

# A divisor/scale worth calling a "field separator".  10^2 (100) is too
# small to be a packed struct in practice and produces noise; 10^3 is the
# smallest one that shows up in real blueprints.
MIN_EXP = 3


def _text(tok) -> str:
    return getattr(tok, "text", tok)


def _num(tok) -> float | None:
    """Literal value of a token, or None if it is a variable."""
    if getattr(tok, "kind", None) != mlogir.KIND_NUM:
        return None
    try:
        v = float(_text(tok))
    except (TypeError, ValueError):
        return None
    return v


def _power_of_ten(v: float | None) -> int | None:
    """Return k if v == 10**k (k >= MIN_EXP), else None."""
    if v is None or v <= 0:
        return None
    k = round(math.log10(v))
    if k < MIN_EXP:
        return None
    if abs(10.0 ** k - v) > 1e-9 * max(1.0, v):
        return None
    return int(k)


@dataclass
class Field:
    """One `op idiv|mod <dst> <src> 10**exp` site."""
    kind: str          # "idiv" | "mod"
    exp: int
    dst: str
    line: int

    def describe(self) -> str:
        if self.kind == "idiv":
            return f"1e{self.exp}  ->  {self.dst}   (= data / 1e{self.exp})"
        return f"1e{self.exp}  ->  {self.dst}   (= data % 1e{self.exp})"


@dataclass
class PackSite:
    src: str
    fields: list[Field] = field(default_factory=list)

    # --- derived, filled by _finish() ---
    low_exp: int | None = None
    capacity_bits: int | None = None   # usable bit positions in the low field
    overlaps: list[tuple[str, str, str]] = field(default_factory=list)
    masked: list[tuple[str, str, str]] = field(default_factory=list)

    def _finish(self, prog: "mlogir.Program | None" = None) -> None:
        exps = sorted({f.exp for f in self.fields})
        # The low field's width is set by the smallest separator in use.
        self.low_exp = exps[0] if exps else None
        if self.low_exp is not None:
            modulus = 10 ** self.low_exp
            # max low value is modulus-1; how many bit positions does it need?
            self.capacity_bits = (modulus - 1).bit_length()
        # `op idiv out src 1e6` does NOT mask off the 1e8 field: out also
        # carries 100 * status.  Real programs therefore either guard the
        # whole decode with `if status == 0`, or subtract the high field
        # first (`op mul t hi <1eN>` / `op sub t src t`).  Only the first kind
        # of program gets a warning -- reporting the second kind was a false
        # positive, and pairing every idiv with every other idiv (including
        # a copy of itself, since a trampoline variable is reused) produced
        # outright nonsense like "`flag-` also contains 1x `flag-`".
        # So: one representative per *distinct* exponent, and no complaint
        # when the subtract-mask idiom is present.
        seen: dict[int, Field] = {}
        for f in sorted((f for f in self.fields if f.kind == "idiv"),
                        key=lambda f: (f.exp, f.line)):
            seen.setdefault(f.exp, f)
        reps = [seen[e] for e in sorted(seen)]
        for i, lo in enumerate(reps):
            for hi in reps[i + 1:]:
                where = _subtract_mask(prog, self.src, hi)
                if where is not None:
                    self.masked.append((
                        lo.dst, hi.dst,
                        f"`{hi.dst}` removed from `{self.src}` first "
                        f"(subtract-mask at L{where}) -- decode is correct",
                    ))
                    continue
                factor = 10 ** (hi.exp - lo.exp)
                self.overlaps.append((
                    lo.dst, hi.dst,
                    f"`{lo.dst}` also contains {factor}x `{hi.dst}`; "
                    f"a correct decode must first assert `{hi.dst} == 0`",
                ))

    @property
    def max_id(self) -> int | None:
        return None if self.capacity_bits is None else self.capacity_bits - 1


def _subtract_mask(prog: "mlogir.Program | None", src: str,
                   hi: Field) -> int | None:
    """Detect `op mul t hi 1eN` + `op sub u src t` -- the correct decode.

    `hi.dst` holds `src / 10**hi.exp`, so multiplying it back by
    `10**hi.exp` reconstructs the high field's contribution, and subtracting
    that from `src` leaves the low field alone.  Programs that do this are
    fine and must not be warned about; the first full corpus run produced
    exactly that false positive.

    Returns the line of the `op mul`, or None when the idiom is absent.
    """
    if prog is None:
        return None
    weight = float(10 ** hi.exp)
    for s in prog.stmts:
        if s.mnemonic != "op" or len(s.args) < 4:
            continue
        if _text(s.args[0]).lower() != "mul":
            continue
        if _text(s.args[2]) != hi.dst:
            continue
        scale = _num(s.args[3])
        if scale is None or abs(scale - weight) > 1e-9 * max(1.0, weight):
            continue
        t = _text(s.args[1])
        for u in prog.stmts:
            if u.mnemonic != "op" or len(u.args) < 4:
                continue
            if _text(u.args[0]).lower() != "sub":
                continue
            a, b = _text(u.args[2]), _text(u.args[3])
            if (a == src and b == t) or (a == t and b == src):
                return s.line
    return None


@dataclass
class BitPeel:
    """The `x & -x` / log2(lowbit) / `x -= lowbit` set-iteration idiom."""
    line: int
    index: str          # variable that receives the bit index
    value: str          # variable being consumed
    lowbit: str         # the isolated lowest set bit
    base: float | None  # the log base (2 for bits)


def find_pack_sites(prog: mlogir.Program) -> list[PackSite]:
    """Group idiv/mod-with-decimal-scale sites by the value they peel."""
    by_src: dict[str, PackSite] = {}
    for s in prog.stmts:
        if s.mnemonic != "op" or len(s.args) < 4:
            continue
        op = _text(s.args[0]).lower()
        if op not in ("idiv", "mod"):
            continue
        dst, src, scale = _text(s.args[1]), _text(s.args[2]), s.args[3]
        exp = _power_of_ten(_num(scale))
        if exp is None:
            continue
        site = by_src.setdefault(src, PackSite(src=src))
        site.fields.append(Field(kind=op, exp=exp, dst=dst, line=s.line))
    sites = []
    for site in by_src.values():
        # One separator alone is not a struct -- it is just a division.
        if len({f.exp for f in site.fields}) < 2:
            continue
        site._finish(prog)
        sites.append(site)
    sites.sort(key=lambda p: p.fields[0].line)
    return sites


def find_bit_peels(prog: mlogir.Program) -> list[BitPeel]:
    """Find `op logn <idx> <low> 2` and locate the `and` that made <low>."""
    found: list[BitPeel] = []
    for s in prog.stmts:
        if s.mnemonic != "op" or len(s.args) < 4:
            continue
        if _text(s.args[0]).lower() != "logn":
            continue
        base = _num(s.args[3])
        if base is None or abs(base - 2.0) > 1e-9:
            continue
        idx, low = _text(s.args[1]), _text(s.args[2])
        # The lowbit is normally produced a line or two earlier by `and`.
        for prev in reversed(prog.stmts[:s.index]):
            if s.index - prev.index > 6:
                break
            if prev.mnemonic != "op" or len(prev.args) < 4:
                continue
            if _text(prev.args[0]).lower() != "and":
                continue
            if _text(prev.args[1]) != low:
                continue
            found.append(BitPeel(line=s.line, index=idx,
                                 value=_text(prev.args[2]), lowbit=low,
                                 base=base))
            break
    return found


def _capacity_note(site: PackSite, item_count: int | None) -> list[str]:
    out: list[str] = []
    if site.capacity_bits is None:
        return out
    out.append(f"low field `1e{site.low_exp}` holds 0..{10 ** site.low_exp - 1}"
               f"  =>  {site.capacity_bits} usable bit positions"
               f" (ids 0..{site.max_id})")
    if item_count is not None and item_count > site.capacity_bits:
        out.append(f"!! {item_count} items exist but only {site.capacity_bits} "
                   f"fit: ids {site.capacity_bits}..{item_count - 1} overflow "
                   f"into the next field (raise the separator, e.g. 1e6 -> 1e7)")
    return out


def format_report(prog: mlogir.Program, item_count: int | None = None,
                  label: str = "") -> str:
    sites = find_pack_sites(prog)
    peels = find_bit_peels(prog)
    lines: list[str] = []
    if label:
        lines.append(f"=== {label}")
    lines.append(f"  packed configs  : {len(sites)}")
    for site in sites:
        lines.append(f"    `{site.src}`  (first seen L{site.fields[0].line})")
        for f in sorted(site.fields, key=lambda f: -f.exp):
            lines.append(f"      {f.describe()}   at L{f.line}")
        for note in _capacity_note(site, item_count):
            lines.append(f"      {note}")
        for lo, hi, text in site.masked:
            lines.append(f"      ok: {text}")
        for lo, hi, text in site.overlaps:
            lines.append(f"      note: {text}")
    lines.append(f"  bit peeling     : {len(peels)}")
    for p in peels:
        lines.append(f"    L{p.line}  `{p.index}` = log2(`{p.lowbit}`)"
                     f"  consuming `{p.value}` in base {p.base:g}")
    if not sites and not peels:
        lines.append("  (no packed-config idiom found -- this program does not "
                     "pack multiple fields into one number)")
    return "\n".join(lines)
