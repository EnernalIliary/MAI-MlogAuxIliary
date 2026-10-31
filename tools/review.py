"""逐条审计助手 —— 把「一堆发现」变成「你可以一条条判的清单」。

背景：分析器给出的是**行级**发现，一次几百上千条，读不了也没法回话。这个工具把
它们压成**可审的行**（每条规则在同一份程序里只留一条，见 `rule_audit.py --rows`），
给每行一个稳定 id，然后让你用四个词给它定案：

    ok      规则说得对（这是真问题）
    bad     规则说错了（这是噪声/误报）
    unsure  我看不出来（保留）
    skip    不想看（等同于没判）

判完 `stats` 会按规则算出你的准确率，`rule_capture.py` 用它决定该降级还是该删。

用法::

    python tools/review.py build --dir out/maps          # 扫一遍，生成 out/audit_rows.json
    python tools/review.py list --unjudged --limit 20    # 看还没判的
    python tools/review.py show 3f9c1a2b04               # 看这条所在的完整程序（标出该行）
    python tools/review.py bad 3f9c1a2b04 --note "mod 里的自定义方块"
    python tools/review.py stats                         # 每条规则你判了多少、错了几条
    python tools/review.py export                        # out/audit.md，按规则分组给人看
    python tools/review.py md --unjudged                 # out/audit_review.md，在编辑器里逐条填判定
    python tools/review.py ingest                        # 把 md 里填的判定读回来

一条龙（第一次用就这三行）::

    python tools/review.py run --pages 2      # 抓 30 张图 -> 分析 -> 写 out/audit_review.md
    # 在编辑器里给每条的 `判定:` 后面填 ok / bad / unsure / skip，可加 `备注:`
    python tools/review.py ingest && python tools/review.py stats

`md` / `run` 每次重写清单前都会先把上一份 md 里填过的判定读回来，填过的不会丢。
机器慢就加 `--serial`（不并行）或 `--pages 1`；只想分析本地已有图就 `run --pages 0`。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mindustry-blueprint" / "tools"))

import map_logic as ML  # noqa: E402
import mlogcheck as _check  # noqa: E402

ROWS = ROOT / "out" / "audit_rows.json"
VERDICTS = ROOT / "out" / "verdicts.csv"
SAVE_DIR = ROOT / "out" / "maps"

VERDICT_WORDS = {"ok": "ok", "bad": "bad", "unsure": "unsure", "skip": "skip"}


def load_rows() -> list[dict]:
    if not ROWS.exists():
        sys.exit(f"no {ROWS}; run: python tools/review.py build")
    return json.loads(ROWS.read_text(encoding="utf-8"))


def load_verdicts() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not VERDICTS.exists():
        return out
    with VERDICTS.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            out[row["id"]] = row
    return out


def save_verdict(fid: str, verdict: str, note: str) -> None:
    new = not VERDICTS.exists()
    VERDICTS.parent.mkdir(parents=True, exist_ok=True)
    with VERDICTS.open("a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["id", "verdict", "note", "when"])
        w.writerow([fid, verdict, note, time.strftime("%Y-%m-%d %H:%M")])


def find_row(rows: list[dict], fid: str) -> dict:
    for r in rows:
        if r["id"] == fid:
            return r
    # also allow an unambiguous prefix
    hits = [r for r in rows if r["id"].startswith(fid)]
    if len(hits) == 1:
        return hits[0]
    sys.exit(f"no such row: {fid}" + (f" ({len(hits)} prefixes match)" if hits else ""))


def cmd_build(args) -> int:
    import rule_audit

    argv = ["--dir", args.dir, "--rows", str(ROWS),
            "--md", str(ROOT / "out" / "rule_audit_maps.md"),
            "--json", str(ROOT / "out" / "rule_audit_maps.json")]
    if args.serial:
        argv.append("--serial")
    return rule_audit.main(argv)


def cmd_list(args) -> int:
    rows = load_rows()
    verd = load_verdicts()
    if args.rule:
        rows = [r for r in rows if args.rule.lower() in r["rule"].lower()]
    if args.unjudged:
        rows = [r for r in rows if r["id"] not in verd]
    if not rows:
        print("nothing to show")
        return 0
    per_rule = Counter(r["rule"] for r in rows)
    print(f"{len(rows)} row(s) across {len(per_rule)} rule(s)\n")
    print(f"{'id':<12} {'rule':<38} {'sev':<7} {'where':<34} line")
    for r in rows[: args.limit or len(rows)]:
        where = f"{Path(r['file']).stem[:22]}:{r['x']},{r['y']}"
        mark = "  " + verd[r["id"]]["verdict"] if r["id"] in verd else ""
        print(f"{r['id']:<12} {r['rule'][:38]:<38} {r['sev']:<7} {where:<34} "
              f"{r['line']}{mark}")
    if args.limit and len(rows) > args.limit:
        print(f"... {len(rows) - args.limit} more")
    return 0


def _load_code(row: dict) -> str:
    path = SAVE_DIR / row["file"]
    if not path.exists():
        sys.exit(f"save not cached: {path}")
    info = ML.read_save(path)
    for t in info.get("logic") or []:
        if t["x"] == row["x"] and t["y"] == row["y"]:
            return (t.get("code") or "")
    sys.exit(f"no processor at ({row['x']},{row['y']}) in {row['file']}")


def _blueprint_for(row: dict, code: str) -> str:
    """A one-processor schematic holding exactly this program, as a paste-able code.

    The point is that judging a finding should not require opening the save. Paste the
    code in-game (or open it in the editor) and the processor is right there, with the
    variable list the user reads anyway.

    Links are NOT re-created: a row about a missing link is about the name being absent,
    and inventing a target would hide that. The names still appear in the code.
    """
    import base64

    import mschgen as G

    name = f"audit-{row['id']}"[:40]
    desc = f"{row['rule']} @ {Path(row['file']).stem}:{row['x']},{row['y']} L{row['line']}"
    bp = G.Blueprint(name, desc, [])
    bp.add_logic(code, 0, 0, "micro-processor", 0, [])
    return base64.b64encode(bp.build()).decode("ascii")


def cmd_code(args) -> int:
    row = find_row(load_rows(), args.id)
    code = _load_code(row)
    b64 = _blueprint_for(row, code)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        import base64

        path.write_bytes(base64.b64decode(b64))
        print(f"wrote {path}  ({len(b64)} chars of code)")
    print(b64)
    return 0


def cmd_codes(args) -> int:
    rows = load_rows()
    verd = load_verdicts()
    if args.rule:
        rows = [r for r in rows if args.rule.lower() in r["rule"].lower()]
    if args.unjudged:
        rows = [r for r in rows if r["id"] not in verd]
    if args.limit:
        rows = rows[: args.limit]
    out_dir = ROOT / "out" / "review"
    out_dir.mkdir(parents=True, exist_ok=True)
    md = [f"# 可粘贴的蓝图码（{len(rows)} 条）", ""]
    md.append("每条一个微处理器，源码就是那张图里那台处理器的原文。")
    md.append("粘贴进游戏即可在编辑器里看变量列表；`out/review/*.msch` 也可以直接丢进")
    md.append("存档目录的 `schematics/` 让它在游戏里出现。")
    md.append("")
    for r in rows:
        code = _load_code(r)
        b64 = _blueprint_for(r, code)
        (out_dir / f"{r['id']}.msch").write_bytes(__import__("base64").b64decode(b64))
        md.append(f"## `{r['id']}` — {r['rule']}")
        md.append("")
        md.append(f"- 出处：`{Path(r['file']).stem}` ({r['x']},{r['y']}) {r.get('block')}")
        md.append(f"- 第 {r['line']} 行：`{(r['code'] or '').strip()}`")
        md.append(f"- 判定行：`{r['msg'][:180]}`")
        md.append("")
        md.append("```")
        md.append(b64)
        md.append("```")
        md.append("")
    path = ROOT / "out" / "audit_codes.md"
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {path} and {len(rows)} .msch file(s) in {out_dir}")
    return 0


def cmd_install(args) -> int:
    """Drop the review blueprints into the game's schematic folder."""
    rows = load_rows()
    verd = load_verdicts()
    if args.rule:
        rows = [r for r in rows if args.rule.lower() in r["rule"].lower()]
    if args.unjudged:
        rows = [r for r in rows if r["id"] not in verd]
    if args.limit:
        rows = rows[: args.limit]
    import base64

    import mschgen as G

    target = Path(G.default_schematics_dir() or "")
    if not target or not target.is_dir():
        sys.exit(f"schematic folder not found: {target}")
    target = target / "audit"
    target.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in rows:
        b64 = _blueprint_for(r, _load_code(r))
        (target / f"audit-{r['id']}.msch").write_bytes(base64.b64decode(b64))
        n += 1
    print(f"installed {n} blueprint(s) into {target}")
    return 0


def cmd_show(args) -> int:
    row = find_row(load_rows(), args.id)
    code = _load_code(row)
    line = int(row["line"])
    print(f"rule  : {row['rule']}  [{row['sev']}]")
    print(f"claim : {row['msg'][:160]}")
    print(f"where : {row['file']} ({row['x']},{row['y']}) {row.get('block')}  line {line}")
    print()
    lines = code.splitlines()
    # `line` is a STATEMENT index -- the number you would type into a `jump`.
    # Map it to the raw line so the window can show surrounding text, then label
    # every printed row with its statement index so the two agree.
    stmts = _check.prepare(lines)
    idx_of = {ln + 1: k for k, (ln, _c) in enumerate(stmts)}
    if 0 <= line < len(stmts):
        raw = stmts[line][0] + 1
    else:
        raw = max(1, min(len(lines), line + 1))
    lo = max(1, raw - args.context)
    hi = min(len(lines), raw + args.context)
    for i in range(lo, hi + 1):
        k = idx_of.get(i)
        mark = ">>>>" if k == line else "    "
        print(f"{mark} {k if k is not None else '-':>4} | {lines[i - 1]}")
    verdict = load_verdicts().get(row["id"])
    print()
    if verdict:
        print(f"verdict: {verdict['verdict']}  ({verdict.get('note') or 'no note'})")
    else:
        print("verdict: (none)  --  ok / bad / unsure / skip")
    return 0


def cmd_verdict(args, word: str) -> int:
    row = find_row(load_rows(), args.id)
    save_verdict(row["id"], word, args.note or "")
    print(f"{row['id']} -> {word}" + (f"  ({args.note})" if args.note else ""))
    return 0


def cmd_stats(args) -> int:
    rows = load_rows()
    verd = load_verdicts()
    by_rule: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        v = verd.get(r["id"])
        by_rule[r["rule"]][v["verdict"] if v else "unjudged"] += 1
    print(f"{'rule':<40} {'rows':>5} {'ok':>4} {'bad':>4} {'unsure':>7} {'left':>5}  verdict")
    for rule, c in sorted(by_rule.items(), key=lambda kv: -kv[1]["rows"]):
        judged = c["ok"] + c["bad"]
        if judged == 0:
            tail = "no verdict yet"
        elif c["bad"] == 0:
            tail = "100% right so far"
        else:
            tail = f"{c['ok'] / judged * 100:.0f}% right"
        print(f"{rule[:40]:<40} {c['rows']:>5} {c['ok']:>4} {c['bad']:>4} "
              f"{c['unsure']:>7} {c['unjudged']:>5}  {tail}")
    total = Counter(v["verdict"] for v in verd.values())
    print(f"\njudged {sum(total.values())} / {len(rows)} rows  "
          f"(ok {total['ok']}, bad {total['bad']}, unsure {total['unsure']}, skip {total['skip']})")
    return 0


def cmd_export(args) -> int:
    rows = load_rows()
    verd = load_verdicts()
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_rule[r["rule"]].append(r)
    out = [f"# 逐条待审清单（{len(rows)} 条）", ""]
    out.append("`python tools/review.py show <id>` 看上下文；`ok`/`bad`/`unsure` 判它。")
    out.append("")
    for rule, rs in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        c = Counter(verd[r["id"]]["verdict"] for r in rs if r["id"] in verd)
        mark = ""
        if c["bad"] and not c["ok"]:
            mark = "  ← **你判的全是误报，考虑降级或删除**"
        out.append(f"## `{rule}` — {len(rs)} 条{mark}")
        out.append("")
        out.append("| id | 级别 | 位置 | 行 | 源码 | 判定 |")
        out.append("|---|---|---|---|---|---|")
        for r in rs:
            v = verd.get(r["id"])
            code = (r["code"] or "").replace("|", "\\|")[:70]
            out.append(f"| `{r['id']}` | {r['sev']} | {Path(r['file']).stem}:{r['x']},{r['y']} "
                       f"| {r['line']} | `{code}` | {v['verdict'] if v else ''} |")
        out.append("")
        out.append(f"> {rs[0]['msg'][:220]}")
        out.append("")
    path = ROOT / "out" / "audit.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    return 0


MD_PATH = ROOT / "out" / "audit_review.md"

_HEAD = re.compile(r"^###\s+`([0-9a-f]{6,})`")
_VERDICT_LINE = re.compile(r"^\s*判定\s*[:：]\s*(\S*)\s*$")
_NOTE_LINE = re.compile(r"^\s*备注\s*[:：]\s*(.*)$")


def _program(row: dict) -> tuple[str, list[str]]:
    """The processor's full source and its own link names."""
    path = SAVE_DIR / row["file"]
    if not path.exists():
        sys.exit(f"save not cached: {path}")
    info = ML.read_save(path)
    for t in info.get("logic") or []:
        if t["x"] == row["x"] and t["y"] == row["y"]:
            names = [(l or {}).get("name") or "" for l in (t.get("links") or [])]
            return (t.get("code") or ""), [n for n in names if n]
    sys.exit(f"no processor at ({row['x']},{row['y']}) in {row['file']}")


def _listing(code_text: str, flag: int, window: int) -> list[str]:
    """Numbered statement listing with the flagged line marked. Numbers are statement
    indices (0-based) because that is what `jump N` in the code counts."""
    lines = [ln.rstrip() for ln in code_text.splitlines()]
    if len(lines) <= 2 * window:
        lo, hi = 0, len(lines)
    else:
        lo, hi = max(0, flag - window), min(len(lines), flag + window + 1)
    out: list[str] = []
    if lo > 0:
        out.append(f"      ... 上面还有 {lo} 条语句")
    for i in range(lo, hi):
        mark = ">>>>" if i == flag else "    "
        out.append(f"{mark}{i:>4}  {lines[i]}")
    if hi < len(lines):
        out.append(f"      ... 下面还有 {len(lines) - hi} 条语句")
    return out


def cmd_md(args) -> int:
    if not getattr(args, "no_ingest", False) and MD_PATH.exists():
        print("reading back the verdicts from the previous md ...")
        try:
            cmd_ingest(_NS(MD_PATH))
        except SystemExit:
            print("  (the old md could not be read; writing a fresh one)")
        print()
    rows = load_rows()
    verd = load_verdicts()
    if args.rule:
        rows = [r for r in rows if args.rule.lower() in r["rule"].lower()]
    if args.unjudged:
        rows = [r for r in rows if r["id"] not in verd]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("nothing to write")
        return 0

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["rule"]].append(r)
    order = sorted(groups, key=lambda k: (-len(groups[k]), k))

    out: list[str] = [f"# 逐条审计（{len(rows)} 条，{len(groups)} 条规则）", ""]
    out.append("判法：每条末尾有 `判定:` 一行，在后面填一个词 ——")
    out.append("`ok`（规则说得对）、`bad`（规则说错了）、`unsure`（看不出来）、`skip`（不想看）。")
    out.append("`备注:` 后面可以写一句原因，会被记进那条规则的历史。")
    out.append("")
    out.append("填完在仓库根目录跑 `python tools/review.py ingest`，判定会写进 `out/verdicts.csv`，")
    out.append("`python tools/review.py stats` 就按规则算准确率。")
    out.append("")
    out.append("**行号是语句序号（0 起）**，和代码里 `jump N` 用的是同一个数；`>>>>` 标的是被指的那一条。")
    out.append("处理器自己的链接名单独列出，所以「名字指错了」这类判定不需要打开地图。")
    out.append("")
    out.append("## 目录")
    out.append("")
    out.append("| 规则 | 条数 | 已判 |")
    out.append("|---|---|---|")
    for rule in order:
        done = sum(1 for r in groups[rule] if r["id"] in verd)
        out.append(f"| `{rule}` | {len(groups[rule])} | {done} |")
    out.append("")

    cache: dict[tuple, tuple[str, list[str]]] = {}
    for rule in order:
        out.append(f"## 规则 `{rule}`")
        out.append("")
        for r in groups[rule]:
            key = (r["file"], r["x"], r["y"])
            if key not in cache:
                cache[key] = _program(r)
            code_text, links = cache[key]
            lines = code_text.splitlines()
            prev = verd.get(r["id"], {})
            out.append(f"### `{r['id']}`")
            out.append("")
            out.append(
                f"- 出处：`{Path(r['file']).stem}` · ({r['x']},{r['y']}) · "
                f"{r.get('block')} · {len(lines)} 条语句"
            )
            shown = ("`" + "`, `".join(links) + "`") if links else "（无）"
            out.append(f"- 处理器自己的链接：{shown}")
            out.append(f"- 被指行：语句序号 **{r['line']}**")
            out.append(f"- 规则说：{r['msg']}")
            out.append("")
            out.append("```")
            out.extend(_listing(code_text, int(r["line"]), args.window))
            out.append("```")
            out.append("")
            out.append(f"判定: {prev.get('verdict', '')}".rstrip())
            out.append(f"备注: {prev.get('note', '')}".rstrip())
            out.append("")

    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {path}  ({len(rows)} row(s), {len(groups)} rule(s))")
    return 0


def cmd_ingest(args) -> int:
    """Read the `判定:` / `备注:` lines back out of an edited markdown file."""
    path = Path(args.path)
    if not path.exists():
        sys.exit(f"no {path}; run: python tools/review.py md")
    known = {r["id"] for r in load_rows()}
    cur: str | None = None
    found: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _HEAD.match(line)
        if m:
            cur = m.group(1)
            found.setdefault(cur, {"verdict": "", "note": "", "bad": ""})
            continue
        if cur is None:
            continue
        mv = _VERDICT_LINE.match(line)
        if mv:
            word = mv.group(1).strip().strip("`*").lower()
            if word in VERDICT_WORDS:
                found[cur]["verdict"] = word
            elif word:
                found[cur]["bad"] = word
            continue
        mn = _NOTE_LINE.match(line)
        if mn:
            found[cur]["note"] = mn.group(1).strip()

    written, problems, unknown = 0, [], []
    for fid, rec in found.items():
        if fid not in known:
            unknown.append(fid)
            continue
        if rec["bad"]:
            problems.append((fid, rec["bad"]))
            continue
        if rec["verdict"]:
            save_verdict(fid, rec["verdict"], rec["note"])
            written += 1

    print(f"read {len(found)} row(s), wrote {written} verdict(s)")
    if problems:
        print(f"\n{len(problems)} 行的判定不是四个词之一，跳过：")
        for fid, word in problems[:10]:
            print(f"  {fid}  ->  {word!r}")
        if len(problems) > 10:
            print(f"  ... 还有 {len(problems) - 10} 行")
    if unknown:
        print(f"\n{len(unknown)} 个 id 不在 out/audit_rows.json 里（重建过清单？先跑 build）：")
        for fid in unknown[:5]:
            print(f"  {fid}")
    print(f"\n统计：python tools/review.py stats")
    return 0 if not problems else 1


class _NS:
    """Namespace shim so cmd_md can reuse the ingest parser."""

    def __init__(self, path) -> None:
        self.path = str(path)


def cmd_fetch(args) -> int:
    """Download maps from api.mindustry.top into --dir (skips ones you have)."""
    import map_site as S

    dest = Path(args.dir)
    dest.mkdir(parents=True, exist_ok=True)
    got, had, failed = 0, 0, 0
    for page in range(max(1, args.pages)):
        try:
            batch = S.list_maps(search=args.search, begin=15 * page)
        except Exception as e:  # network, 4xx, whatever
            print(f"page {page}: {e}")
            break
        if not batch:
            print(f"page {page}: no more maps")
            break
        for m in batch:
            if args.limit and got >= args.limit:
                break
            mid = m.get("id")
            target = dest / f"mindustrytop-{mid}.msav"
            if target.exists():
                had += 1
                continue
            try:
                S.download(mid, target)
            except Exception as e:
                failed += 1
                print(f"  {mid:>7}  failed: {e}")
                continue
            got += 1
            print(f"  {mid:>7}  {str(m.get('name', ''))[:44]}")
    print(f"fetched {got} new, {had} already here, {failed} failed -> {dest}")
    return 0


def cmd_run(args) -> int:
    """fetch (optional) -> build -> md, in that order."""
    if args.pages:
        rc = cmd_fetch(args)
        if rc:
            return rc
    rc = cmd_build(args)
    if rc:
        return rc
    args.limit = 0          # --limit was for fetching; do not truncate the list
    return cmd_md(args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="scan a folder and write out/audit_rows.json")
    b.add_argument("--dir", default=str(SAVE_DIR))
    b.add_argument("--serial", action="store_true")
    b.set_defaults(fn=cmd_build)

    ls = sub.add_parser("list", help="table of rows")
    ls.add_argument("--rule", default="")
    ls.add_argument("--unjudged", action="store_true")
    ls.add_argument("--limit", type=int, default=0)
    ls.set_defaults(fn=cmd_list)

    sh = sub.add_parser("show", help="show one row with context")
    sh.add_argument("id")
    sh.add_argument("--context", type=int, default=6)
    sh.set_defaults(fn=cmd_show)

    cd = sub.add_parser("code", help="print a paste-able blueprint code holding this program")
    cd.add_argument("id")
    cd.add_argument("--out", default="", help="also write the .msch file here")
    cd.set_defaults(fn=cmd_code)

    cs = sub.add_parser("codes", help="write out/audit_codes.md + out/review/*.msch")
    cs.add_argument("--rule", default="")
    cs.add_argument("--unjudged", action="store_true")
    cs.add_argument("--limit", type=int, default=0)
    cs.set_defaults(fn=cmd_codes)

    ins = sub.add_parser("install", help="drop the blueprints into the game's schematics/audit")
    ins.add_argument("--rule", default="")
    ins.add_argument("--unjudged", action="store_true")
    ins.add_argument("--limit", type=int, default=0)
    ins.set_defaults(fn=cmd_install)

    md = sub.add_parser("md", help="write out/audit_review.md: paste-able code + a verdict slot")
    md.add_argument("--rule", default="")
    md.add_argument("--unjudged", action="store_true")
    md.add_argument("--limit", type=int, default=0)
    md.add_argument("--window", type=int, default=30, help="statements of context each side")
    md.add_argument("--out", default=str(MD_PATH))
    md.add_argument("--no-ingest", action="store_true",
                     help="do not read the previous md first (loses no verdicts, it just skips it)")
    md.set_defaults(fn=cmd_md)

    ig = sub.add_parser("ingest", help="read the verdicts you typed into the markdown back in")
    ig.add_argument("path", nargs="?", default=str(MD_PATH))
    ig.set_defaults(fn=cmd_ingest)

    for word in VERDICT_WORDS:
        vp = sub.add_parser(word, help=f"mark a row as {word}")
        vp.add_argument("id")
        vp.add_argument("--note", default="")
        vp.set_defaults(fn=lambda a, w=word: cmd_verdict(a, w))

    fe = sub.add_parser("fetch", help="download maps from api.mindustry.top into out/maps")
    fe.add_argument("--pages", type=int, default=1, help="15 maps per page")
    fe.add_argument("--search", default=None, help="the site searches names/descriptions only")
    fe.add_argument("--limit", type=int, default=0, help="stop after N new maps")
    fe.add_argument("--dir", default=str(ROOT / "out" / "maps"))
    fe.set_defaults(fn=cmd_fetch)

    rn = sub.add_parser("run", help="fetch + build + md in one go")
    rn.add_argument("--pages", type=int, default=1, help="0 = skip fetching, analyse what is here")
    rn.add_argument("--search", default=None)
    rn.add_argument("--limit", type=int, default=0)
    rn.add_argument("--dir", default=str(ROOT / "out" / "maps"))
    rn.add_argument("--workers", type=int, default=0, help="0 = let parallel.py decide")
    rn.add_argument("--serial", action="store_true", help="no multiprocessing (weak machine)")
    rn.add_argument("--window", type=int, default=30)
    rn.add_argument("--out", default=str(MD_PATH))
    rn.set_defaults(fn=cmd_run, rule=None, unjudged=False, no_ingest=False,
                    md=str(MD_PATH))
    st = sub.add_parser("stats", help="per-rule verdict tally")
    st.set_defaults(fn=cmd_stats)

    ex = sub.add_parser("export", help="write out/audit.md")
    ex.set_defaults(fn=cmd_export)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
