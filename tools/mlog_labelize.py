#!/usr/bin/env python3
"""Turn numeric jump targets into labels, so a .mlog is easier to edit.

Usage:
    python tools/mlog_labelize.py Logic/foo.mlog             # writes foo.labels.mlog
    python tools/mlog_labelize.py Logic/foo.mlog -o out.mlog
    python tools/mlog_labelize.py Logic/foo.mlog --prefix J  # J12: instead of L12

What it does
------------
* A label line (`name:`) is not a statement: the assembler gives it no slot,
  so inserting labels does **not** shift any statement index.
* Every `jump N ...` whose N is a literal inside 0..statement-count-1 gets N
  replaced by `L<N>` (or an existing label directly above that statement).
* Labels are inserted immediately before their target statement.
* Jumps to -1 / out of range, and jumps that already use a label, are left
  alone and reported on stderr.
* Comments and blank lines are preserved.

The result is for *editing*. Mindustry's LAssembler accepts `label:` lines, but
for delivery run the same post-process as the signature:

    python tools/aigenerated.py post foo.labels.mlog -o foo.mlog

That turns labels back into line numbers and re-signs in one step; the round
trip is byte-identical to the original numeric file.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import mlogir  # noqa: E402

LABEL_RE = re.compile(r"^\s*[A-Za-z_][\w.\-]*:\s*$")
JUMP_RE = re.compile(r"^(\s*jump\s+)(-?\d+)(\s+.*)?$", re.I)


def split_lines(text: str):
    """Return (lines, kinds, stmt_index) where kind is blank/comment/label/stmt."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    kinds, idx = [], 0
    for line in lines:
        s = line.strip()
        if not s:
            kinds.append(("blank", None))
        elif s.startswith("#"):
            kinds.append(("comment", None))
        elif LABEL_RE.match(line):
            kinds.append(("label", None))
        else:
            kinds.append(("stmt", idx))
            idx += 1
    return lines, kinds, idx


def existing_label_above(lines, kinds, line_no: int) -> str | None:
    """Label on the line directly above (skipping blanks only)."""
    j = line_no - 1
    while j >= 0 and kinds[j][0] == "blank":
        j -= 1
    if j >= 0 and kinds[j][0] == "label":
        return lines[j].strip().rstrip(":")
    return None


def labelize(text: str, prefix: str = "L"):
    lines, kinds, total = split_lines(text)
    jumps = []          # (line_no, target)
    for i, (kind, idx) in enumerate(kinds):
        if kind != "stmt":
            continue
        m = JUMP_RE.match(lines[i])
        if not m:
            continue
        target = int(m.group(2))
        if 0 <= target < total:
            jumps.append((i, int(m.group(2))))

    targets = sorted({t for _, t in jumps})
    insert_at: dict[int, list[str]] = {}
    names: dict[int, str] = {}
    used = {k for k in (ln.strip().rstrip(":") for ln, kd in zip(lines, kinds) if kd[0] == "label")}

    for t in targets:
        # find the source line of statement t
        line_no = next(i for i, (kind, idx) in enumerate(kinds) if kind == "stmt" and idx == t)
        above = existing_label_above(lines, kinds, line_no)
        if above:
            names[t] = above
            continue
        name = f"{prefix}{t}"
        n = 2
        while name in used:
            name = f"{prefix}{t}_{n}"
            n += 1
        used.add(name)
        names[t] = name
        insert_at.setdefault(line_no, []).append(f"{name}:")

    for i, target in jumps:
        m = JUMP_RE.match(lines[i])
        lines[i] = f"{m.group(1)}{names[target]}{m.group(3) or ''}"

    out = []
    for i, line in enumerate(lines):
        for name in insert_at.get(i, []):
            out.append(name)
        out.append(line)
    return "\n".join(out) + "\n", total, len(jumps), sorted(set(names.values()))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="convert numeric jump targets to labels")
    ap.add_argument("file", help="input .mlog")
    ap.add_argument("-o", "--output", help="output path (default: <stem>.labels.mlog)")
    ap.add_argument("--prefix", default="L", help="label prefix (default: L)")
    ap.add_argument("--check", action="store_true", help="run mlog.py check on the result")
    ap.add_argument("--sign", action="store_true",
                    help="re-apply the @aigenerated signature (automatic if the input is signed)")
    args = ap.parse_args(argv)

    src = Path(args.file)
    if not src.is_file():
        print(f"missing input: {src}", file=sys.stderr)
        return 1
    text = src.read_text(encoding="utf-8-sig")

    # Labels change the body text, so an existing signature would no longer
    # verify. Strip it, labelize, then sign the result again (attribution must
    # survive a formatting pass).
    import aigenerated
    signed = not aigenerated.verify_text(text)
    author = aigenerated.DEFAULT_AUTHOR
    comments = []
    if signed:
        m = re.search(r'"aigenerated:([^":]+):[0-9a-f]{12}"', text)
        if m:
            author = m.group(1)
        comments = aigenerated.extract_text(text)
        text = aigenerated._canonical(aigenerated._clean(text))
    else:
        comments = aigenerated.collect_comments(text)

    out_text, total, njumps, labels = labelize(text, args.prefix)
    pid = None
    if signed or args.sign:
        out_text, pid = aigenerated.sign_text(
            out_text, author, force=True, comments=comments)
        if comments:
            # visible for editing; the token keeps the original line numbers
            out_text, pid, _ = aigenerated.restore_text(out_text)

    dst = Path(args.output) if args.output else src.with_suffix(".labels.mlog")
    dst.write_text(out_text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    print(f"labelized {src.name}: {total} statements, {njumps} jump(s), {len(labels)} label(s) -> {dst}")
    if signed or args.sign:
        print(f"re-signed as {author} (id {pid})")

    if args.check:
        import subprocess
        r = subprocess.run([sys.executable, str(HERE / "mlog.py"), "check", str(dst)])
        if r.returncode:
            return r.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
