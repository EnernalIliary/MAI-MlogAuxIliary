"""Hidden @aigenerated signature for AI-written mlog.

    python tools/aigenerated.py sign   Logic/foo.mlog [--author 猪猪鱼] [--force]
    python tools/aigenerated.py verify Logic/foo.mlog
    python tools/aigenerated.py strip  Logic/foo.mlog   # always refused

Where the mark lives
--------------------
Preferred: if the program's last real statement is an unconditional
`jump <n> always <x>`, the token replaces its unused `compare` slot (the
fourth and last operand; `always` never reads it):

    jump 27 always 0 "aigenerated:猪猪鱼:1f02465d9e75"

A bare `jump <n> always` (no value operand) gets one spelled out as `0` first,
because `always` never reads it either:

    jump 27 always 0 "aigenerated:猪猪鱼:1f02465d9e75"

Either way the `end` that used to follow such a jump is dropped: an
unconditional jump never falls through, so that `end` could not run and
`mlogcheck` reports it as an unreachable statement (v0.13.2 rule
`unreachable-statement`).  Signing therefore never adds a statement and never
leaves a dead one behind.

Otherwise the closing `end` becomes

    jump 0 always 0 "aigenerated:猪猪鱼:1f02465d9e75"

`end` means "next instruction reads 0"; `jump 0 always` does the same thing, so
the program behaves identically.  The token sits in a declared `JumpStatement`
operand that `always` never reads, so the game's own `LAssembler.read` /
`LAssembler.write` round-trip re-emits it verbatim (measured on v160.7 with the
four-operand form, no `end`: `b.code` comes back byte-identical).  A surplus
token after `end`, `set` or `op` is *not* used: `LogicIO.write` drops everything
past the declared arity (measured on v160.7, see mlog工作笔记).  A program with
no `end` gets one appended by the canonical form, then the same replacement
applies.

Why not a comment?
-----------------
`#` comments are stripped when a blueprint is compiled, and the in-game editor
makes a leading comment obvious.

Removal policy
--------------
There is deliberately no `strip` mode: the signature is an attribution term.
Anyone can edit the text, but `verify` re-hashes the body without the token,
so deleting it or changing the logic both fail.  Re-sign after an intentional
edit with `sign --force`; that produces a new id.

The id is the first 12 hex of SHA-256 over `author + "\\n" + body`, with the
body normalised to LF, the signature line removed, and a trailing `end`
guaranteed (so signing is idempotent even for programs that omitted it).
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import zlib
from pathlib import Path

DEFAULT_AUTHOR = "猪猪鱼"
# the sidecar (tools/logicd.py, tools/jobqueue.py) signs with this one; the
# environment override exists so a build can attribute a different author
SIGN_AUTHOR = os.environ.get("MAI_SIGN_AUTHOR") or DEFAULT_AUTHOR
SIG_LINE = 'jump 0 always 0 "aigenerated:{author}:{pid}{payload}"'
# addr/value are not captured: author/id/payload stay at groups 1/2/3
SIG_RE = re.compile(
    r'^jump\s+\S+\s+always\s+\S+\s+"aigenerated:([^":]+):([0-9a-f]{12})'
    r'(?::([A-Za-z0-9_-]+))?"\s*$')
SIG_PARTS_RE = re.compile(r'^jump\s+(\S+)\s+always\s+(\S+)\s+"aigenerated:')
# older releases used a comment header + unreachable set; drop those on re-sign
OLD_MARK_RE = re.compile(
    r"^(?:# @aigenerated(?:-end)?\b.*|set aigenerated\.[0-9a-f]{12}\b.*)$")
# release 0.9 put the token after `end`; normalise that back to a plain `end`
OLD_TAIL_RE = re.compile(r'^end\s+"aigenerated:[^"]*"\s*$')


class SignError(Exception):
    pass


def _read(path: Path) -> str:
    return (path.read_text(encoding="utf-8-sig")
            .replace("\r\n", "\n").replace("\r", "\n"))


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _clean(text: str) -> list[str]:
    """Normalise newlines and drop every existing signature / legacy mark."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for line in text.split("\n"):
        if line.strip().startswith("#"):
            continue
        if SIG_RE.match(line.strip()):
            m = SIG_PARTS_RE.match(line.strip())
            if m and m.group(1) != "0":
                # signature sat in an existing jump's compare slot
                out.append(f"jump {m.group(1)} always {m.group(2)}")
            continue
        if OLD_MARK_RE.match(line):
            continue
        if OLD_TAIL_RE.match(line.strip()):
            out.append("end")
            continue
        out.append(line)
    return out


def _canonical(lines: list[str]) -> str:
    """Signature-free body, guaranteed to end with a single `end`."""
    lines = list(lines)
    while lines and not lines[-1].strip():
        lines.pop()
    last = -1
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if s and not s.startswith("#"):
            last = i
            break
    if last < 0:
        return ""
    trailing = [l for l in lines[last + 1:] if l.strip().startswith("#")]
    head = lines[:last + 1]
    if head[last].strip() == "end":
        head = head[:last]
    # a trailing `jump 0 always ...` is exactly an `end`; normalise it away so
    # signing in its value slot keeps the id stable
    while head and re.match(r'^jump\s+0\s+always(?:\s+\S+)?\s*$', head[-1].strip()):
        head.pop()
    return "\n".join(head + trailing + ["end"]) + "\n"


def _body(text: str) -> str:
    return _canonical(_clean(text))


_LABEL_LINE = re.compile(r"^\s*([A-Za-z_][\w.\-]*):\s*$")


def collect_comments(text: str) -> list[dict]:
    """Comments with their original 1-based line and statement anchor.

    `stmt` is the number of statements before the comment; label lines do not
    count, so the anchor survives `resolve_labels` and re-labelling.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[dict] = []
    stmt = 0
    for no, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            out.append({"line": no, "stmt": stmt, "text": s})
            continue
        if _LABEL_LINE.match(line):
            continue
        stmt += 1
    return out


def encode_comments(comments: list[dict]) -> str:
    if not comments:
        return ""
    raw = json.dumps(
        [[c["line"], c["stmt"], c["text"]] for c in comments],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    comp = zlib.compress(raw, 9)
    blob = (b"z" + comp) if len(comp) < len(raw) else (b"j" + raw)
    return base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")


def decode_comments(payload: str | None) -> list[dict]:
    if not payload:
        return []
    pad = "=" * (-len(payload) % 4)
    try:
        blob = base64.urlsafe_b64decode(payload + pad)
        raw = zlib.decompress(blob[1:]) if blob[:1] == b"z" else blob[1:]
        rows = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, zlib.error, json.JSONDecodeError):
        raise SignError("bad comment payload in the signature")
    return [{"line": int(r[0]), "stmt": int(r[1]), "text": str(r[2])} for r in rows]


def _hash(author: str, body: str) -> str:
    return hashlib.sha256((author + "\n" + body).encode("utf-8")).hexdigest()


def _find(text: str):
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return [m for m in (SIG_RE.match(ln.strip()) for ln in text.split("\n")) if m]


def sign_text(text: str, author: str = DEFAULT_AUTHOR, force: bool = False,
              comments: list[dict] | None = None):
    """Return (signed_text, id).  Raises SignError if it would change an id.

    Comments are moved into the signature string (base64 payload), so the
    delivered code carries them without occupying lines: no `#` line survives in
    the signed text (the game's assembler drops them, and `b.code` is rebuilt from
    the assembled program, so a kept one would look like a write that never
    landed).  The id still hashes the canonical body, comments included.
    """
    hits = _find(text)
    body = _body(text)
    # an already-signed program whose body is nothing but the signature is fine to
    # re-sign (its canonical body is ""); only a text with nothing in it at all is
    # refused, because there would be no statement to attach the token to
    if not body and not hits:
        raise SignError("no statement to attach the signature to")
    author = re.sub(r'[:"]', "", author).strip() or DEFAULT_AUTHOR
    # the body always ends with `end`; when the statement just before it is an
    # unconditional jump the token goes into that jump's value slot instead, and
    # the `end` behind it is dropped (see the comment further down).  The id has
    # to hash the body `verify_text` will rebuild from the signed text, so a
    # `jump <n> always` tail is spelled out as `jump <n> always 0` first.
    lines = body.rstrip("\n").split("\n") if body else [""]
    # comments travel in the signature payload, never as lines of the delivered
    # code: the game's assembler drops `#` lines, and `b.code` -- which the mod
    # compares with the job's code character by character -- is rebuilt from the
    # assembled program, so a kept comment line would read as "the write did not
    # land".  The canonical body still carries them, which is what the id hashes.
    lines = [l for l in lines if not l.strip().startswith("#")]
    idx = len(lines) - 2
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    slot = None
    if idx >= 0:
        m = re.match(r'^(jump\s+(\S+)\s+always)\s+(\S+)\s*$', lines[idx].strip())
        if m and m.group(2) != "0":
            slot = f"{m.group(1)} {m.group(3)}"
        elif m is None:
            m = re.match(r'^(jump\s+(\S+)\s+always)\s*$', lines[idx].strip())
            if m and m.group(2) != "0":
                slot = f"{m.group(1)} 0"
                lines[idx] = slot
                body = "\n".join(lines) + "\n"
    if idx < 0:
        # a program of nothing but `end` has no statement to hang the token on:
        # the signed text is the terminal jump alone, and verify_text() rebuilds
        # "" from it, so "" is what the id hashes (re-signing stays idempotent)
        body = ""
    pid = _hash(author, body)[:12]
    if hits:
        old_id, old_author = hits[0].group(2), hits[0].group(1)
        if old_id == pid and old_author == author and not force:
            return text, pid
        if (old_id != pid or old_author != author) and not force:
            raise SignError(
                f"body changed since signing (id {old_id} -> {pid}); "
                f"pass --force to re-sign")
    cs = collect_comments(text) if comments is None else comments
    old = (decode_comments(hits[0].group(3))
           if hits and hits[0].group(3) else [])
    if comments is None and old:
        if len(cs) == len(old):
            # keep the original line numbers, refresh the text/anchors
            for o, c in zip(old, cs):
                c["line"] = o["line"]
        else:
            cs = old
    enc = encode_comments(cs)
    payload = f":{enc}" if enc else ""
    sig = f'"aigenerated:{author}:{pid}{payload}"'
    if slot is not None:
        lines[idx] = f"{slot} {sig}"
        # an unconditional jump never falls through: the `end` behind it can
        # never run, and mlogcheck reports exactly that as an unreachable
        # statement, so the signed text stops at the jump
        lines.pop()
    else:
        lines[-1] = f"jump 0 always 0 {sig}"
    return "\n".join(lines) + "\n", pid


def verify_text(text: str) -> list[str]:
    hits = _find(text)
    if not hits:
        return ['missing hidden @aigenerated argument '
                '(expected `jump 0 always 0 "aigenerated:<author>:<id>"`)']
    if len(hits) > 1:
        return [f"{len(hits)} @aigenerated arguments found, expected 1"]
    author, want = hits[0].group(1), hits[0].group(2)
    got = _hash(author, _body(text))[:12]
    if want != got:
        return [f"body changed after signing (id {want} != {got})"]
    return []


def sign(path: Path, author: str, force: bool) -> str:
    text = _read(path)
    hits = _find(text)
    body = _body(text)
    if not body:
        raise SignError(f"{path}: no statement to attach the signature to")
    pid = _hash(author, body)[:12]
    if hits:
        old_id, old_author = hits[0].group(2), hits[0].group(1)
        if old_id == pid and old_author == author and not force:
            return f"already signed  {path}  id={pid}"
        if (old_id != pid or old_author != author) and not force:
            raise SignError(
                f"{path}: body changed since signing "
                f"(id {old_id} -> {pid}); pass --force to re-sign")

    signed, pid = sign_text(text, author, force=True)
    _write(path, signed)
    return f"signed  {path}  id={pid}  author={author}"


def verify(path: Path) -> list[str]:
    return [f"{path}: {p}" for p in verify_text(_read(path))]


LABEL_LINE_RE = re.compile(r"^\s*([A-Za-z_][\w.\-]*):\s*$")
JUMP_LABEL_RE = re.compile(r"^(\s*jump\s+)([A-Za-z_][\w.\-]*)(\s+.*)?$", re.I)


def resolve_labels(text: str) -> tuple[str, int, int]:
    """Turn `jump label` back into `jump N` and drop the label lines.

    A label line does not occupy a statement slot, so `label -> N` is the
    number of statements before it. Unknown labels are left untouched.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    labels: dict[str, int] = {}
    kinds: list[str] = []
    idx = 0
    for line in lines:
        s = line.strip()
        m = LABEL_LINE_RE.match(line)
        if not s or s.startswith("#"):
            kinds.append("keep")
        elif m:
            labels[m.group(1)] = idx
            kinds.append("label")
        else:
            kinds.append("stmt")
            idx += 1
    out: list[str] = []
    rewritten = 0
    for line, kind in zip(lines, kinds):
        if kind == "label":
            continue
        if kind == "stmt":
            m = JUMP_LABEL_RE.match(line)
            if m and not m.group(2).isdigit() and m.group(2) in labels:
                line = f"{m.group(1)}{labels[m.group(2)]}{m.group(3) or ''}"
                rewritten += 1
        out.append(line)
    return "\n".join(out) + "\n", len(labels), rewritten


def _group_comments(text: str) -> str:
    """Move every `#` line to just before the signature (delivery layout)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    code, comments = [], []
    for line in lines:
        if line.strip().startswith("#"):
            comments.append(line.rstrip())
        elif line.strip() or code:
            code.append(line.rstrip())
    while code and not code[-1].strip():
        code.pop()
    if not comments:
        return "\n".join(code) + "\n"
    return "\n".join(code + [""] + comments) + "\n"


def post(path: Path, author: str, output: Path | None = None) -> str:
    """Delivery post-process: labels -> line numbers, then sign."""
    text = _read(path)
    if not verify_text(text):
        m = re.search(r'"aigenerated:([^":]+):[0-9a-f]{12}"', text)
        if m:
            author = m.group(1)
    numeric, nlabels, rewritten = resolve_labels(text)
    signed, pid = sign_text(numeric, author, force=True)
    dst = output or path
    _write(dst, signed)
    return (f"post  {dst}: {nlabels} label(s), {rewritten} jump(s) numeric; "
            f"signed id={pid} author={author}")


def extract_text(text: str) -> list[dict]:
    hits = _find(text)
    if not hits:
        raise SignError("missing hidden @aigenerated argument")
    return decode_comments(hits[0].group(3))


def restore_text(text: str) -> tuple[str, str, list[dict]]:
    """Put the comments back before their anchor statements, same token."""
    hits = _find(text)
    if not hits:
        raise SignError("missing hidden @aigenerated argument")
    author = hits[0].group(1)
    cs = decode_comments(hits[0].group(3))
    body = _body(text)
    lines = body.rstrip("\n").split("\n")
    total = len(lines)
    buckets: dict[int, list[dict]] = {}
    for c in cs:
        buckets.setdefault(min(max(c["stmt"], 0), total), []).append(c)
    out: list[str] = []
    for i, line in enumerate(lines):
        out.extend(c["text"] for c in buckets.pop(i, []))
        if line.strip() == "end" and i == total - 1:
            out.extend(c["text"] for k in sorted(buckets) for c in buckets[k])
            buckets.clear()
        out.append(line)
    out.extend(c["text"] for k in sorted(buckets) for c in buckets[k])
    # keep the comments in the delivered text; only the hash ignores them
    enc = encode_comments(cs)
    payload = f":{enc}" if enc else ""
    sig = SIG_LINE.format(author=author, pid=hits[0].group(2), payload=payload)
    while out and not out[-1].strip():
        out.pop()
    out[-1] = sig
    return "\n".join(out) + "\n", hits[0].group(2), cs


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="hidden @aigenerated signature for AI-written mlog")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("sign", help="add/replace the hidden argument")
    ps.add_argument("files", nargs="+")
    ps.add_argument("--author", default=DEFAULT_AUTHOR)
    ps.add_argument("--force", action="store_true", help="re-sign after an intentional edit")

    pv = sub.add_parser("verify", help="check the hidden argument and body hash")
    pv.add_argument("files", nargs="+")

    pt = sub.add_parser("strip", help="refused by policy")
    pt.add_argument("files", nargs="+")

    pp = sub.add_parser("post", help="delivery post-process: labels -> numbers, then sign")
    pp.add_argument("files", nargs="+")
    pp.add_argument("--author", default=DEFAULT_AUTHOR)
    pp.add_argument("-o", "--output", help="write here instead of in place (single file)")

    pe = sub.add_parser("extract", help="list the comments hidden in the signature")
    pe.add_argument("files", nargs="+")

    pr = sub.add_parser("restore", help="put the hidden comments back into the file")
    pr.add_argument("files", nargs="+")

    args = ap.parse_args(argv)
    if args.cmd == "strip":
        print("refused: the @aigenerated signature is an attribution term and cannot be stripped;\n"
              "edit the logic and re-sign with `sign --force` instead", file=sys.stderr)
        return 2

    failed = 0
    for name in args.files:
        path = Path(name)
        if not path.is_file():
            print(f"missing file: {path}", file=sys.stderr)
            failed += 1
            continue
        if args.cmd == "extract":
            try:
                for c in extract_text(_read(path)):
                    print(f"L{c['line']} (stmt {c['stmt']}): {c['text']}")
            except SignError as e:
                print(f"FAIL  {path}: {e}", file=sys.stderr)
                failed += 1
        elif args.cmd == "restore":
            try:
                signed, pid, cs = restore_text(_read(path))
                _write(path, signed)
                print(f"restored  {path}: {len(cs)} comment(s), id={pid}")
            except SignError as e:
                print(f"FAIL  {path}: {e}", file=sys.stderr)
                failed += 1
        elif args.cmd == "post":
            if args.output and len(args.files) != 1:
                print("-o/--output only works with a single file", file=sys.stderr)
                return 2
            try:
                print(post(path, args.author, Path(args.output) if args.output else None))
            except SignError as e:
                print(f"FAIL  {e}", file=sys.stderr)
                failed += 1
        elif args.cmd == "sign":
            try:
                print(sign(path, args.author, args.force))
            except SignError as e:
                print(f"FAIL  {e}", file=sys.stderr)
                failed += 1
        else:
            problems = verify(path)
            if problems:
                failed += 1
                for p in problems:
                    print("FAIL  " + p, file=sys.stderr)
            else:
                m = _find(_read(path))[0]
                print(f"OK    {path}  id={m.group(2)}  author={m.group(1)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
