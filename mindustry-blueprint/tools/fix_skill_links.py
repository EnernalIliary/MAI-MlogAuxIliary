"""
fix_skill_links.py -- make relative links in the skill docs work from wherever
the skill sits.

The docs reference the Mindustry source checkout (`Mindustry-Source/...`) and the
workspace `tools/`. Those live at the WORKSPACE ROOT, so the number of `../`
needed depends on how deep the skill is installed:

    <root>/mindustry-blueprint/REFERENCE.md             -> ../
    <root>/.dsh/skills/mindustry-blueprint/REFERENCE.md -> ../../../

Rather than hardcode a depth, this script HUNTS for the workspace root by walking
up until it finds `Mindustry-Source`, then rewrites every relative link to be
correct for that position.

Usage:
    python fix_skill_links.py <skill-dir> [<workspace-root>]
"""
from __future__ import annotations

import os
import re
import sys

MARKER = "Mindustry-Source"
SKIP_SCHEMES = ("http://", "https://", "mailto:", "#")

ROOT_ONLY = {
    "block_ids_from_source.py", "block_sizes.py", "fetch_wiki_ids.py",
    "build_cmd_index.py", "scan_all.py", "extract_logic.py",
    "analyze_logic.py", "rank_logic.py", "mine_idioms.py",
    "scan_mod_blocks.py", "fix_skill_links.py",
}


def find_root(start: str, explicit: str | None = None) -> str | None:
    if explicit:
        return os.path.abspath(explicit)
    cur = os.path.abspath(start)
    for _ in range(12):
        if os.path.isdir(os.path.join(cur, MARKER)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    skill = os.path.abspath(sys.argv[1])
    root = find_root(skill, sys.argv[2] if len(sys.argv) > 2 else None)
    if not root:
        print(f"could not find a '{MARKER}' directory above {skill}", file=sys.stderr)
        return 1

    rel_up = os.path.relpath(root, skill)
    prefix = "" if rel_up == "." else rel_up.replace(os.sep, "/") + "/"
    print(f"skill : {skill}")
    print(f"root  : {root}")
    print(f"prefix: {prefix!r}   (how docs must reach the workspace root)")
    print()

    docs = [f for f in sorted(os.listdir(skill)) if f.endswith(".md")]
    for name in docs:
        path = os.path.join(skill, name)
        text = open(path, encoding="utf-8").read()
        before = text

        # 1) Mindustry-Source links -> always correct depth
        text = re.sub(r"\]\((?:\.\./)*Mindustry-Source/",
                      f"]({prefix}{MARKER}/", text)
        text = re.sub(r"\]\((?:\.\./)*logic-corpus/",
                      f"]({prefix}logic-corpus/", text)

        # 2) cross-doc links (old filenames -> the names inside this skill)
        text = re.sub(r"\]\(蓝图逻辑模式库\.md", "](PATTERNS.md", text)
        text = re.sub(r"\]\(蓝图层逐类精讲\.md", "](BLUEPRINT-ANALYSIS.md", text)

        # 3) tool links
        def fix_tool(m: re.Match) -> str:
            fname = m.group(1)
            if fname in ROOT_ONLY:
                return f"]({prefix}tools/{fname})"
            return f"](tools/{fname})"

        text = re.sub(r"\]\((?:\.\./)*tools/([A-Za-z0-9_]+\.py)\)", fix_tool, text)

        if text != before:
            open(path, "w", encoding="utf-8").write(text)
            print(f"  fixed {name}")
        else:
            print(f"  unchanged {name}")

    print()
    total_broken = 0
    for name in docs:
        path = os.path.join(skill, name)
        t = open(path, encoding="utf-8").read()
        links = set(re.findall(r"\]\(([^)#]+?)(?:#[^)]*)?\)", t))
        broken = []
        for l in links:
            if l.startswith(SKIP_SCHEMES):
                continue
            if not os.path.exists(os.path.normpath(os.path.join(skill, l))):
                broken.append(l)
        total_broken += len(broken)
        flag = "OK" if not broken else f"{len(broken)} BROKEN"
        print(f"  {name}: {len(links)} links -- {flag}")
        for b in sorted(broken)[:5]:
            print("      ", b)
    return 1 if total_broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
