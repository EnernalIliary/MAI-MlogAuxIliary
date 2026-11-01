"""Name a finding by the rule in rules/*.json that claims it.

The analyzer emits prose ("... is never assigned anywhere in this program"),
while each rules/*.json file matches that prose through match.sevs +
match.msg_any. tools/rule_capture.py:matches() is the reference reading of
those fields. Consumers used to re-implement the matching (the corpus sweep
did, and its report ended up showing statement text in the rule column), so
this module holds the single definition and mlogcheck stamps every Finding with
its rule id at construction time.

The rules directory is $MLOG_RULES_DIR if set, else the nearest rules/ with at
least one JSON file in the four directories above this module. Walking upwards
matters because tools/ ships in mirrors at different depths:
<repo>/tools (one level up) and <repo>/mindustry-blueprint/tools (two levels
up, with no rules/ of its own -- yet the test suite imports that copy and must
still name findings). A tree packaged without rules/ (the standalone skill)
yields rule_id() = None for everything instead of raising, and callers fall back
to the statement text -- the pre-2026-10-26 behaviour.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def rules_dir():
    """Nearest rules/ directory above this module, or None."""
    env = os.environ.get("MLOG_RULES_DIR")
    if env:
        return Path(env)
    for base in list(Path(__file__).resolve().parents)[:4]:
        cand = base / "rules"
        if cand.is_dir() and any(cand.glob("*.json")):
            return cand
    return None


_CACHE = None


def matchers() -> list:
    """(rule id, {sevs}, [lowercased msg_any]) for every rule with a matcher.

    Rules whose match block is empty are dropped on purpose: rule_capture.py
    treats them as silent verdict records (severity SUPPRESSED, nothing to
    match), so they must never claim a finding.
    """
    global _CACHE
    if _CACHE is None:
        out = []
        d = rules_dir()
        if d is not None:
            for p in sorted(d.glob("*.json")):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                m = data.get("match") or {}
                sevs = set(m.get("sevs") or ())
                keys = [k.lower() for k in (m.get("msg_any") or ()) if k]
                if sevs and keys:
                    out.append((data.get("id") or p.stem, sevs, keys))
        # Longest key first, then rule id: a narrow rule beats a broad one and
        # two processes agree on the winner.  rule_id() re-does the comparison
        # on the key that actually matched (this sort only orders by each
        # rule's longest key, which is not the same thing).
        out.sort(key=lambda t: (-max(len(k) for k in t[2]), t[0]))
        _CACHE = out
    return _CACHE


def rule_id(sev: str, msg: str):
    """Rule id claiming this (severity, message), or None."""
    low = (msg or "").lower()
    if not low:
        return None
    best = None
    blen = -1
    for rid, sevs, keys in matchers():
        if sev not in sevs:
            continue
        for k in keys:
            if k not in low:
                continue
            # Longest matched key wins; equal keys are decided by rule id so the
            # answer never depends on iteration order.  (Sorting alone is not
            # enough: a rule whose OTHER key is longer would win a tie.)
            if len(k) > blen or (len(k) == blen and (best is None or rid < best)):
                best, blen = rid, len(k)
    return best


def rule_ids() -> list:
    """Every rule id that can be matched -- used by the gate's coverage check."""
    return [rid for rid, _, _ in matchers()]
