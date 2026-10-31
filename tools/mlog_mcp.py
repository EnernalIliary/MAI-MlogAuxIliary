#!/usr/bin/env python
"""mlog_mcp.py -- MCP (stdio) server for the mlog toolkit.

Exposes the analysis side of `mlog.py` and the real-executor simulator
(`mlogsim.py`) as MCP tools, so a model does not have to discover the repo
layout or pipe PowerShell output.  Everything is JSON in / JSON out.

Run:  python tools/mlog_mcp.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import mlogcheck as _check  # noqa: E402
import mlogir as _ir  # noqa: E402
import mlogval as _val  # noqa: E402
import msch  # noqa: E402
import mlogsim as _sim  # noqa: E402

try:  # every byte this process writes is UTF-8
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

PROTO = "2024-11-05"
NAME = "mlog-toolkit"
VERSION = "0.1.0"

TOOLS = [
    {
        "name": "mlog_doctor",
        "description": "Environment check: repo root, python, UTF-8, server jar, JRE21, "
                       "and the five canonical commands. Run this first in a new workspace.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "mlog_check",
        "description": "Static analysis of mlog (.mlog or processors inside .msch). "
                       "Returns findings: severity, line (1-based), code, message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": ".mlog or .msch path"},
                "cost": {"type": "boolean", "description": "also include the cost/loop report"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "mlog_ir",
        "description": "Control-flow overview: statement count, unreachable statement "
                       "indices, dead stores (write never read).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "mlog_values",
        "description": "Abstract-value report: constant branches, provable nulls, "
                       "with per-line evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "mlog_gen",
        "description": "Compile mlog source into a blueprint paste code (base64 .msch). "
                       "Optionally writes the binary and signs the source.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": ".mlog path (default: logic)"},
                "logic": {"type": "string", "description": "inline mlog code"},
                "name": {"type": "string"},
                "links": {"type": "array", "items": {"type": "string"},
                          "description": "name@dx,dy, relative to the processor"},
                "blocks": {"type": "array", "items": {"type": "string"},
                           "description": "block@dx,dy extra blocks"},
                "out": {"type": "string", "description": "optional .msch output path"},
            },
        },
    },
    {
        "name": "mlog_sim",
        "description": "Run code on the real v160.7 executor (warm server, ~0.6s after "
                       "first call). Returns variables, @counter, text buffer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": ".mlog or .msch"},
                "logic": {"type": "string", "description": "inline mlog (instead of path)"},
                "ticks": {"type": "integer", "description": "run N ticks (time-accurate)"},
                "steps": {"type": "integer", "description": "run N instructions"},
                "units": {"type": "string", "description": "e.g. 'mono:1,flare:2'"},
                "unit_pos": {"type": "string", "description": "x,y tile position"},
                "core_items": {"type": "string", "description": "e.g. 'silicon:400'"},
                "save": {"type": "string", "description": ".msav scene to load"},
                "update": {"type": "string", "enum": ["all", "none"]},
                "links": {"type": "string", "enum": ["auto", "off"]},
            },
            "required": [],
        },
    },
]

_SRV = None
_SRV_KEY = None


def _server(save: str | None):
    global _SRV, _SRV_KEY
    key = save or ""
    if _SRV is not None and _SRV_KEY == key:
        _SRV.reset_world()
        return _SRV
    if _SRV is not None:
        try:
            _SRV.close()
        except Exception:
            pass
    _SRV = _sim.Server(_sim._default_java(), _sim._default_jar(),
                       save=save or None)
    _SRV_KEY = key
    return _SRV


def _programs(path: str):
    p = Path(path)
    if p.suffix.lower() == ".msch":
        sch = msch.parse_file(str(p))
        for t in sch.logic_tiles():
            assert t.logic is not None
            yield (f"{path} [{t.block} @({t.x},{t.y})]",
                   t.logic.code.split("\n"),
                   [l.name for l in t.logic.links])
    else:
        yield path, p.read_text(encoding="utf-8-sig").split("\n"), None


def tool_doctor(_args):
    root = HERE.parent
    jar = root / "out" / "_server160.jar"
    jre = root / "out" / "jre21" / "jdk-21.0.5+11-jre" / "bin" / "java.exe"
    return {
        "repo": str(root),
        "entry": str(HERE / "mlog.py"),
        "python": "%d.%d.%d" % sys.version_info[:3],
        "stdout_utf8": True,
        "server_jar": jar.exists(),
        "jre21": jre.exists(),
        "sim_ready": jar.exists() and jre.exists(),
        "commands": [
            "python tools/mlog.py check FILE --cost",
            "python tools/mlog.py ir FILE",
            "python tools/mlog.py values FILE",
            "python tools/mlog.py gen --source FILE --name demo",
            "python tools/mlogsim.py FILE --ticks 120 --update all",
        ],
    }


def tool_check(args):
    out = []
    for label, lines, names in _programs(args["path"]):
        fs = _check.analyse(lines, names)
        out.append({
            "program": label,
            "findings": [{"sev": f.sev, "line": f.line, "code": f.code, "msg": f.msg}
                         for f in fs[:60]],
            "counts": {s: sum(1 for f in fs if f.sev == s)
                       for s in ("ERR", "SUSPECT", "WARN", "INFO")},
        })
        if args.get("cost"):
            out[-1]["cost"] = _check.cost_report(_check.prepare(lines), names)[:4000]
    return out


def tool_ir(args):
    out = []
    for label, lines, names in _programs(args["path"]):
        prog = _ir.parse(lines)
        out.append({
            "program": label,
            "statements": len(prog.stmts),
            "unreachable": prog.dead()[:80],
            "dead_stores": [{"line": i + 1, "name": n}
                            for i, n in prog.dead_stores()[:60]],
        })
    return out


def tool_values(args):
    out = []
    for label, lines, names in _programs(args["path"]):
        prog = _ir.parse(lines)
        st = _val.analyze(prog)
        out.append({"program": label,
                    "report": _val.format_report(label, prog, st)[:5000]})
    return out


def tool_gen(args):
    root = HERE.parent
    cmd = [sys.executable, str(HERE / "mlog.py"), "gen"]
    if args.get("source"):
        cmd += ["--source", args["source"]]
    if args.get("logic"):
        cmd += ["--logic", args["logic"]]
    if args.get("name"):
        cmd += ["--name", args["name"]]
    for link in args.get("links") or []:
        cmd += ["--link", link]
    for b in args.get("blocks") or []:
        cmd += ["--with-block", b]
    if args.get("out"):
        cmd += ["--binary", args["out"]]
    p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:2000])
    code = (p.stdout or "").strip().splitlines()[-1].strip()
    return {"blueprint_base64": code,
            "binary": args.get("out"),
            "stderr": (p.stderr or "").strip()[:500]}


def _sim_subprocess(args):
    """Advanced options (units/save/core) go through the CLI so behaviour stays
    identical to the documented command; the warm path below covers the common case."""
    root = HERE.parent
    cmd = [sys.executable, str(HERE / "mlogsim.py")]
    if args.get("path"):
        cmd.append(args["path"])
    else:
        cmd += ["--logic", args["logic"]]
    if args.get("ticks"):
        cmd += ["--ticks", str(int(args["ticks"]))]
    else:
        cmd += ["--steps", str(int(args.get("steps") or 60))]
    if args.get("units"):
        cmd += ["--units", args["units"]]
    if args.get("unit_pos"):
        cmd += ["--unit-pos", args["unit_pos"]]
    if args.get("core_items"):
        cmd += ["--core-items", args["core_items"]]
    if args.get("save"):
        cmd += ["--save", args["save"]]
    if args.get("update"):
        cmd += ["--update", args["update"]]
    if args.get("links"):
        cmd += ["--links", args["links"]]
    cmd.append("--json")
    p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:2000])
    data = json.loads((p.stdout or "").strip().splitlines()[-1])
    rec = (data.get("results") or [data])[0]
    return rec


def tool_sim(args):
    path = args.get("path")
    logic = args.get("logic")
    if not path and not logic:
        raise ValueError("path or logic required")
    if any(args.get(k) for k in ("units", "unit_pos", "core_items", "save")):
        rec = _sim_subprocess(args)
    else:
        srv = _server(None)
        ticks = bool(args.get("ticks"))
        steps = int(args.get("ticks") or args.get("steps") or 60)
        update = args.get("update") or ("all" if ticks else "none")
        links = args.get("links") or "auto"
        if logic:
            rec = _sim.run_mlog(srv, "inline", logic, steps, ticks=ticks,
                                update=update, links=links)
        elif path.lower().endswith(".msch"):
            rec = _sim.run_msch(srv, path, path, steps, ticks=ticks,
                                update=update)[0]
        else:
            code = Path(path).read_text(encoding="utf-8-sig")
            rec = _sim.run_mlog(srv, path, code, steps, ticks=ticks,
                                update=update, links=links)
    return {
        "vars": rec.get("vars", {}),
        "counter": rec.get("counter"),
        "text": (rec.get("text") or "")[:400],
        "mode": rec.get("mode"),
        "processor": rec.get("processor"),
        "links": rec.get("links", []),
    }


HANDLERS = {
    "mlog_doctor": tool_doctor,
    "mlog_check": tool_check,
    "mlog_ir": tool_ir,
    "mlog_values": tool_values,
    "mlog_gen": tool_gen,
    "mlog_sim": tool_sim,
}


def _reply(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return _reply(rid, {
            "protocolVersion": PROTO,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": NAME, "version": VERSION},
        })
    if method == "notifications/initialized":
        return
    if method == "ping":
        return _reply(rid, {})
    if method == "tools/list":
        return _reply(rid, {"tools": TOOLS})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        fn = HANDLERS.get(name)
        if fn is None:
            return _reply(rid, error={"code": -32602, "message": f"unknown tool {name}"})
        try:
            out = fn(params.get("arguments") or {})
            text = json.dumps(out, ensure_ascii=False)
            return _reply(rid, {"content": [{"type": "text", "text": text}],
                                "isError": False})
        except Exception as exc:  # report, do not crash the server
            return _reply(rid, {"content": [{"type": "text",
                                             "text": "%s: %s" % (type(exc).__name__, exc)}],
                                "isError": True})
    if rid is None:
        return
    return _reply(rid, error={"code": -32601, "message": f"method not found: {method}"})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            handle(req)
        except Exception:
            traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
