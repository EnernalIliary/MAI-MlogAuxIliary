#!/usr/bin/env python3
"""mlogrange -- 变量区间分析 + @counter 跳转表展开.

为什么需要它: `jump` 的落点可以静态算出, 但 `op add @counter @counter x`
的落点取决于 x 的**取值范围**。只按数学上界猜 (比如 8 槽 -> 0..7) 会把
不可能的值也当成可达, 或者反过来漏掉真正的越界。这里用前向区间分析
把 x 的范围机器算出来, 再枚举成跳转表。

只认能证明的区间:
  - set 字面量
  - op add/sub/mul/mod/min/max (操作数都有区间)
  - select (两个分支取并集)
  - 其它写入 (sensor/read/ubind/ulocate/...) 一律当作"未知", 不猜.
未知的值不会进跳转表, 只报 "unknown"。

用法:
  python tools/mlogrange.py ranges FILE.mlog
  python tools/mlogrange.py jumps  FILE.mlog [--max 64]
  python tools/mlogrange.py jumps  FILE.mlog --var counter.a
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlogir  # noqa: E402

NUM = re.compile(r"^-?\d+(?:\.\d+)?$")
WIDE = 10 ** 6
MAX_ENUM = 64
ROUNDS = 300


def _num(tok):
    if tok is None or getattr(tok, "kind", "") == "str":
        return None
    s = tok.text
    if NUM.match(s):
        v = float(s)
        if v.is_integer():
            return int(v)
    return None


def _name(tok):
    if tok is None or getattr(tok, "kind", "") == "str":
        return None
    s = tok.text
    if NUM.match(s) or s.startswith("@"):
        return None
    return s


def _val(tok, state):
    """token -> interval or None (unknown)."""
    if tok is None:
        return None
    n = _num(tok)
    if n is not None:
        return (n, n)
    name = _name(tok)
    if name is None:
        return None
    if name == "@counter":
        return None  # 由调用处替换
    return state.get(name, (0, 0))


def _join(a, b):
    if a is None or b is None:
        return None
    return (min(a[0], b[0]), max(a[1], b[1]))


def _op(kind, x, y):
    if x is None or y is None:
        return None
    if kind == "add":
        return (x[0] + y[0], x[1] + y[1])
    if kind == "sub":
        return (x[0] - y[1], x[1] - y[0])
    if kind == "mul":
        vals = [x[0] * y[0], x[0] * y[1], x[1] * y[0], x[1] * y[1]]
        if max(abs(v) for v in vals) > WIDE:
            return None
        return (min(vals), max(vals))
    if kind in ("min", "max"):
        vals = [x[0], x[1], y[0], y[1]]
        return (min(vals), max(vals)) if kind == "min" else (min(vals), max(vals))
    if kind in ("mod", "idiv"):
        if y[0] != y[1] or y[0] == 0:
            return None
        d = abs(y[0])
        if kind == "mod":
            if x[0] >= 0:
                return x if x[1] < d else (0, d - 1)
            if x[1] < 0:
                return x if x[0] > -d else (-(d - 1), 0)
            return (-(d - 1), d - 1)
        if x[0] >= 0:
            return (0, max(0, x[1] // d))
        return None
    return None


def _refine(state, cond, name, c, truth):
    """按比较条件收紧 name 的区间; 返回 None 表示这条边不可能."""
    r = state.get(name, (0, 0))
    if r is None:
        return state
    lo, hi = r
    if cond == "lessThan":
        hi = min(hi, c - 1) if truth else hi
        lo = max(lo, c) if not truth else lo
    elif cond == "lessThanEq":
        hi = min(hi, c) if truth else hi
        lo = max(lo, c + 1) if not truth else lo
    elif cond == "greaterThan":
        lo = max(lo, c + 1) if truth else lo
        hi = min(hi, c) if not truth else hi
    elif cond == "greaterThanEq":
        lo = max(lo, c) if truth else lo
        hi = min(hi, c - 1) if not truth else hi
    elif cond == "equal":
        if truth:
            lo, hi = max(lo, c), min(hi, c)
        elif lo == hi == c:
            return None
    elif cond == "notEqual":
        if truth:
            if lo == hi == c:
                return None
        else:
            lo, hi = max(lo, c), min(hi, c)
    else:
        return state
    if lo > hi:
        return None
    out = dict(state)
    out[name] = (lo, hi)
    return out


def _edge(state, s, taken):
    """沿一条边传播时, 用 jump 条件收紧状态."""
    if s.mnemonic != "jump":
        return state
    cond = s.arg_text(1)
    a, b = s.arg(2), s.arg(3)
    an, bn = _num(a), _num(b)
    if an is not None and _name(b):
        # const OP var
        flip = {"lessThan": "greaterThan", "lessThanEq": "greaterThanEq",
                "greaterThan": "lessThan", "greaterThanEq": "lessThanEq",
                "equal": "equal", "notEqual": "notEqual"}
        return _refine(state, flip.get(cond, cond), _name(b), an, taken)
    if bn is not None and _name(a):
        return _refine(state, cond, _name(a), bn, taken)
    return state


def _counter_targets(s, i, st, n):
    """Possible jump targets of a statement that writes @counter."""
    m = s.mnemonic
    if m == "set":
        v = _val(s.arg(1), st)
        return [v[0]] if v is not None and v[0] == v[1] else list(range(n))
    if m != "op":
        return list(range(n))
    kind = s.arg_text(0)
    a, b = s.arg(2), s.arg(3)
    base = i + 1
    if kind not in ("add", "sub"):
        return list(range(n))
    if a is not None and a.text == "@counter":
        var = b
    elif b is not None and b.text == "@counter":
        var = a
    else:
        return list(range(n))
    v = _val(var, st)
    if v is None or v[1] - v[0] > 64:
        return list(range(n))
    sign = 1 if kind == "add" else -1
    return [base + sign * x for x in range(v[0], v[1] + 1)]


def _transfer(s, i, st):
    out = dict(st)
    m = s.mnemonic
    defs = list(s.defs)
    if m == "set" and defs:
        out[defs[0]] = _val(s.arg(1), st)
    elif m == "op" and defs:
        kind = s.arg_text(0)
        a, b = s.arg(2), s.arg(3)
        av = (i + 1, i + 1) if (a is not None and a.text == "@counter") else _val(a, st)
        bv = (i + 1, i + 1) if (b is not None and b.text == "@counter") else _val(b, st)
        out[defs[0]] = _op(kind, av, bv)
    elif m == "select" and defs:
        out[defs[0]] = _join(_val(s.arg(4), st), _val(s.arg(5), st))
    else:
        for d in defs:
            out[d] = None
    if m == "sensor" and defs:
        out[defs[0]] = None
    return out


def analyse(prog: mlogir.Program):
    """前向区间分析 (Gauss-Seidel 扫描). entry[i] = {var: (lo,hi) | None}."""
    n = len(prog.stmts)
    entry = [{} for _ in range(n)]
    for _ in range(120):
        changed = False
        for i, s in enumerate(prog.stmts):
            out = _transfer(s, i, entry[i])
            if any(t is not None and t.text == "@counter" for t in s.args):
                # A counter write is a dispatch, not a fallthrough. Enumerate
                # the targets so the slots still get the incoming state; an
                # unknown target means "could be anywhere".
                for j in _counter_targets(s, i, entry[i], n):
                    if 0 <= j < n:
                        merged = entry[j]
                        for k, v in list(out.items()):
                            ov = merged.get(k)
                            nv = _join(ov, v) if k in merged else v
                            if nv is not None and nv[1] - nv[0] > WIDE:
                                nv = None
                            if ov != nv:
                                merged[k] = nv
                                changed = True
                continue
            target = s.jump_target() if s.mnemonic == "jump" else None
            for j in prog.successors(i):
                if not (0 <= j < n):
                    continue
                e = _edge(out, s, target is not None and j == target)
                if e is None:
                    continue
                merged = entry[j]
                for k, v in list(e.items()):
                    ov = merged.get(k)
                    nv = _join(ov, v) if k in merged else v
                    if nv is not None and nv[1] - nv[0] > WIDE:
                        nv = None
                    if ov != nv:
                        merged[k] = nv
                        changed = True
        if not changed:
            break
    return entry


def cmd_ranges(path, var_filter=None):
    prog = mlogir.parse(Path(path).read_text(encoding="utf-8-sig"))
    entry = analyse(prog)
    print(f"=== {path}  ({len(prog.stmts)} statements)")
    for i, s in enumerate(prog.stmts):
        st = entry[i]
        items = sorted(st.items())
        if var_filter:
            items = [(k, v) for k, v in items if k == var_filter]
        if not items:
            continue
        body = ", ".join(
            f"{k} in [{v[0]},{v[1]}]" if v is not None else f"{k}=?"
            for k, v in items
        )
        print(f"L{i:<3} {body}")
    return 0


def _dispatch(s):
    if s.mnemonic != "op":
        return None
    kind = s.arg_text(0)
    if kind not in ("add", "sub"):
        return None
    a, b = s.arg(2), s.arg(3)
    if a is not None and a.text == "@counter":
        var = _name(b)
        if var:
            return (kind, var, -1 if kind == "sub" else 1)
    if b is not None and b.text == "@counter":
        var = _name(a)
        if var:
            return ("add", var, 1)
    return None


def cmd_jumps(path, max_enum=MAX_ENUM, var_filter=None):
    prog = mlogir.parse(Path(path).read_text(encoding="utf-8-sig"))
    entry = analyse(prog)
    n = len(prog.stmts)
    found = 0
    for i, s in enumerate(prog.stmts):
        d = _dispatch(s)
        if not d:
            continue
        kind, var, sign = d
        if var_filter and var != var_filter:
            continue
        found += 1
        rng = entry[i].get(var)
        print(f"L{i}  {s.raw.strip()}")
        if rng is None:
            print(f"  {var} = unknown (not expanded)")
            continue
        lo, hi = rng
        print(f"  {var}  in  [{lo},{hi}]")
        if hi - lo + 1 > max_enum:
            print(f"  range too wide ({hi - lo + 1} > {max_enum}); pass --max")
            continue
        for v in range(lo, hi + 1):
            t = i + 1 + sign * v
            ok = 0 <= t < n
            txt = prog.stmts[t].raw.strip() if ok else "-- out of range --"
            print(f"    {var}={v:<3} -> L{t:<3} {txt}")
    if not found:
        print("no @counter arithmetic found")
    return 0


def main(argv):
    if len(argv) < 3 or argv[1] not in ("ranges", "jumps"):
        print(__doc__)
        return 2
    cmd, path = argv[1], argv[2]
    var = None
    max_enum = MAX_ENUM
    if "--var" in argv:
        var = argv[argv.index("--var") + 1]
    if "--max" in argv:
        max_enum = int(argv[argv.index("--max") + 1])
    if cmd == "ranges":
        return cmd_ranges(path, var)
    return cmd_jumps(path, max_enum, var)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
