"""Abstract value analysis for mlog: ranges, exact constants, and provable null.

The whole point is a fact that a syntax checker cannot see: mlog variables are
either a *number* or an *object* (including null), and every arithmetic
instruction silently coerces. From `LVar.java`:

    public double num(){ return isobj ? objval != null ? 1 : 0
                                       : invalid(numval) ? 0 : numval; }
    public static boolean invalid(double d){ return Double.isNaN(d) || Double.isInfinite(d); }

so **null counts as 0** when used as a number, and a non-null object counts as 1.
And `setnum()` runs every numeric result through

    if(invalid(value)){ objval = null; isobj = true; }

i.e. **NaN and +-Infinity are silently converted to null**. That single line is
why the classic "strip one bit at a time" loop terminates:

    op sub twosComp 0 data          # -0 or -data
    op and lowbit data twosComp     # data & -data   (lowest set bit)
    op logn inputID lowbit 2        # log2 -> -Infinity when lowbit is 0
    op sub data data lowbit
    jump ... strictEqual inputID null

`logn(0,2) = -Infinity` -> null, and `strictEqual null null` is true. Nothing in
the token stream says "exit"; only the value domain does.

Scope: this is a *may* analysis with widening, so a reported range is a
superset of the truth. `PROVABLY NULL` and `resolved` branches are sound in the
other direction -- they only fire when the abstract result is exact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import mlogir

TOP = None  # sentinel below; real values are V instances


@dataclass(frozen=True)
class V:
    """Abstract value: a number range, a string, definite null, or top."""

    kind: str                 # "top" | "num" | "str" | "na"
    lo: float = 0.0
    hi: float = 0.0
    s: str = ""
    null: bool = False        # num/str: the value MAY also be null

    def exact(self) -> float | None:
        if self.kind == "num" and not self.null and self.lo == self.hi:
            return self.lo
        return None

    def __str__(self) -> str:
        if self.kind == "top":
            return "?"
        if self.kind == "na":
            return "null"
        if self.kind == "str":
            return f'"{self.s}"' + ("|null" if self.null else "")
        body = _fnum(self.lo) if self.lo == self.hi else f"{_fnum(self.lo)}..{_fnum(self.hi)}"
        return body + ("|null" if self.null else "")


TOP_V = V("top")
NA = V("na")
NUM0 = V("num", 0.0, 0.0)
NUM1 = V("num", 1.0, 1.0)


def num(lo: float, hi: float, null: bool = False) -> V:
    if lo > hi:
        lo, hi = hi, lo
    return V("num", lo, hi, null=null)


def _fnum(x: float) -> str:
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return f"{x:.4g}"


def _setnum(x: float) -> V:
    """What `LVar.setnum()` leaves behind: non-finite becomes null."""
    if math.isnan(x) or math.isinf(x):
        return NA
    return num(x, x)


# ---------------------------------------------------------------- lattice ----

def join(a: V, b: V) -> V:
    if a.kind == "top" or b.kind == "top":
        return TOP_V
    if a.kind == "na" and b.kind == "na":
        return NA
    if a.kind == "na":
        return V(b.kind, b.lo, b.hi, b.s, null=True) if b.kind != "na" else NA
    if b.kind == "na":
        return V(a.kind, a.lo, a.hi, a.s, null=True)
    if a.kind == "str" and b.kind == "str":
        if a.s == b.s:
            return V("str", s=a.s, null=a.null or b.null)
        return TOP_V
    if a.kind != b.kind:
        return TOP_V
    return num(min(a.lo, b.lo), max(a.hi, b.hi), a.null or b.null)


def _join_state(a: dict[str, V], b: dict[str, V]) -> dict[str, V]:
    out = dict(a)
    for k, vb in b.items():
        out[k] = join(out[k], vb) if k in out else vb
    return out


# ------------------------------------------------------- numeric projection ---

def as_num(v: V) -> tuple[float, float] | None:
    """Project to a numeric range the way `LVar.num()` would, or None for top.

    null -> 0, non-null object -> 1. This is the coercion that makes the
    bit-stripping loop terminate, so it has to be modelled, not ignored.
    """
    if v.kind == "top":
        return None
    if v.kind == "na":
        return (0.0, 0.0)
    if v.kind == "str":
        return (1.0, 1.0)          # any non-null object reads as 1
    if v.null:
        return (min(0.0, v.lo), max(0.0, v.hi))
    return (v.lo, v.hi)


# ------------------------------------------------------------ transfer ops ---

_BIN_EXACT = {
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mul": lambda a, b: a * b,
    "div": lambda a, b: a / b if b else math.copysign(math.inf, a) if a else math.nan,
    # Java: idiv = Math.floor(a / b) -- FLOOR, so -7 // 3 is -3 (not -2).
    "idiv": lambda a, b: float(math.floor(a / b)) if b else math.nan,
    # Java: mod = a % b -- C-style remainder (truncates toward zero), so -7 % 3 is -1.
    "mod": lambda a, b: math.fmod(a, b) if b else math.nan,
    # Java: emod = ((a % b) + b) % b -- floors as well, so -7 emod 3 is 2.
    # `idiv` and `mod` therefore disagree for mixed signs; `emod` is the one that
    # satisfies a == (a idiv b) * b + (a emod b). Do not "simplify" the two.
    "emod": lambda a, b: (math.fmod(a, b) + b) % b if b else math.nan,
    "pow": lambda a, b: _pow(a, b),
    "max": max,
    "min": min,
    "angle": lambda a, b: math.degrees(math.atan2(b, a)),
    "angleDiff": lambda a, b: (b - a + 180.0) % 360.0 - 180.0,
    "len": lambda a, b: math.hypot(a, b),
    "and": lambda a, b: float(_i(a) & _i(b)),
    "or": lambda a, b: float(_i(a) | _i(b)),
    "xor": lambda a, b: float(_i(a) ^ _i(b)),
    "shl": lambda a, b: float(_i(_i(a) << (_i(b) & 63))),
    "shr": lambda a, b: float(_i(a) >> (_i(b) & 63)),
    "ushr": lambda a, b: float((_i(a) & 0xFFFFFFFFFFFFFFFF) >> (_i(b) & 63)),
}

_UN_EXACT = {
    "abs": abs,
    "sign": lambda a: (a > 0) - (a < 0),
    "log": lambda a: math.log(a) if a > 0 else (-math.inf if a == 0 else math.nan),
    "log10": lambda a: math.log10(a) if a > 0 else (-math.inf if a == 0 else math.nan),
    "floor": lambda a: float(math.floor(a)),
    "ceil": lambda a: float(math.ceil(a)),
    "round": lambda a: float(math.floor(a + 0.5)),
    "sqrt": lambda a: math.sqrt(a) if a >= 0 else math.nan,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": lambda a: math.asin(a) if -1.0 <= a <= 1.0 else math.nan,
    "acos": lambda a: math.acos(a) if -1.0 <= a <= 1.0 else math.nan,
    "atan": math.atan,
    "not": lambda a: float(~_i(a)),
}

# `rand` and `noise` are deliberately absent from both tables. NaN is not a
# "don't know" sentinel here -- `setnum()` turns it into null, so modelling a
# non-deterministic op as NaN makes every `op rand` a *provably null*, which is
# a false positive on the single most common op in the corpus (1v1.msch alone
# produced 10 of them). Unknown is TOP, and TOP is what they get by falling
# through.

_CMP = {
    "equal": lambda a, b: a == b,
    "notEqual": lambda a, b: a != b,
    "lessThan": lambda a, b: a < b,
    "lessThanEq": lambda a, b: a <= b,
    "greaterThan": lambda a, b: a > b,
    "greaterThanEq": lambda a, b: a >= b,
    "land": lambda a, b: bool(_i(a)) and bool(_i(b)),
}

# monotone in each argument: safe to evaluate at the corners of a range
_MONOTONE_BIN = {"add", "sub", "mul", "min", "max", "len", "angle", "angleDiff",
                 "pow"}
_MONOTONE_UN = {"abs", "floor", "ceil", "round", "sign"}


def _i(x: float) -> int:
    try:
        return int(x)
    except (ValueError, OverflowError):
        return 0


def _pow(a: float, b: float) -> float:
    try:
        r = a ** b
    except (OverflowError, ZeroDivisionError):
        return math.inf
    if isinstance(r, complex):
        return math.nan
    return float(r)


def _float_or_none(x: V) -> float | None:
    """Exact float value, projecting null/objects the way `num()` does."""
    if x.kind == "na":
        return 0.0
    if x.kind == "str":
        return 1.0
    return x.exact()


# `_cond` hands us a lower-cased operator token; the tables are keyed by the
# real camelCase LogicOp names. Without this map `strictEqual` silently never
# matches and every null-check branch stays unresolved.
_CANON = {k.lower(): k for k in
          list(_BIN_EXACT) + list(_UN_EXACT) + list(_CMP)
          + ["logn", "strictEqual", "select"]}


def op_value(name: str, name_known: bool, a: V, b: V | None) -> V:
    """Result of `op <name> ...` after the `setnum()` normalisation."""
    name = _CANON.get((name or "").lower(), name or "")
    if not name_known:
        return TOP_V

    # exact/eaxct -- evaluate for real, then normalise non-finite to null
    if name in _BIN_EXACT:
        if b is None:
            return TOP_V
        fa, fb = _float_or_none(a), _float_or_none(b)
        if fa is not None and fb is not None:
            try:
                return _setnum(float(_BIN_EXACT[name](fa, fb)))
            except (ValueError, ZeroDivisionError, OverflowError):
                return NA
        ra, rb = as_num(a), as_num(b)
        if ra is None or rb is None:
            return TOP_V
        if name in _MONOTONE_BIN:
            cands = [float(_BIN_EXACT[name](x, y))
                     for x in (ra[0], ra[1]) for y in (rb[0], rb[1])]
            finite = [c for c in cands if not (math.isnan(c) or math.isinf(c))]
            if not finite:
                return NA
            if len(finite) != len(cands):
                return num(min(finite), max(finite), null=True)
            return num(min(finite), max(finite))
        return TOP_V

    if name in _UN_EXACT:
        if a.kind == "top":
            return TOP_V
        fa = _float_or_none(a)
        if fa is not None:
            try:
                return _setnum(float(_UN_EXACT[name](fa)))
            except (ValueError, ZeroDivisionError, OverflowError):
                return NA
        ra = as_num(a)
        if ra is None:
            return TOP_V
        if name in _MONOTONE_UN:
            cands = [float(_UN_EXACT[name](x)) for x in (ra[0], ra[1])]
            finite = [c for c in cands if not (math.isnan(c) or math.isinf(c))]
            if not finite:
                return NA
            if len(finite) != len(cands):
                return num(min(finite), max(finite), null=True)
            return num(min(finite), max(finite))
        return TOP_V

    if name == "logn":
        if b is None:
            return TOP_V
        fa, fb = _float_or_none(a), _float_or_none(b)
        if fa is not None and fb is not None:
            if fa > 0 and fb > 0 and fb != 1:
                return _setnum(math.log(fa) / math.log(fb))
            return NA                      # log(0) = -Inf, log(neg) = NaN
        ra = as_num(a)
        if ra is None:
            return TOP_V
        if ra[0] <= 0 <= ra[1]:
            rest = num(max(ra[0], 1e-300), ra[1], null=True) if ra[1] > 0 else NA
            return rest
        return TOP_V

    if name in _CMP:
        if b is None:
            return TOP_V
        fa, fb = _float_or_none(a), _float_or_none(b)
        if fa is not None and fb is not None:
            return NUM1 if _CMP[name](fa, fb) else NUM0
        ra, rb = as_num(a), as_num(b)
        if ra is not None and rb is not None and ra[1] < rb[0]:
            # every left value is below every right one
            return NUM1 if name in ("lessThan", "lessThanEq", "notEqual") else NUM0
        if ra is not None and rb is not None and ra[0] > rb[1]:
            return NUM0 if name in ("lessThan", "lessThanEq") else (
                NUM1 if name in ("greaterThan", "greaterThanEq", "notEqual") else NUM0)
        return TOP_V

    if name == "strictEqual":
        if b is None:
            return TOP_V
        if a.kind == "na" and b.kind == "na":
            return NUM1
        if a.kind == "na" and b.kind == "num" and not b.null:
            return NUM0
        if b.kind == "na" and a.kind == "num" and not a.null:
            return NUM0
        return TOP_V

    return TOP_V


# ---------------------------------------------------------------- analysis ---

@dataclass
class Analysis:
    states: dict[int, dict[str, V]] = field(default_factory=dict)
    after: dict[int, dict[str, V]] = field(default_factory=dict)
    branches: list[tuple[int, str, V]] = field(default_factory=list)   # (index, "always"|"never", cond)
    nulls: list[tuple[int, str]] = field(default_factory=list)         # (index, var)
    rounds: int = 0
    stable: bool = True
    cross_folded: int = 0      # `read` results replaced by knowledge of the target


def _const_token(tok: mlogir.Tok) -> V | None:
    if tok.kind == mlogir.KIND_NUM:
        t = tok.text.lower()
        if t == "true":
            return NUM1
        if t == "false":
            return NUM0
        try:
            x = float(t)
        except ValueError:
            return None
        return num(x, x)
    if tok.kind == mlogir.KIND_STR:
        return V("str", s=tok.text)
    # `null` is a literal in mlog, not a variable name -- LogicIO maps that token
    # to an empty object. Test the *text*, not the token kind: `tokenize()` has no
    # reason to special-case it and files it as an ordinary identifier, which is
    # why checking KIND_WORD here never fired and every `strictEqual x null`
    # stayed unresolved.
    if tok.text.lower() == "null":
        return NA
    return None


_BUILTIN_CONST = {"@pi": math.pi, "@e": math.e, "@degToRad": math.pi / 180.0,
                  "@radToDeg": 180.0 / math.pi, "@pi2": math.pi * 2}


def value_of(tok: mlogir.Tok, state: dict[str, V]) -> V:
    c = _const_token(tok)
    if c is not None:
        return c
    if tok.kind == mlogir.KIND_BUILTIN:
        v = _BUILTIN_CONST.get(tok.text)
        return num(v, v) if v is not None else TOP_V
    return state.get(tok.text, TOP_V)


def _cond(name: str, a: V, b: V) -> V:
    return op_value(name.strip().lower(), True, a, b)


def analyze(prog: mlogir.Program, max_rounds: int = 40,
            slots: dict[tuple[str, str], V] | None = None) -> Analysis:
    """Abstract interpretation over the CFG.

    `slots` is the cross-processor knowledge map from
    `mlogcross.slot_values()`: `(link name, slot name) -> value`. When given,
    `read x someProcessor "name"` stops being an unknown writer and yields the
    value the *other* processor actually holds. Without it every cross read is
    TOP, which on a multi-processor map means half the interesting variables
    are unknowable -- which is why this parameter exists.
    """
    stmts = prog.stmts
    n = len(stmts)
    st = Analysis()
    if n == 0:
        return st

    entry = prog.successors(0) if n else []
    # entry state of pc 0 includes the loop back-edge, so seed it as the join
    # of everything that can reach it -- handled naturally by the worklist.
    preds: dict[int, list[int]] = {i: [] for i in range(n)}
    for i in range(n):
        for s in prog.successors(i):
            if 0 <= s < n:
                preds[s].append(i)

    # An mlog variable that has never been assigned is the NUMBER 0, not null:
    # `new LVar(name)` leaves numval = 0 and isobj = false. Without this seed
    # the first join at pc 0 takes the back-edge value verbatim, which is how
    # `ILOS.test.msch` got `strictEqual message null` reported at L28 while
    # `message` is only set to null at L29 -- true on the *second* pass, false
    # on the first one, so the only honest answer there is "0 or null".
    seed: dict[str, V] = {}
    for s in stmts:
        for v in s.defs:
            if not v.startswith("@"):
                seed[v] = NUM0

    in_state: dict[int, dict[str, V]] = {i: {} for i in range(n)}
    out_state: dict[int, dict[str, V]] = {}
    reach = prog.reachable()
    work = sorted(reach)
    touched = {i: 0 for i in range(n)}
    rounds = 0
    folded: set[int] = set()      # statements whose `read` was resolved cross-processor

    def transfer(i: int, cur: dict[str, V]) -> dict[str, V]:
        s = stmts[i]
        nxt = dict(cur)
        mn = s.mnemonic
        if mn == "read" and slots and len(s.args) >= 3:
            # read <dst> <link> "<slot>" -- resolved against the other
            # processor's state instead of falling through to TOP.
            dst = s.args[0].text
            name = s.args[2].text
            if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
                name = name[1:-1]
            v = slots.get((s.args[1].text, name))
            if v is not None:
                nxt[dst] = v
                folded.add(i)
                return nxt
        if mn == "set":
            if len(s.args) >= 2:
                nxt[s.args[0].text] = value_of(s.args[1], cur)
            return nxt
        if mn == "op":
            if len(s.args) >= 3:
                name = s.args[0].text
                dst = s.args[1].text
                a = value_of(s.args[2], cur)
                b = value_of(s.args[3], cur) if len(s.args) >= 4 else None
                nxt[dst] = op_value(name, True, a, b)
            return nxt
        if mn == "select":
            # select <result> <op> <c0> <c1> <a> <b>
            if len(s.args) >= 6:
                cond = _cond(s.args[1].text, value_of(s.args[2], cur),
                             value_of(s.args[3], cur))
                va = value_of(s.args[4], cur)
                vb = value_of(s.args[5], cur)
                ce = cond.exact()
                if ce is not None:
                    nxt[s.args[0].text] = va if ce else vb
                else:
                    nxt[s.args[0].text] = join(va, vb)
            return nxt
        # every other writer: unknown value (sensor, radar, getlink, ulocate, ...)
        for v in s.defs:
            if v != "@counter":
                nxt[v] = TOP_V
        return nxt

    changed = True
    while changed and rounds < max_rounds:
        changed = False
        rounds += 1
        for i in work:
            if i not in reach:
                continue
            acc: dict[str, V] = {}
            for p in preds[i]:
                if p in out_state and p in reach:
                    acc = _join_state(acc, out_state[p]) if acc else dict(out_state[p])
            if i == 0:
                acc = _join_state(dict(seed), acc) if acc else dict(seed)
            if i in in_state:
                merged = _join_state(in_state[i], acc)
            else:
                merged = acc
            # widening: a variable that keeps changing loses precision on purpose
            if merged != in_state.get(i):
                for k, v in merged.items():
                    if in_state.get(i, {}).get(k, TOP_V) != v:
                        touched[i] += 1
                if touched[i] > 3:
                    merged = {k: (v if k not in in_state.get(i, {}) else
                                  (v if in_state[i][k] == v else TOP_V))
                              for k, v in merged.items()}
                in_state[i] = merged
                out_state[i] = transfer(i, merged)
                changed = True
            elif i not in out_state:
                out_state[i] = transfer(i, merged)

    st.states = in_state
    st.after = out_state
    st.rounds = rounds
    st.stable = not changed

    for i in work:
        s = stmts[i]
        if s.mnemonic == "jump" and len(s.args) >= 4:
            a = value_of(s.args[2], in_state.get(i, {}))
            b = value_of(s.args[3], in_state.get(i, {}))
            c = _cond(s.args[1].text, a, b)
            ce = c.exact()
            if ce is not None:
                st.branches.append((i, "always" if ce else "never", c))
        for v in sorted(s.uses):
            if in_state.get(i, {}).get(v, TOP_V).kind == "na":
                st.nulls.append((i, v))
    st.cross_folded = len(folded)
    return st


def format_report(label: str, prog: mlogir.Program, st: Analysis) -> str:
    lines: list[str] = [f"=== {label}"]
    if not st.stable:
        lines.append(f"  ! did not reach a fixpoint in {st.rounds} rounds; "
                     f"values shown are conservative")
    known = [(i, v) for i, v in sorted(st.states.items()) if v]
    lines.append(f"  statements      : {len(prog.stmts)}")
    lines.append(f"  rounds          : {st.rounds}")
    if st.branches:
        lines.append(f"  resolved branches: {len(st.branches)}")
        for i, mode, cond in st.branches[:20]:
            s = prog.stmts[i]
            # `equal`/`notEqual` fall back to a NUMERIC compare unless both
            # sides are objects (`ConditionOp.java:35-39`), and an unset mlog
            # variable is the number 0. So writing `equal x null` as a
            # "has it been assigned yet?" test is constant -- say so, or the
            # reader will think the tool guessed.
            note = ""
            if (len(s.args) >= 4 and s.args[1].text in ("equal", "notEqual")
                    and any(a.text == "null" for a in s.args[2:4])):
                note = ("   [== against null is numeric: an unset variable is 0, "
                        "so only === tells null from 0]")
            lines.append(f"      L{s.line:<5} {mode:<7} taken   "
                         f"`{s.raw.strip()}`   (cond = {cond}){note}")
    else:
        lines.append("  resolved branches: none (every condition depends on "
                     "something outside the program)")
    if st.nulls:
        lines.append(f"  provably null   : {len(st.nulls)}")
        for i, v in st.nulls[:20]:
            s = prog.stmts[i]
            lines.append(f"      L{s.line:<5} `{v}` is null at `{s.raw.strip()}`")
    else:
        lines.append("  provably null   : none")
    return "\n".join(lines)


def trace(prog: mlogir.Program, st: Analysis, var: str) -> list[str]:
    """Every statement that changes the abstract value of `var`."""
    out: list[str] = []
    prev: V | None = None
    for i, s in enumerate(prog.stmts):
        cur = st.after.get(i, {}).get(var, TOP_V)
        if var in s.defs and cur != prev:
            out.append(f"  L{s.line:<5} {var} = {cur}   <- `{s.raw.strip()}`")
            prev = cur
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="mlogval",
                                 description="abstract value / null analysis for mlog")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--trace", metavar="VAR", action="append", default=[])
    a = ap.parse_args(argv)

    rc = 0
    for path in a.files:
        with open(path, encoding="utf-8-sig") as f:
            prog = mlogir.parse(f.read())
        st = analyze(prog)
        print(format_report(path, prog, st))
        for var in a.trace:
            print(f"  -- trace {var} --")
            for ln in trace(prog, st, var) or ["  (never assigned)"]:
                print(ln)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
