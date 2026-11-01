"""
mlogir.py -- a structured intermediate representation for mlog source.

Why this exists
---------------
`mlogcheck.py` works on raw lines and re-derives everything with regexes. That is
fine for "is this line shaped right", but it cannot answer value questions:
which variable is assigned here, is that value ever read, what can this
expression evaluate to. Those need tokens, statements, and a def/use split.

This module provides exactly that, and NOTHING that changes existing output:
`mlogcheck.py` keeps its own line-based path so `check` / `--cost` stay
byte-identical (the 429/429 round-trip and 4151-block smoke test are the
regression net for that).

What it gives you
-----------------
    toks = tokenize('op add result a 1')
    prog = parse(source)                # list[Stmt] with per-statement defs/uses
    prog.stmts[3].mnemonic              # 'op'
    prog.stmts[3].defs                  # {'result'}
    prog.stmts[3].uses                  # {'a'}

Design notes
------------
* **Semantics come from a table, not from the mnemonic's name.** The table is
  `instr_semantics.json` when present (derived from LExecutor.java); the built-in
  DEFAULT_TABLE below is a conservative fallback so the module is never useless
  on its own. Table entries win over defaults.
* **Unknown instructions are treated as "reads everything, writes nothing".**
  That is the safe direction: it can cause a false "value never used" instead of
  a false "value is dead", and a wrong dead-store warning is far more damaging
  than a missed one.
* `@counter` is genuinely writable, so `set @counter 5` is a real def.
* Numbers and string literals are never variables; `true`/`false` are numbers.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #

KIND_NUM = "num"
KIND_STR = "str"
KIND_VAR = "var"          # identifier the program owns
KIND_BUILTIN = "builtin"  # @something
KIND_WORD = "word"        # bare keyword-ish token (operator name, sub-command)
_INT_RE = re.compile(r"^-?\d+$")

# LAssembler.var() turns every one of these into a constant, never a variable:
# decimal/float, 0x hex, 0b binary (all with optional sign), and the %rrggbb /
# %[name] colour literals. See LAssembler.parseDouble().
_NUM_RE = re.compile(
    r"^[+-]?(?:"
    r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
    r"|0[xX][0-9a-fA-F]+"
    r"|0[bB][01]+"
    r")$|^%[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$|^%\[[A-Za-z]+\]$")


@dataclass(frozen=True)
class Tok:
    text: str
    kind: str

    @property
    def is_literal(self) -> bool:
        return self.kind in (KIND_NUM, KIND_STR)

    @property
    def is_var(self) -> bool:
        """A token that names storage the analyzer should track."""
        return self.kind in (KIND_VAR, KIND_BUILTIN)

    def number(self) -> float | None:
        if self.kind != KIND_NUM:
            return None
        return float(self.text)

    def __str__(self) -> str:
        return self.text


def classify(text: str) -> str:
    if text.startswith('"'):
        return KIND_STR
    if text.startswith("@"):
        return KIND_BUILTIN
    if _NUM_RE.match(text):
        return KIND_NUM
    if text in ("true", "false"):
        return KIND_NUM
    return KIND_VAR


def tokenize(line: str) -> list[Tok]:
    """Split one mlog statement into tokens.

    Handles the one genuinely tricky case: string literals may contain spaces,
    and `\"` is an escaped quote. Everything else is whitespace separated.
    """
    out: list[Tok] = []
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            break
        if line[i] == '"':
            j = i + 1
            buf = []
            while j < n:
                c = line[j]
                if c == "\\" and j + 1 < n:
                    buf.append(c)
                    buf.append(line[j + 1])
                    j += 2
                    continue
                if c == '"':
                    j += 1
                    break
                buf.append(c)
                j += 1
            out.append(Tok('"' + "".join(buf) + '"', KIND_STR))
            i = j
            continue
        j = i
        while j < n and not line[j].isspace():
            j += 1
        text = line[i:j]
        out.append(Tok(text, classify(text)))
        i = j
    return out


# --------------------------------------------------------------------------- #
# instruction semantics
# --------------------------------------------------------------------------- #

# Conservative built-in fallback. `table` entries override these.
# defs = argument indices that are WRITTEN; uses = indices that are READ.
# Indices count *arguments only* (the mnemonic is not arg 0).
DEFAULT_TABLE: dict[str, dict] = {
    # --- data movement ----------------------------------------------------
    "set":       {"defs": [0], "uses": [1]},
    "op":        {"defs": [1], "uses": [2, 3], "note": "arg0 is the operator name"},
    "read":      {"defs": [0], "uses": [1, 2]},
    "write":     {"defs": [],  "uses": [0, 1, 2]},
    "getlink":   {"defs": [0], "uses": [1]},
    "sensor":    {"defs": [0], "uses": [1, 2]},
    "lookup":    {"defs": [1], "uses": [2], "note": "arg0 is the content type"},
    "packcolor": {"defs": [0], "uses": [1, 2, 3, 4]},
    # SelectStatement (LStatements.java:786-788) declares its fields in the
    # order result, op, comp0, comp1, a, b -- and LStatement.write serialises
    # public fields in declaration order, so that IS the textual order.
    "select":    {"defs": [0], "uses": [2, 3, 4, 5],
                  "note": "arg1 is the comparison operator"},
    # --- control ----------------------------------------------------------
    "jump":      {"defs": [],  "uses": [0, 2, 3],
                  "note": "arg0 = line number (may itself be a variable); "
                          "args 2,3 are the compared operands"},
    "end":       {"defs": [],  "uses": []},
    "stop":      {"defs": [],  "uses": []},
    "wait":      {"defs": [],  "uses": [0]},
    # --- output -----------------------------------------------------------
    "print":     {"defs": [],  "uses": [0]},
    "printflush": {"defs": [], "uses": [0]},
    "draw":      {"defs": [],  "uses": "all"},
    # --- world / building interaction -------------------------------------
    "control":   {"defs": [],  "uses": "all"},
    "radar":     {"defs": [6], "uses": [5]},
    # --- units ------------------------------------------------------------
    "ubind":     {"defs": [],  "uses": [0]},
    "ucontrol":  {"defs": [],  "uses": "all", "note": "sub-command dependent"},
    "uradar":    {"defs": [6], "uses": [5]},
    "ulocate":   {"defs": [4, 5, 6, 7], "uses": [2, 3]},
    # --- misc -------------------------------------------------------------
    "noop":      {"defs": [],  "uses": [],  "note": "arguments are ignored"},
}

STATEMENTS: frozenset[str] = frozenset(DEFAULT_TABLE)

# Argument positions that hold a KEYWORD, not a variable.
#
# This matters more than it looks: in `radar enemy any any distance core 1 out`
# four of those bare words tokenize exactly like variable names. Without this,
# dead-store analysis would report "variable `enemy` is assigned but never read"
# for every radar statement in the corpus.
DEFAULT_KEYWORDS: dict[str, list[int]] = {
    "op":      [0],                       # operator name
    "lookup":  [0],                       # block / unit / item / liquid
    "control": [0],                       # enabled / shoot / shootp / config / color
    "ucontrol": [0],                      # sub-command
    "draw":    [0],                       # sub-command
    "radar":   [0, 1, 2, 3, 4],           # 3 target filters, sort key, sort order
    "uradar":  [0, 1, 2, 3, 4],
    "ulocate": [0, 1],                    # locate kind, group
    "jump":    [1],                       # comparison operator
    "select":  [1],                       # comparison operator
}

_TABLE_CACHE: dict | None = None



def load_table(path: str | None = None) -> dict:
    """Load instr_semantics.json merged over DEFAULT_TABLE."""
    global _TABLE_CACHE
    if _TABLE_CACHE is not None and path is None:
        return _TABLE_CACHE
    merged = {k: dict(v) for k, v in DEFAULT_TABLE.items()}
    p = path or os.path.join(HERE, "instr_semantics.json")
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        for name, spec in (data.get("instructions") or {}).items():
            base = merged.setdefault(name, {"defs": [], "uses": []})
            if "defs" in spec:
                base["defs"] = spec["defs"]
            if "uses" in spec:
                base["uses"] = spec["uses"]
            if spec.get("subcommands"):
                base["subcommands"] = spec["subcommands"]
            if spec.get("keywords"):
                base["keywords"] = spec["keywords"]
            # "tokens" are positions holding an enum literal (ConditionOp,
            # TileLayer, ...) -- never storage, so treat them as keywords.
            if spec.get("tokens"):
                base["keywords"] = sorted(int(k) for k in spec["tokens"])
            if spec.get("note"):
                base["note"] = spec["note"]
        if data.get("ops"):
            merged["op"]["ops"] = data["ops"]
        _TABLE_CACHE = merged
    except Exception:
        _TABLE_CACHE = merged
    return _TABLE_CACHE


# --------------------------------------------------------------------------- #
# statements
# --------------------------------------------------------------------------- #

@dataclass
class Stmt:
    index: int                      # statement index (0-based, comments/blanks dropped)
    line: int                       # 1-based source line
    raw: str
    mnemonic: str
    args: list[Tok] = field(default_factory=list)
    defs: set[str] = field(default_factory=set)
    uses: set[str] = field(default_factory=set)
    # `read x @this "name"` / `write v @this "name"` address a *processor
    # variable* by a string, so the text argument is not a variable token and
    # never reaches defs/uses. Keep the names separately for the checker.
    addr_reads: set[str] = field(default_factory=set)
    addr_writes: set[str] = field(default_factory=set)
    # Names that this statement *creates* without assigning anything. The
    # assembler's readVar() calls vars.get(name, true), so the first mention of
    # a name anywhere declares it (initial value null) -- it does not have to be
    # a `set`. `draw triangle a b c d e f` is the dense form: six in one line.
    declares: set[str] = field(default_factory=set)
    unknown: bool = False           # mnemonic not in the semantics table

    def arg(self, i: int) -> Tok | None:
        return self.args[i] if 0 <= i < len(self.args) else None

    def arg_text(self, i: int) -> str:
        t = self.arg(i)
        return t.text if t else ""

    def jump_target(self) -> int | None:
        if self.mnemonic not in ("jump", "end"):
            return None
        if self.mnemonic == "end":
            return None
        t = self.arg(0)
        if t and t.kind == KIND_NUM:
            try:
                return int(float(t.text))
            except ValueError:
                return None
        return None

    def __str__(self) -> str:
        return f"L{self.line}: {self.raw}"


def strip_comment(line: str) -> str:
    """Drop a comment line. mlog comments start with '#' at the beginning of a line."""
    s = line.strip().lstrip("\ufeff")
    if not s or s.startswith("#"):
        return ""
    return s


def _resolve_spec(stmt_mnemonic: str, args: list[Tok], table: dict) -> dict:
    spec = table.get(stmt_mnemonic)
    if spec is None:
        return {"defs": [], "uses": "all"}
    subs = spec.get("subcommands")
    if subs and args:
        # the first argument selects the sub-command
        key = args[0].text
        if key in subs:
            sub = dict(spec)
            sub.update(subs[key])
            return sub
    return spec


def _index_set(val, nargs: int) -> set[int]:
    if val == "all":
        return set(range(nargs))
    return {int(i) for i in val if int(i) < nargs}


def build_stmt(index: int, line_no: int, raw: str, table: dict) -> Stmt:
    toks = tokenize(raw)
    if not toks:
        return Stmt(index, line_no, raw, "", unknown=True)
    mnemonic = toks[0].text.lower()
    args = toks[1:]
    spec = _resolve_spec(mnemonic, args, table)
    defs_all = _index_set(spec.get("defs", []), len(args))
    uses_idx = _index_set(spec.get("uses", "all"), len(args))

    # keyword slots (operator name, sub-command, radar filter, ...) are not vars
    kw = spec.get("keywords", DEFAULT_KEYWORDS.get(mnemonic, []))
    if isinstance(kw, dict):
        kw = [int(k) for k in kw]
    kw = {int(i) for i in kw}
    defs_idx = set(defs_all)

    # A checker must never claim "nobody reads this" when the game might.
    # Three sources of doubt, all resolved in favour of "read":
    #   maybe_uses   -- read on *some* executions: `select` dereferences
    #                   only one of a/b (Java ternary at LExecutor.java:892),
    #                   ulocate writes outX/outY only on a hit.
    #   semantic_uses -- read by the executor rather than by a text argument.
    #   unaccounted  -- a position that is neither an output slot nor a
    #                   keyword, so the table simply does not say. `playsound`
    #                   declares 8 params and marks only 1 as read; `setrule`
    #                   6 and only 1. Leaving these out produced a wave of
    #                   false "overwritten before any read" reports.
    # Over-approximating reads can only hide a finding, never invent one,
    # which is the right trade for a linter whose output a human acts on.
    blind = set(range(len(args))) - defs_all - kw

    # `op` is the one instruction whose arity depends on arg0. The game parses
    # every token on the line and then hands only the operands it wants to the
    # operator (UnaryOp reads `a`, BinaryOp reads `a` and `b`), so a 4th token
    # on a unary op -- `op rand flag 1000 b` -- is parsed and silently dropped.
    # Counting it as a read produced a hard error on clean code:
    # "`b` is read here but is never assigned anywhere in this program".
    if mnemonic == "op" and len(args) >= 2:
        _opname = getattr(args[0], "text", "").lower()
        _meta = (table.get("op") or {}).get("ops") or {}
        _n = (_meta.get(_opname) or {}).get("operands")
        if _n in (1, 2):
            _keep = {2} if _n == 1 else {2, 3}
            uses_idx = {i for i in _keep if i < len(args)}
            blind = {i for i in blind if i in _keep}

    for extra in ("maybe_uses", "semantic_uses"):
        v = spec.get(extra, [])
        if v == "all":
            uses_idx |= set(range(len(args)))
        else:
            uses_idx |= {int(i) for i in v if int(i) < len(args)}
    uses_idx |= blind

    # `jump T always x false` is the shape emitted by generators and dispatch
    # tables: arity is padded to four tokens for every jump, and `always` reads
    # neither operand. Counting them as reads invents undefined variables named
    # `x`/`false` in hundreds of blocks.
    if mnemonic == "jump" and len(args) >= 2 and args[1].text == "always":
        uses_idx = set()

    for i in kw:
        defs_idx.discard(i)
        uses_idx.discard(i)

    def vars_at(idx: set[int]) -> set[str]:
        out = set()
        for i in sorted(idx):
            t = args[i]
            # a literal is never storage; a bare keyword is not storage either
            if t.kind == KIND_VAR or (t.kind == KIND_BUILTIN):
                out.add(t.text)
        return out

    known = mnemonic in table
    addr_reads: set[str] = set()
    addr_writes: set[str] = set()
    declares: set[str] = set()
    # `draw triangle a b c d e f` is the dense declaration form: the executor
    # reads the six names (null -> 0) and the assembler creates each one on
    # first mention. It is used as a compact variable table, so surface them as
    # declarations rather than only reads.
    if mnemonic == "draw" and args and args[0].text == "triangle":
        declares = {t.text for t in args[1:] if t.kind == KIND_VAR}
    # `read out target address` / `write value target address`: a *string*
    # address on @this names a processor variable. The name is data, not a
    # variable token, so record it separately for the checker.
    if mnemonic in ("read", "write") and len(args) > 2:
        if args[1].text == "@this" and args[2].kind == KIND_STR:
            name = args[2].text.strip('"')
            (addr_reads if mnemonic == "read" else addr_writes).add(name)
    return Stmt(
        index=index,
        line=line_no,
        raw=raw,
        mnemonic=mnemonic,
        args=args,
        defs=vars_at(defs_idx),
        uses=vars_at(uses_idx),
        addr_reads=addr_reads,
        addr_writes=addr_writes,
        declares=declares,
        unknown=not known,
    )


# --------------------------------------------------------------------------- #
# program
# --------------------------------------------------------------------------- #

@dataclass
class Program:
    stmts: list[Stmt]
    labels: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.stmts)

    @property
    def unknown_mnemonics(self) -> list[str]:
        return sorted({s.mnemonic for s in self.stmts if s.unknown})

    def successors(self, i: int) -> list[int]:
        """CFG successors. `end` restarts at 0; `stop` is the only real sink.

        Writing `@counter` is a computed jump: `set @counter N` is absolute,
        while `op add @counter @counter K` lands on `i + 1 + K` because the
        executor increments the register *before* running the instruction.
        Unresolvable forms fall back to "everything is reachable".
        """
        if i < 0 or i >= len(self.stmts):
            return []
        s = self.stmts[i]
        n = len(self.stmts)
        if s.mnemonic == "stop":
            return []
        if s.mnemonic == "end":
            return [0] if n else []
        if s.mnemonic == "set" and s.arg_text(0) == "@counter":
            arg = s.arg_text(1)
            if _INT_RE.match(arg):
                tgt = int(arg)
                return [tgt] if 0 <= tgt < n else []
            return list(range(n))
        if s.mnemonic == "op" and s.arg_text(1) == "@counter":
            opname, a, b = s.arg_text(0), s.arg_text(2), s.arg_text(3)
            la = int(a) if _INT_RE.match(a) else None
            lb = int(b) if _INT_RE.match(b) else None
            base = i + 1
            tgt = None
            if opname == "add":
                if a == "@counter" and lb is not None:
                    tgt = base + lb
                elif b == "@counter" and la is not None:
                    tgt = base + la
                elif la is not None and lb is not None:
                    tgt = la + lb
            elif opname == "sub":
                if a == "@counter" and lb is not None:
                    tgt = base - lb
                elif la is not None and lb is not None:
                    tgt = la - lb
            elif la is not None and lb is not None:
                tgt = {"mul": la * lb, "div": la // lb if lb else None,
                       "mod": la % lb if lb else None}.get(opname)
            if tgt is None:
                return list(range(n))
            return [tgt] if 0 <= tgt < n else []
        if s.mnemonic == "jump":
            out = []
            tgt = s.jump_target()
            if tgt is not None and 0 <= tgt < n:
                out.append(tgt)
            elif tgt is not None and tgt < 0:
                # `jump -1 always` is the idiomatic way of DISABLING a jump:
                # JumpI.run() is guarded by `if(address != -1)`, so a negative
                # address does nothing and control falls through. Treating it as
                # a terminator reported the whole rest of the program as dead
                # (坦克大战.msch: 182 of 190 statements).
                return [i + 1] if i + 1 < n else []
            cond = s.arg_text(1).lower()
            if cond != "always" and i + 1 < n:
                out.append(i + 1)
            return out
        return [i + 1] if i + 1 < n else []

    def reachable(self) -> set[int]:
        seen: set[int] = set()
        if not self.stmts:
            return seen
        stack = [0]
        while stack:
            i = stack.pop()
            if i in seen:
                continue
            seen.add(i)
            stack.extend(self.successors(i))
        return seen

    def dead(self) -> list[int]:
        live = self.reachable()
        return [i for i in range(len(self.stmts)) if i not in live]

    def writes_counter(self) -> bool:
        """True if the program computes its own control flow via `@counter`.

        `op add @counter @counter id` is a dispatch table: the next statement is
        whatever the id-th line is, so static reachability is meaningless and
        `dead()` reports the whole table as unreachable. Real example in the
        wild: 388 consecutive `jump T always x false` lines indexed by a block
        content id (质驱T4 大超.msch), of which 584 statements looked dead.
        """
        return any("@counter" in s.defs for s in self.stmts)

    @property
    def declared(self) -> set[str]:
        """Every name the program creates, in any statement.

        The assembler's readVar() does ``vars.get(name, true)``: the first
        mention of a name *anywhere* declares it (initial value null), so
        declaration does not require a ``set``. This is why
        ``draw triangle a b c d e f`` can be used as a six-slot table, and why
        "never mentioned" -- not "never assigned" -- is the only true
        undefined-variable condition.
        """
        names: set[str] = set()
        for s in self.stmts:
            names |= s.defs | s.uses | s.declares | s.addr_reads | s.addr_writes
        return names

    def def_use_map(self) -> dict[str, list[tuple[int, str]]]:
        """variable -> [(stmt index, 'def'|'use'), ...] in program order."""
        m: dict[str, list[tuple[int, str]]] = {}
        for s in self.stmts:
            for v in sorted(s.uses):
                m.setdefault(v, []).append((s.index, "use"))
            for v in sorted(s.defs):
                m.setdefault(v, []).append((s.index, "def"))
        return m

    # ----------------------------------------------------------------- #
    # liveness  (backward dataflow over the CFG, restart edge included)
    # ----------------------------------------------------------------- #

    def liveness(self) -> tuple[dict[int, set[str]], dict[int, set[str]]]:
        """Return (live_in, live_out) per statement index.

        The graph is cyclic because `end` jumps back to 0, so the usual
        backward worklist converges to the least fixpoint -- which is
        exactly what we want: a value written on the last line and read on
        the first line of the next cycle *is* live, and the fixpoint finds
        that without any special-casing.
        """
        n = len(self.stmts)
        succ: list[list[int]] = [[] for _ in range(n)]
        for i in range(n):
            succ[i] = [s for s in self.successors(i) if 0 <= s < n]

        live_in: dict[int, set[str]] = {i: set() for i in range(n)}
        live_out: dict[int, set[str]] = {i: set() for i in range(n)}

        changed = True
        while changed:
            changed = False
            for i in range(n - 1, -1, -1):
                out: set[str] = set()
                for s in succ[i]:
                    out |= live_in[s]
                inn = self.stmts[i].uses | (out - self.stmts[i].defs)
                if out != live_out[i] or inn != live_in[i]:
                    live_out[i] = out
                    live_in[i] = inn
                    changed = True
        return live_in, live_out

    def dead_stores(self) -> list[tuple[int, str]]:
        """(stmt index, variable) pairs whose value can never be read.

        A def is dead when the variable is not live-out of that statement.
        Caveat surfaced by the caller: a processor's variables are readable
        from *outside* via `read result someProcessor "name"`, so "not read
        in this file" is not proof of uselessness on a multi-processor map.
        """
        _, live_out = self.liveness()
        out: list[tuple[int, str]] = []
        for s in self.stmts:
            for v in sorted(s.defs):
                # `@counter` is a control transfer, not a value: writing it
                # changes what runs next, so "never read" means nothing.
                if v == "@counter":
                    continue
                if v not in live_out[s.index]:
                    out.append((s.index, v))
        return out

    def classify_dead_stores(self) -> list[tuple[int, str, str]]:
        """(stmt index, variable, kind) with kind in
        `overwritten` / `stale-write` / `never-read`.

        The kinds are very different evidence, and reporting them as one list
        is what made the first version unusable:

        `overwritten` -- the variable *is* read somewhere in this program, so
        some other write destroys this value before any read can reach it.
        That is almost always a mistake: a copy-paste, or a destination
        variable typo'd into a fresh name.

        `stale-write` -- nothing reads the variable anywhere, but it is written
        more than once, so every write after the first is provably discarded
        work.  This is the shape of a real bug found in the corpus: a checker
        that does `ulocate ... found ...` twice and never looks at `found`
        (17十七硫幽灵四熔毁四油海啸组.msch).  A single never-read write can be
        a declaration; a second one cannot be anything but waste.

        `never-read` -- exactly one write and no read at all.  It may be a
        deliberate declaration or an in-editor annotation, so it is much
        weaker evidence and callers normally hide it.
        """
        used = {v for s in self.stmts for v in s.uses}
        writes: dict[str, int] = {}
        for s in self.stmts:
            for v in s.defs:
                writes[v] = writes.get(v, 0) + 1

        def kind(v: str) -> str:
            if v in used:
                return "overwritten"
            return "stale-write" if writes.get(v, 0) > 1 else "never-read"

        return [(i, v, kind(v)) for i, v in self.dead_stores()]


def _is_label(body: str) -> str | None:
    """A line whose only token ends with ':' is a jump label, not a statement."""
    toks = tokenize(body)
    if len(toks) == 1:
        t = toks[0].text
        if t.endswith(":") and len(t) > 1:
            return t[:-1]
    return None


def parse(source: str | list[str], table: dict | None = None) -> Program:
    tbl = table if table is not None else load_table()
    lines = source.split("\n") if isinstance(source, str) else list(source)
    # Pass 1: drop comments, blanks and jump labels. A label names the position
    # of the *next* statement -- `LParser.java:117-125` stores
    # `jumpLocations.put(label, line)` with `line` being the count of statements
    # already parsed, so a label consumes no instruction slot.
    bodies: list[tuple[int, str]] = []
    labels: dict[str, int] = {}
    for ln, raw in enumerate(lines, 1):
        body = strip_comment(raw)
        if not body:
            continue
        label = _is_label(body)
        if label is not None:
            labels[label] = len(bodies)
            continue
        bodies.append((ln, body))
    stmts = [build_stmt(i, ln, body, tbl) for i, (ln, body) in enumerate(bodies)]
    # Pass 2: resolve `jump <label>` to the numeric address it names. A trailing
    # label sits at the very end, so jumps to it go one past the last statement,
    # which is where `end` would restart anyway.
    for st in stmts:
        if st.mnemonic != "jump" or not st.args:
            continue
        t = st.args[0]
        if t.kind == KIND_NUM or t.text not in labels:
            continue
        st.args[0] = Tok(str(labels[t.text]), KIND_NUM)
    return Program(stmts, labels)


# --------------------------------------------------------------------------- #
# self-check
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys

    sample = r'''
# comment
set x 1
op add y x 1
jump 5 lessThan y 10
print y
end
sensor s dome1 @x
ucontrol move s 0 0 0 0
'''
    prog = parse(sample)
    print(f"{len(prog)} statements")
    for st in prog.stmts:
        d = ",".join(sorted(st.defs)) or "-"
        u = ",".join(sorted(st.uses)) or "-"
        flag = "  [unknown]" if st.unknown else ""
        print(f"  {st.index:2d} L{st.line:<3} {st.mnemonic:<11} def={d:<12} use={u}{flag}")
    print()
    print("unknown mnemonics:", prog.unknown_mnemonics)
    print("dead statements  :", prog.dead())
    print()
    tbl = load_table()
    print(f"semantics table: {len(tbl)} instructions")
    sys.exit(0)
