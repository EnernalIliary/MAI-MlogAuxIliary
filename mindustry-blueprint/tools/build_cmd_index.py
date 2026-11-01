"""
build_cmd_index.py -- extract every command's reference from Logic-Docs-v1.1.0.

Pulls the 导出格式 (export format), 参数 table and 注意事项 for each command
chapter so they can be turned into a compact cheat-sheet.
"""
from __future__ import annotations

import json
import os
import re
import sys

DOC = os.path.join("Logic", "Logic-Docs-v1.1.0 (1).md")
OUT = os.path.join("tools", "cmd_index.json")


def main() -> None:
    with open(DOC, encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    # locate top-level command chapters (# NN - name)
    heads = []
    for i, l in enumerate(lines):
        m = re.match(r"^# (\d+) - (.+)$", l)
        if m:
            heads.append((i, m.group(1), m.group(2).strip()))
    heads.append((len(lines), None, None))

    chapters = []
    for k in range(len(heads) - 1):
        start, num, title = heads[k]
        end = heads[k + 1][0]
        body = lines[start:end]
        text = "\n".join(body)

        fmt = ""
        m = re.search(r"## 导出格式\s*\n+```+\s*\n(.+?)\n```+", text, re.S)
        if m:
            fmt = m.group(1).strip()
        else:
            m = re.search(r"## 导出格式\s*\n+```+\s*\n?(.+?)\n?```+", text, re.S)
            if m:
                fmt = m.group(1).strip()

        # first parameter table
        params = []
        tm = re.search(r"## 参数(?:说明)?\s*\n(.*?)(?=\n## )", text, re.S)
        if tm:
            for row in re.findall(r"^\|\s*([^|\n]+?)\s*\|(.+?)\|$", tm.group(1), re.M):
                cells = [c.strip() for c in row[1].split("|")]
                if row[0].startswith("---") or row[0] in ("参数", "参数位置", "位置"):
                    continue
                params.append((row[0], cells[0] if cells else ""))

        # 注意事项 bullets
        notes = []
        nm = re.search(r"## 注意(?:事项)?\s*\n(.*?)(?=\n## |\Z)", text, re.S)
        if nm:
            for b in re.findall(r"^[-*]\s+(.+)$", nm.group(1), re.M):
                b = re.sub(r"\*\*|`", "", b).strip()
                if b:
                    notes.append(b)

        chapters.append({
            "num": num, "title": title, "line": start + 1,
            "format": fmt, "params": params, "notes": notes[:6],
        })

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(chapters, fh, ensure_ascii=False, indent=1)

    print(f"extracted {len(chapters)} command chapters -> {OUT}\n")
    for c in chapters:
        first = c["format"].split("\n")[0] if c["format"] else "(no format block)"
        print(f"{c['num']:>3} | {c['title'][:34]:<34} | {first[:60]}")


if __name__ == "__main__":
    main()
