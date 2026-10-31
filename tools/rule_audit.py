"""R2 — noise audit: run every rule over a corpus and print how much of the report it owns.

The problem this solves: a checker with 20 rules produces a flat list, and the rule that
fires 96% of the time is invisible inside it. That rule is almost always the one that is
wrong — the arity class owned 96% of all warnings on a community map pack, and it turned
out to be reading the parser's slot count instead of the parameters the instruction uses.

For every rule in ``rules/*.json`` this reports:

  hits       how many findings in this corpus it produced
  programs   in how many distinct programs (corpus dedupes by source hash)
  share      share of all findings -- the first number to look at
  precision  a *measurable* proxy: share of hits whose source line still matches the rule's
             own ``line_regex``. If a rule says "unary op with extra operand" and half its
             hits have no unary op on the line, the predicate has drifted from the claim.
             100% means "self-consistent", NOT "correct" -- correctness still needs a human.

``--apply`` writes the measured numbers back into each rule record (``observed``) and
prints the downgrade it *would* make for rules flagged ``auto_downgrade``. It never edits
mlogcheck.py: a rule moving from WARN to INFO changes what every user sees, so the decision
is recorded, not executed. Upgrades are never automatic at all.

Usage::

    python tools/rule_audit.py                          # saves folder
    python tools/rule_audit.py --dir out/maps --json out/rule_audit.json
    python tools/rule_audit.py --apply                  # record numbers into rules/*.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mindustry-blueprint" / "tools"))

import map_logic as ML  # noqa: E402
import mlogcheck as C  # noqa: E402
import parallel  # noqa: E402

RULES_DIR = ROOT / "rules"
DEFAULT_DIR = Path.home() / "AppData" / "Roaming" / "Mindustry" / "saves"
ROW_CAP = 3  # reviewable rows per rule per save


def load_rules() -> list[dict]:
    out = []
    for path in sorted(RULES_DIR.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {path.name}: {exc}")
            continue
        rec["_path"] = path
        rec["_line_re"] = re.compile(rec["line_regex"]) if rec.get("line_regex") else None
        out.append(rec)
    return out


def classify(sev: str, msg: str, rules: list[dict]) -> dict | None:
    low = msg.lower()
    for rec in rules:
        m = rec.get("match") or {}
        if sev not in (m.get("sevs") or []):
            continue
        keys = [k.lower() for k in (m.get("msg_any") or [])]
        if keys and not any(k in low for k in keys):
            continue
        return rec
    return None


def audit_one(path_str: str) -> dict:
    """Worker: one save, deduped by source hash, findings bucketed by rule id."""
    import hashlib

    rules = load_rules()
    try:
        info = ML.read_save(Path(path_str))
    except Exception as exc:  # noqa: BLE001
        return {"file": Path(path_str).name, "error": str(exc)[:120], "tiles": 0}

    seen: set[str] = set()
    hits: dict[str, int] = {}
    bad_regex: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    unclassified: dict[str, int] = {}
    rows: list[dict] = []
    tiles = 0

    for t in info.get("logic") or []:
        code = t.get("code") or ""
        norm = "\n".join(ln.rstrip() for ln in code.replace("\r\n", "\n").split("\n")).strip("\n")
        h = hashlib.sha1(norm.encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        tiles += 1
        lines = code.splitlines()
        for f in C.analyse(lines, [lk["name"] for lk in t.get("links") or []]):
            rec = classify(f.sev, f.msg, rules)
            if rec is None:
                key = f.msg.split(";")[0].split(" -- ")[0]
                key = re.sub(r"`[^`]*`", "`X`", key)
                key = re.sub(r"\d+", "N", key).strip()[:110]
                unclassified[key] = unclassified.get(key, 0) + 1
                continue
            rid = rec["id"]
            hits[rid] = hits.get(rid, 0) + 1
            if rec["_line_re"] and not rec["_line_re"].search(f.code or ""):
                bad_regex[rid] = bad_regex.get(rid, 0) + 1
            bucket = samples.setdefault(rid, [])
            if len(bucket) < 2:
                bucket.append(f"{Path(path_str).name}:{t['x']},{t['y']} L{f.line}  {(f.code or '')[:88]}")
            # One reviewable row per (rule, program), capped per rule so the table stays
            # something a human can actually go through.
            n_row = sum(1 for r in rows if r["rule"] == rid)
            if n_row < ROW_CAP:
                rows.append({
                    "rule": rid,
                    "sev": f.sev,
                    "file": Path(path_str).name,
                    "x": t.get("x"), "y": t.get("y"),
                    "block": t.get("block"),
                    "line": f.line,
                    "code": (f.code or "").strip(),
                    "msg": f.msg,
                    "hash": h[:12],
                })
    return {
        "file": Path(path_str).name,
        "tiles": tiles,
        "hits": hits,
        "bad_regex": bad_regex,
        "samples": samples,
        "unclassified": unclassified,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="folder of .msav (default: saves)")
    ap.add_argument("--limit", type=int, default=0, help="only the N largest files")
    ap.add_argument("--workers", type=int, default=0, help="0 = let parallel.py decide")
    ap.add_argument("--serial", action="store_true")
    ap.add_argument("--json", default="out/rule_audit.json")
    ap.add_argument("--md", default="out/rule_audit.md")
    ap.add_argument("--apply", action="store_true", help="write observed numbers into rules/*.json")
    ap.add_argument("--rows", default="", help="also dump reviewable rows to this JSON")
    args = ap.parse_args(argv)

    rules = load_rules()
    if not rules:
        print(f"no rules in {RULES_DIR}")
        return 1
    files = sorted(Path(args.dir).glob("*.msav"), key=lambda p: -p.stat().st_size)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no .msav under {args.dir}")
        return 1
    print(f"auditing {len(files)} save(s) from {args.dir} against {len(rules)} rule(s)")

    items = [str(p) for p in files]
    if args.serial:
        results = [audit_one(p) for p in items]
    else:
        results = parallel.run(audit_one, items, workers=args.workers or None,
                               label="rule audit", mem_per_worker_mb=192.0)

    hits: dict[str, int] = {}
    programs: dict[str, int] = {}
    bad: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    unclassified: dict[str, int] = {}
    review_rows: list[dict] = []
    errors = 0
    for r in results:
        if r.get("error"):
            errors += 1
            continue
        review_rows.extend(r.get("rows") or [])
        for rid, n in (r.get("hits") or {}).items():
            hits[rid] = hits.get(rid, 0) + n
            if n:
                programs[rid] = programs.get(rid, 0) + 1
        for rid, n in (r.get("bad_regex") or {}).items():
            bad[rid] = bad.get(rid, 0) + n
        for rid, ss in (r.get("samples") or {}).items():
            bucket = samples.setdefault(rid, [])
            for s in ss:
                if len(bucket) < 3:
                    bucket.append(s)
        for key, n in (r.get("unclassified") or {}).items():
            unclassified[key] = unclassified.get(key, 0) + n

    total = sum(hits.values()) + sum(unclassified.values())
    rows = []
    for rec in rules:
        rid = rec["id"]
        n = hits.get(rid, 0)
        if not n and rec.get("severity") == "SUPPRESSED":
            continue
        b = bad.get(rid, 0)
        rows.append({
            "id": rid,
            "kind": rec.get("kind", "?"),
            "sev": rec.get("severity", "?"),
            "hits": n,
            "programs": programs.get(rid, 0),
            "share": n / total if total else 0.0,
            "precision": (n - b) / n if n else None,
            "auto": bool(rec.get("auto_downgrade")),
            "samples": samples.get(rid, []),
        })
    rows.sort(key=lambda r: -r["hits"])

    lines = [f"# 规则噪声审计（{Path(args.dir).name}）", ""]
    lines.append(f"- 存档 {len(files)}（失败 {errors}）、发现 {total} 条，其中 "
                 f"{total - sum(unclassified.values())} 条落在 {len(rules)} 条规则里、"
                 f"{sum(unclassified.values())} 条未归类")
    lines.append("")
    lines.append("`precision` = 命中里源码行仍匹配该规则自己 `line_regex` 的比例。"
                 "100% 只代表「规则和它的说法一致」，**不代表结论正确**。")
    lines.append("")
    lines.append("| 规则 | 种类 | 级别 | 命中 | 程序 | 占比 | precision | 自动? |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|")
    for r in rows:
        prec = "—" if r["precision"] is None else f"{r['precision'] * 100:.0f}%"
        share = f"{r['share'] * 100:.1f}%"
        lines.append(f"| `{r['id']}` | {r['kind']} | {r['sev']} | {r['hits']:,} | "
                     f"{r['programs']} | {share} | {prec} | {'Y' if r['auto'] else ''} |")
    lines.append("")
    lines.append("## 每条的样本")
    lines.append("")
    flags = [r for r in rows if r["share"] >= 0.5 and r["hits"] >= 20]
    for r in rows:
        if not r["samples"]:
            continue
        mark = "  ← 占了一半以上，先怀疑它" if r in flags else ""
        lines.append(f"### `{r['id']}` — {r['hits']:,} 条{mark}")
        for s in r["samples"]:
            lines.append(f"- `{s}`")
        lines.append("")
    if unclassified:
        lines.append("## 未归类的发现（前 15）")
        lines.append("")
        lines.append("这些没有规则认领。高频项要么值得写一条规则（R5），要么是真的杂音。")
        lines.append("")
        for key, n in sorted(unclassified.items(), key=lambda kv: -kv[1])[:15]:
            lines.append(f"- {n:,} × `{key}`")
        lines.append("")

    Path(args.md).parent.mkdir(parents=True, exist_ok=True)
    Path(args.md).write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json).write_text(json.dumps({
        "dir": str(args.dir), "saves": len(files), "errors": errors,
        "total": total, "rules": rows, "unclassified": unclassified,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {args.md} and {args.json}")
    if args.rows:
        rows_path = Path(args.rows)
        rows_path.parent.mkdir(parents=True, exist_ok=True)
        # Stable id: same rule on the same line of the same program keeps its id even if the
        # save is re-downloaded or the file is renamed.
        for r in review_rows:
            r["id"] = hashlib.sha1(
                f"{r['rule']}|{r['hash']}|{r['line']}|{r['code']}".encode("utf-8")
            ).hexdigest()[:10]
        # The same rule can fire twice on one line (e.g. two different messages that both
        # match the pattern); for review purposes one row is enough.
        seen_ids: set[str] = set()
        unique = []
        for r in review_rows:
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])
            unique.append(r)
        rows_path.write_text(json.dumps(unique, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")
        print(f"wrote {len(unique)} reviewable row(s) to {rows_path}")
    for r in rows:
        if r["hits"]:
            prec = "—" if r["precision"] is None else f"{r['precision'] * 100:.0f}%"
            print(f"  {r['sev']:<5} {r['hits']:>7,}  {r['share'] * 100:>5.1f}%  "
                  f"prec {prec:>4}  {r['id']}")
    if unclassified:
        print(f"  {'?':<5} {sum(unclassified.values()):>7,}  "
              f"{sum(unclassified.values()) / total * 100:>5.1f}%  unclassified")

    if args.apply:
        print("\n--apply:")
        for r in rows:
            rec = next(x for x in rules if x["id"] == r["id"])
            rec["observed"] = {"hits": r["hits"], "programs": r["programs"],
                               "share": round(r["share"], 4), "dir": str(args.dir)}
            rec["_path"].write_text(
                json.dumps({k: v for k, v in rec.items() if not k.startswith("_")},
                           ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if r["auto"] and r["share"] >= 0.8 and r["sev"] == "WARN":
                print(f"  would downgrade `{r['id']}` WARN -> INFO (owns {r['share'] * 100:.0f}% "
                      f"of the report); recorded, not executed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
