#!/usr/bin/env python3
"""Build and query a local knowledge base over this repository's docs and tools.

Retrieval is hybrid: dense embeddings (bge-small-zh-v1.5, ONNX) + SQLite FTS5
trigram BM25, fused with reciprocal-rank fusion, and optionally reranked with a
cross-encoder (bge-reranker-base; off by default — it is weak on short
Chinese keyword queries). Everything runs locally through
``fastembed``/onnxruntime; no network is needed after the models are cached.

    python tools/kb.py build                 # index docs + tool sources
    python tools/kb.py search "jump 标签" -k 6
    python tools/kb.py search "read @this" --kind doc
    python tools/kb.py serve                 # MCP server on stdio
    python tools/kb.py stats

Models and libraries are looked up in ``out/_kblibs`` and ``out/_kbmodels``;
override with ``KB_LIBS`` / ``KB_MODELS``. On first build set
``HF_ENDPOINT=https://hf-mirror.com`` if huggingface.co is unreachable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

for _s in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
KB_DIR = Path(os.environ.get("KB_DIR", ROOT / "out" / "_kb"))
DB_PATH = KB_DIR / "kb.sqlite3"
_LIBS_DEFAULT = ROOT / "out" / ("_kblibs311" if sys.version_info < (3, 12) else "_kblibs")
LIBS = Path(os.environ.get("KB_LIBS", _LIBS_DEFAULT))
MODELS = Path(os.environ.get("KB_MODELS", ROOT / "out" / "_kbmodels"))
EMB_MODEL = os.environ.get("KB_EMB_MODEL", "BAAI/bge-small-zh-v1.5")
RERANK_MODEL = os.environ.get("KB_RERANK_MODEL", "BAAI/bge-reranker-base")

DOC_FILES = [
    "README.md",
    "Logic/逻辑代码规范.md",
    "mindustry-blueprint/SKILL.md",
    "mindustry-blueprint/REFERENCE.md",
    "mindustry-blueprint/EMULATOR-CROSSCHECK.md",
    "tools/README.md",
    "dsh-preset-mlog/README.md",
    "rules/README.md",
    "docs/本地知识库.md",
    # Truth-source docs: what was actually run and what got refuted. Without
    # these, "is L2-07 still ✅?" is unanswerable from the KB (found 2026-10-24
    # when a rebuilt snapshot could not find 「已推翻」 anywhere).
    "docs/断言表.md",
    "docs/验证记录.md",
]
CODE_GLOBS = ["tools/*.py", "tests/run_tests.py"]
DOC_GLOBS = ["docs/游戏速查/*.md"]
EXCLUDE_DIRS = {"__pycache__", "out", ".git", "assetsraw", "Mindustry-Source", "Arc-Source"}
_EMB = None
_RR = None


def _ensure_libs() -> None:
    if LIBS.is_dir() and str(LIBS) not in sys.path:
        sys.path.insert(0, str(LIBS))


def _embedder():
    global _EMB
    if _EMB is None:
        _ensure_libs()
        from fastembed import TextEmbedding
        _EMB = TextEmbedding(model_name=EMB_MODEL, cache_dir=str(MODELS))
    return _EMB


def _reranker():
    global _RR
    if _RR is None:
        _ensure_libs()
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        _RR = TextCrossEncoder(model_name=RERANK_MODEL, cache_dir=str(MODELS))
    return _RR


# ---------------------------------------------------------------- chunking
def _section_chunks(text: str, path: str, kind: str) -> list[dict]:
    """Split markdown by headings, code by top-level def/class blocks."""
    out: list[dict] = []
    if kind == "code":
        lines = text.splitlines()
        starts = [i for i, l in enumerate(lines, 1)
                  if re.match(r"^(?:def |class |@)", l)]
        if not starts or starts[0] != 1:
            starts = [1] + starts
        bounds = list(zip(starts, starts[1:] + [len(lines) + 1]))
        for s, e in bounds:
            body = "\n".join(lines[s - 1:e - 1])
            for piece, ps, pe in _split_long(body, s, 120):
                out.append({"path": path, "start": ps, "end": pe, "kind": kind,
                            "heading": "", "text": piece})
        return out

    lines = text.splitlines()
    heading = ""
    cur: list[str] = []
    cur_start = 1

    def flush(end_line: int):
        nonlocal cur, cur_start
        body = "\n".join(cur).strip()
        if body:
            lines = body.splitlines()
            # 标题行本身已经在 body 里了，别再加一次前缀（之前每个块标题都出现两遍）
            if lines and re.match(r"^#{1,4} ", lines[0]):
                body = "\n".join(lines[1:]).strip()
            prefix = f"# {heading}\n" if heading else ""
            if body:
                for piece, ps, pe in _split_long(prefix + body, cur_start, 1400):
                    out.append({"path": path, "start": ps, "end": pe, "kind": kind,
                                "heading": heading, "text": piece})
        cur = []

    for i, line in enumerate(lines, 1):
        if re.match(r"^#{1,4} ", line):
            flush(i)
            heading = line.lstrip("# ").strip()
            cur_start = i
        cur.append(line)
    flush(len(lines) + 1)
    return out


def _split_long(body: str, start: int, limit: int) -> list[tuple[str, int, int]]:
    """Split oversized text on paragraph boundaries; returns (text, start, end)."""
    if len(body) <= limit:
        return [(body, start, start + body.count("\n"))]
    chunks, cur, cur_start = [], [], start
    for line in body.splitlines():
        cur.append(line)
        if sum(len(x) + 1 for x in cur) >= limit:
            chunks.append(("\n".join(cur), cur_start, cur_start + len(cur) - 1))
            cur_start += len(cur)
            cur = []
    if cur:
        chunks.append(("\n".join(cur), cur_start, cur_start + len(cur) - 1))
    return chunks


def _corpus() -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for rel in DOC_FILES:
        p = ROOT / rel
        if p.is_file():
            files.append((p, "doc"))
    for pattern in CODE_GLOBS:
        for p in sorted(ROOT.glob(pattern)):
            if p.is_file() and not any(part in EXCLUDE_DIRS for part in p.parts):
                files.append((p, "code"))
    for pattern in DOC_GLOBS:
        for p in sorted(ROOT.glob(pattern)):
            if p.is_file() and not any(part in EXCLUDE_DIRS for part in p.parts):
                files.append((p, "doc"))
    return files


# ---------------------------------------------------------------- index
def build(quiet: bool = False) -> dict:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """CREATE TABLE chunks(
               id INTEGER PRIMARY KEY, path TEXT, start INTEGER, end INTEGER,
               kind TEXT, heading TEXT, text TEXT, vec BLOB);
           CREATE VIRTUAL TABLE fts USING fts5(
               text, heading, path, content='chunks', content_rowid='id',
               tokenize='trigram');"""
    )
    rows: list[tuple] = []
    seen: set[str] = set()
    for path, kind in _corpus():
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for ch in _section_chunks(text, rel, kind):
            norm = re.sub(r"\s+", "", ch["text"])
            if len(norm) < 24 or norm in seen:
                continue  # 同一段文字在 SKILL/README 等文档里重复出现时只留第一份
            seen.add(norm)
            rows.append((ch["path"], ch["start"], ch["end"], ch["kind"],
                         ch["heading"], ch["text"]))
    if not rows:
        raise SystemExit("no corpus files found")

    model = _embedder()
    vecs = []
    B = 32
    for i in range(0, len(rows), B):
        batch = [r[5] for r in rows[i:i + B]]
        vecs.extend(model.embed(batch))
    import numpy as np
    conn.executemany(
        "INSERT INTO chunks(path,start,end,kind,heading,text,vec) VALUES (?,?,?,?,?,?,?)",
        [(r[0], r[1], r[2], r[3], r[4], r[5],
          np.asarray(v, dtype="float32").tobytes()) for r, v in zip(rows, vecs)])
    conn.execute("INSERT INTO fts(rowid,text,heading,path) SELECT id,text,heading,path FROM chunks")
    conn.commit()
    n = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    conn.close()
    stats = {"chunks": n, "files": len({r[0] for r in rows})}
    if not quiet:
        print(json.dumps(stats, ensure_ascii=False))
    return stats


def _open() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise SystemExit("no index yet: python tools/kb.py build")
    return sqlite3.connect(DB_PATH)


def _fts_query(q: str) -> str:
    q = q.strip()
    if len(q) < 3:
        return ""
    return '"' + q.replace('"', '""') + '"'


def search(query: str, k: int = 6, kind: str | None = None,
           rerank: bool = False, candidates: int = 30) -> list[dict]:
    import numpy as np
    conn = _open()
    where = " WHERE kind = ?" if kind else ""
    args = (kind,) if kind else ()
    rows = conn.execute(f"SELECT id,path,start,end,kind,heading,text,vec FROM chunks{where}",
                        args).fetchall()
    if not rows:
        return []
    qv = np.asarray(next(iter(_embedder().embed([query]))), dtype="float32")
    mat = np.frombuffer(b"".join(r[7] for r in rows), dtype="float32").reshape(len(rows), -1)
    sims = mat @ qv / (np.linalg.norm(mat, axis=1) * np.linalg.norm(qv) + 1e-9)
    dense = list(np.argsort(-sims)[:candidates])

    sparse: list[int] = []
    fq = _fts_query(query)
    if fq:
        try:
            sparse = [r[0] for r in conn.execute(
                "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?", (fq, candidates))]
        except sqlite3.OperationalError:
            sparse = []
    by_id = {r[0]: r for r in rows}
    dense_ids = [rows[i][0] for i in dense]
    fused: dict[int, float] = {}
    for rank, cid in enumerate(dense_ids):
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + rank)
    for rank, cid in enumerate(sparse):
        if cid in by_id:
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + rank)
    ordered = sorted(fused, key=lambda x: -fused[x])[:candidates]
    picks = [by_id[i] for i in ordered]

    scores: dict[int, float] = {}
    if rerank and picks:
        try:
            rr = _reranker()
            vals = list(rr.rerank(query, [p[6] for p in picks]))
            for p, v in zip(picks, vals):
                scores[p[0]] = float(v)
            picks.sort(key=lambda p: -scores[p[0]])
        except Exception as exc:  # rerank is optional
            print(f"rerank skipped: {exc}", file=sys.stderr)
    out = []
    for p in picks[:k]:
        cid, path, start, end, kind_, heading, text = p[:7]
        out.append({"path": path, "start": start, "end": end, "kind": kind_,
                    "heading": heading, "text": text,
                    "score": round(scores.get(cid, fused.get(cid, 0.0)), 4)})
    conn.close()
    return out


def _format(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        snippet = " ".join(r["text"].split())
        if len(snippet) > 420:
            snippet = snippet[:420] + "…"
        head = f" — {r['heading']}" if r["heading"] else ""
        lines.append(f"[{i}] {r['path']}:{r['start']}-{r['end']}{head}  score={r['score']}")
        lines.append(f"    {snippet}")
    return "\n".join(lines) if lines else "(no results)"


# ---------------------------------------------------------------- MCP
def serve() -> None:
    """Minimal MCP stdio server exposing the kb_search tool."""
    _embedder()  # warm up before answering
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            method = msg.get("method")
            mid = msg.get("id")
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05",
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "mlog-kb", "version": "0.1.0"}}
            elif method == "tools/list":
                result = {"tools": [{
                    "name": "kb_search",
                    "description": "Search this repo's mlog docs and tool sources (hybrid dense+BM25; optional cross-encoder rerank).",
                    "inputSchema": {"type": "object", "properties": {
                        "query": {"type": "string"},
                        "k": {"type": "integer", "default": 6},
                        "kind": {"type": "string", "enum": ["doc", "code"]}},
                        "required": ["query"]}}]}
            elif method == "tools/call":
                params = msg.get("params", {})
                if params.get("name") != "kb_search":
                    raise ValueError(f"unknown tool: {params.get('name')}")
                args = params.get("arguments", {})
                res = search(str(args.get("query", "")), int(args.get("k", 6)),
                             args.get("kind"), rerank=bool(args.get("rerank", False)))
                result = {"content": [{"type": "text", "text": _format(res)}],
                          "isError": False}
            elif method in ("notifications/initialized", "notifications/cancelled"):
                continue
            else:
                result = {}
            if mid is not None:
                print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result},
                                 ensure_ascii=False), flush=True)
        except Exception as exc:
            msg = str(exc)
            if os.environ.get("KB_DEBUG"):
                import traceback
                msg = traceback.format_exc()
            print(json.dumps({"jsonrpc": "2.0", "id": locals().get("mid"),
                              "error": {"code": -32000, "message": msg}},
                             ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    sp = sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("-k", type=int, default=6)
    sp.add_argument("--kind", choices=["doc", "code"])
    sp.add_argument("--rerank", action="store_true",
                    help="cross-encoder rerank (off by default: bge-reranker-base is weak on short Chinese queries)")
    sub.add_parser("serve")
    sub.add_parser("stats")
    args = ap.parse_args(argv)

    if args.cmd == "build":
        build()
        return 0
    if args.cmd == "serve":
        serve()
        return 0
    if args.cmd == "stats":
        if not DB_PATH.exists():
            print("no index yet: run `python tools/kb.py build` first")
            return 1
        with sqlite3.connect(DB_PATH) as cx:
            n = cx.execute("select count(*) from chunks").fetchone()[0]
            files = cx.execute("select count(distinct path) from chunks").fetchone()[0]
        print(json.dumps({"chunks": n, "files": files}, ensure_ascii=False))
        return 0
    res = search(args.query, args.k, args.kind, rerank=args.rerank)
    print(_format(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
