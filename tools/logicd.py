#!/usr/bin/env python3
"""logicd -- the local half of the in-game logic-writing service.

The mod (MlogAuxiliary/) never composes mlog itself.  It posts an *intent* here, this
process turns it into code, checks it with the same analyzer the gate uses, and
hands back the exact byte payload the game's own config channel expects
(LogicBlock.compress: version byte + code + link table, see
Mindustry-Source/core/src/mindustry/world/blocks/logic/LogicBlock.java:137-166).
The mod then calls build.configure(bytes) -- which is also the multiplayer path,
because that config is applied on the server (LogicBlock.java:65-69).

Why a sidecar and not "all in JS"
---------------------------------
The rules, the instruction table and the cost model already exist in Python and are
what the gate pins.  Re-implementing them in Rhino would create a second copy that
drifts silently.  So the contract is: game = transport + UI + a few instant checks,
sidecar = composition + full analysis.  When the sidecar is unreachable the mod
still works in "offline" mode (templates bundled in JS, instant checks only).

Protocol (v1, JSON over HTTP, loopback only)
--------------------------------------------
    GET  /health        -> {"ok":true,"protocol":1}
    GET  /capabilities  -> instructions, ops, templates, limits
    POST /analyze       {code, links:[name|{name,x,y}], ipt}      -> findings + cost
    POST /compose       {template, params, links, ipt}            -> code + findings + bytes_b64
    POST /region        {snapshot}                                -> per-processor link candidates
    POST /snapshot      {snapshot}                                -> writes out/snapshots/*.json, returns the path
    POST /plan          {intent, links, ipt}                       -> compose, plus which template matched and why
    POST /jobs          {desc, mode, snapshot, links, ipt, origin}  -> queued job (id is sha1 of the body)
    GET  /jobs?id=|?state=|?all=1&limit=N                           -> one job / the unfinished ones
                                                                     / the newest N of every state
    DELETE /jobs?id=                                                -> cancel (running is cancelled too)

Everything returns "ok": false when any finding is ERR: a write is refused, not
downgraded.  WARN/INFO are returned with the payload so the panel can show them
next to the confirm button.

Usage
-----
    python tools/logicd.py                    # 127.0.0.1:8765
    python tools/logicd.py --port 0           # ephemeral port, printed on the ready line
    python tools/logicd.py --print-template patrol   # dump one template, no server
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

#: v0.13.1: the shipped package runs this as a PyInstaller one-file exe
#: (`sidecar/logicd.exe`), so the two roots split.  Read-only data (the *.json
#: tables and rules/) is unpacked into ``sys._MEIPASS``, which is a temp dir that
#: is deleted on exit; job files and snapshots have to live next to the exe.
FROZEN = bool(getattr(sys, 'frozen', False))
if FROZEN:
    HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
    WROOT = os.path.dirname(os.path.abspath(sys.executable))
    # jobqueue computes its default from its own __file__ (also inside the temp
    # dir when frozen), so pin the writable side through the env var it reads.
    os.environ.setdefault('MLOG_JOBS_DIR', os.path.join(WROOT, 'out', 'jobs'))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    WROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mlogcheck as C          # noqa: E402
import msch                    # noqa: E402
import jobqueue                # noqa: E402  (imports us back lazily, one-way edge)
import mlogprovider           # noqa: E402  (prompt / reply parsing / model call; offline at import)
import aigenerated             # noqa: E402  (the hidden @aigenerated signature)

PROTOCOL = 1
# The sidecar is what gets restarted when a fix ships, and a stale process answers
# /health with 200 all day, so let the answer say which build it is (v0.12.3).
SIDECAR_VERSION = '0.14.6'
GAME = 'mindustry v160.7 (executor jar out/_server160.jar)'

#: v0.14.2: every program this sidecar hands out is signed before it is analyzed
#: and compiled, so the bytes the mod writes carry the attribution and the job's
#: `code` is byte-identical to the processor's `b.code` (main.js armWriteCheck).
SIGN_AUTHOR = aigenerated.SIGN_AUTHOR

#: LogicBlock.instructionsPerTick is 1 by default and clamped to
#: maxInstructionsPerTick = 40 (LogicBlock.java:46,48,826) -- the panel shows the
#: budget against this cap.
IPT_MAX = 40
#: a non-privileged processor can only link within 10 tiles (LogicBlock.range)
LINK_RANGE_TILES = 10


# ---------------------------------------------------------------- templates

class Template:
    """One deterministic pattern.  Params are typed so a bad request is a 400."""

    def __init__(self, tid, title, params, build):
        self.id = tid
        self.title = title
        self.params = params      # name -> (type, default, help)
        self.build = build        # (params) -> (lines, referenced link names)

    def spec(self):
        return {'id': self.id, 'title': self.title,
                'params': [{'name': k, 'type': v[0], 'default': v[1], 'help': v[2]}
                           for k, v in self.params.items()]}


def _p(params, name, kind, default):
    v = params.get(name, default)
    if kind == 'int':
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValueError('param %r must be an integer, got %r' % (name, v))
    if kind == 'float':
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ValueError('param %r must be a number, got %r' % (name, v))
    if kind == 'link':
        s = str(v or '').strip()
        if not s or not s.replace('_', 'a').replace('-', 'a').isalnum():
            raise ValueError('param %r must be a link name, got %r' % (name, v))
        return s
    if kind == 'unit':
        s = str(v or '').strip()
        if not s:
            raise ValueError('param %r must be a unit type' % name)
        return s if s.startswith('@') else '@' + s
    if kind == 'item':
        s = str(v or '').strip()
        if not s:
            raise ValueError('param %r must be an item name' % name)
        return s if s.startswith('@') else '@' + s
    s = str(v if v is not None else '')
    if len(s) > 120:
        raise ValueError('param %r is too long' % name)
    return s.replace('\n', ' ').replace('\r', ' ')


def _t_patrol(params):
    unit = _p(params, 'unit', 'unit', 'flare')
    x1, y1 = _p(params, 'x1', 'int', 10), _p(params, 'y1', 'int', 10)
    x2, y2 = _p(params, 'x2', 'int', 20), _p(params, 'y2', 'int', 10)
    hold = _p(params, 'wait', 'float', 1)
    lines = ['ubind ' + unit,
             'loop:',
             'ucontrol move %d %d 0 0 0' % (x1, y1),
             'wait %s' % hold,
             'ucontrol move %d %d 0 0 0' % (x2, y2),
             'wait %s' % hold,
             'jump loop always']
    return lines, []


def _t_item_threshold(params):
    cell = _p(params, 'container', 'link', 'container1')
    item = _p(params, 'item', 'item', 'copper')
    thr = _p(params, 'threshold', 'int', 100)
    msg = _p(params, 'message', 'link', 'message1')
    lines = ['loop:',
             'sensor n %s %s' % (cell, item),
             'jump enough greaterThanEq n %d' % thr,
             'print "low"',
             'printflush %s' % msg,
             'enough:',
             'wait 1',
             'jump loop always']
    return lines, [cell, msg]


def _t_radar_fire(params):
    turret = _p(params, 'turret', 'link', 'turret1')
    lines = ['loop:',
             'radar enemy any any distance %s 1 target' % turret,
             'jump fire notEqual target null',
             'wait 0.5',
             'jump loop always',
             'fire:',
             'control shootp %s target 1' % turret,
             'jump loop always']
    return lines, [turret]


def _t_memory_table(params):
    cell = _p(params, 'cell', 'link', 'cell1')
    idx = _p(params, 'index', 'int', 0)
    val = _p(params, 'value', 'int', 42)
    lines = ['write %d %s %d' % (val, cell, idx),
             'read out %s %d' % (cell, idx),
             'print out',
             'printflush message1',
             'end']
    return lines, [cell]


def _t_timer_flush(params):
    msg = _p(params, 'message', 'link', 'message1')
    secs = _p(params, 'seconds', 'float', 2)
    text = _p(params, 'text', 'str', 'tick')
    lines = ['loop:',
             'wait %s' % secs,
             'print "%s"' % text,
             'printflush %s' % msg,
             'jump loop always']
    return lines, [msg]


def _t_counter(params):
    cell = _p(params, 'cell', 'link', 'cell1')
    step = _p(params, 'step', 'int', 1)
    lines = ['loop:',
             'read n %s 0' % cell,
             'op add n n %d' % step,
             'write n %s 0' % cell,
             'wait 1',
             'jump loop always']
    return lines, [cell]


TEMPLATES = {t.id: t for t in [
    Template('patrol', 'bind a unit and move it between two tiles', {
        'unit': ('unit', 'flare', 'unit type, @ optional'),
        'x1': ('int', 10, 'first tile x'), 'y1': ('int', 10, 'first tile y'),
        'x2': ('int', 20, 'second tile x'), 'y2': ('int', 10, 'second tile y'),
        'wait': ('float', 1, 'seconds to hold at each end'),
    }, _t_patrol),
    Template('item-threshold', 'print when a container holds enough of an item', {
        'container': ('link', 'container1', 'container link name'),
        'item': ('item', 'copper', 'item name, @ optional'),
        'threshold': ('int', 100, 'count that counts as enough'),
        'message': ('link', 'message1', 'message link name'),
    }, _t_item_threshold),
    Template('radar-fire', 'point a turret at the nearest enemy unit', {
        'turret': ('link', 'turret1', 'turret link name'),
    }, _t_radar_fire),
    Template('memory-table', 'write and read one memory cell slot', {
        'cell': ('link', 'cell1', 'memory cell link name'),
        'index': ('int', 0, 'cell slot'), 'value': ('int', 42, 'value to store'),
    }, _t_memory_table),
    Template('timer-flush', 'print a line every N seconds', {
        'message': ('link', 'message1', 'message link name'),
        'seconds': ('float', 2, 'period in seconds'), 'text': ('str', 'tick', 'line to print'),
    }, _t_timer_flush),
    Template('counter', 'count up in a memory cell forever', {
        'cell': ('link', 'cell1', 'memory cell link name'),
        'step': ('int', 1, 'increment per second'),
    }, _t_counter),
]}


# ---------------------------------------------------------------- intent routing
#
# This is the ONE place a language model would plug in.  Everything downstream is
# the deterministic path (template -> code -> analyzer -> byte payload), so a model
# can only ever choose a template and fill its parameters -- it never hands raw mlog
# to the game.  The default backend is keyword rules, which runs offline; swapping in
# a model means replacing _route() and keeping its return shape.

_NUM = re.compile(r'-?[0-9]+(?:[.][0-9]+)?')
_ATNAME = re.compile(r'@[A-Za-z][A-Za-z0-9_-]*')

#: keyword sets, most specific first (ordered because ties break by position)
_INTENTS = [
    ('radar-fire', ('雷达', '开火', '炮塔', '攻击', '打怪', '射击', '瞄准',
                    'radar', 'turret', 'fire', 'shoot', 'aim')),
    ('item-threshold', ('物品', '资源', '容器', '阈值', '库存', '够', '满了', '铜', '铅',
                        'container', 'vault', 'threshold', 'item', 'stock', 'enough')),
    ('timer-flush', ('定时', '每隔', '周期', '报时', '刷屏', 'timer', 'every', 'interval')),
    ('counter', ('计数', '自增', '累加', 'counter', 'count up', 'count', 'increment')),
    ('memory-table', ('内存', '存储', '记忆', '存值', 'memory', 'store', 'save')),
    ('patrol', ('巡逻', '来回', '移动', '飞过去', 'patrol', 'move', 'walk', 'fly')),
]

_ITEMS = None


def _content_items():
    """Item names from tools/content_names.json, for "watch copper" style intents."""
    global _ITEMS
    if _ITEMS is None:
        try:
            data = json.load(open(os.path.join(HERE, 'content_names.json'), encoding='utf-8'))
            _ITEMS = [str(x) for x in (data.get('items') or [])]
        except Exception:
            _ITEMS = []
    return _ITEMS


def _numbers(text):
    out = []
    for m in _NUM.findall(text):
        try:
            f = float(m)
        except ValueError:
            continue
        out.append(int(f) if f == int(f) else f)
    return out


def _atoms(text):
    return _ATNAME.findall(text)


_ZH_LINK = {
    '容器': 'container', '仓库': 'vault', '核心': 'core', '内存': 'cell',
    '电池': 'battery', '消息': 'message', '炮塔': 'turret', '炮': 'turret',
    '显示器': 'display', '开关': 'switch',
}


_ITEM_ZH = {
    '铜': 'copper', '铅': 'lead', '煤': 'coal', '钛': 'titanium', '硅': 'silicon',
    '石墨': 'graphite', '钢化玻璃': 'metaglass', '爆炸物': 'blast-compound',
    '合金': 'surge-alloy', '相织物': 'phase-fabric', '碳化物': 'carbide',
    '裂变物质': 'fissile-matter', '铍': 'beryllium', '钨': 'tungsten',
    '废料': 'scrap', '钍': 'thorium', '沙': 'sand', '冰': 'ice', '孢子荚': 'spore-pod',
}


def _match_item(text):
    """First item mentioned in the sentence, English or Chinese.

    Order: @atoms, then English words with a word boundary (so 'sand' does not
    match 'thousand'), then the Chinese table.  None when nothing matches, so the
    caller keeps the template default.
    """
    items = _content_items()
    low = text.lower()
    for a in _atoms(text):
        name = a.lstrip('@')
        if name in items:
            return name
    for name in items:
        if re.search(r'\b' + re.escape(name.lower()) + r'\b', low):
            return name
    for zh, en in _ITEM_ZH.items():
        if zh in text and en in items:
            return en
    return None

def _pick_link(names, *subs):
    """First link whose name contains one of the substrings.

    Chinese substrings are translated to the English block names the game builds
    link names from (LogicBlock.getLinkName takes the block name), so a Chinese
    sentence can still pick the right link instead of silently falling back to
    whatever came first.
    """
    want = []
    for sub in subs:
        want.append(sub)
        for zh, en in _ZH_LINK.items():
            if zh in sub:
                want.append(en)
    for sub in want:
        for n in names:
            if sub in n.lower():
                return n
    return None


def _fallback_link(names, *avoid):
    for n in names:
        low = n.lower()
        if not any(a in low for a in avoid):
            return n
    return names[0] if names else None


def _params_patrol(text, names):
    atoms = _atoms(text)
    nums = _numbers(text)
    p = {}
    if atoms:
        p['unit'] = atoms[0].lstrip('@')
    if len(nums) >= 4:
        p['x1'], p['y1'], p['x2'], p['y2'] = nums[0], nums[1], nums[2], nums[3]
    elif len(nums) == 2:
        p['x2'], p['y2'] = nums[0], nums[1]
    return p


def _params_radar_fire(text, names):
    turret = _pick_link(names, 'turret', '炮', 'tur', 'duo', 'scatter', 'lance', 'arc')
    if turret is None:
        turret = _fallback_link(names, 'message', 'msg', 'cell', 'bank')
    return {'turret': turret} if turret else {}


def _params_item_threshold(text, names):
    p = {}
    container = _pick_link(names, 'container', 'vault', 'storage', '容器', 'core')
    if container is None:
        container = _fallback_link(names, 'message', 'msg', 'cell')
    if container:
        p['container'] = container
    message = _pick_link(names, 'message', 'msg')
    if message:
        p['message'] = message
    item = _match_item(text)
    if item:
        p['item'] = item
    nums = _numbers(text)
    if nums:
        p['threshold'] = nums[0]
    return p


def _params_memory(text, names):
    p = {}
    cell = _pick_link(names, 'cell', 'memory', 'bank', '内存')
    if cell is None:
        cell = _fallback_link(names, 'message', 'msg', 'container')
    if cell:
        p['cell'] = cell
    nums = _numbers(text)
    if len(nums) >= 2:
        p['index'], p['value'] = nums[0], nums[1]
    elif nums:
        p['value'] = nums[0]
    return p


def _params_timer(text, names):
    p = {}
    message = _pick_link(names, 'message', 'msg')
    if message:
        p['message'] = message
    nums = _numbers(text)
    if nums:
        p['seconds'] = nums[0]
    return p


def _params_counter(text, names):
    p = {}
    cell = _pick_link(names, 'cell', 'memory', 'bank', '内存')
    if cell is None:
        cell = _fallback_link(names, 'message', 'msg', 'container')
    if cell:
        p['cell'] = cell
    nums = _numbers(text)
    if nums:
        p['step'] = nums[0]
    return p


_PARAMS = {
    'patrol': _params_patrol,
    'radar-fire': _params_radar_fire,
    'item-threshold': _params_item_threshold,
    'memory-table': _params_memory,
    'timer-flush': _params_timer,
    'counter': _params_counter,
}


def _route(intent, names):
    """intent text -> (template id, one-line reason, params).  Rules only, offline."""
    low = intent.lower()
    best, best_score, best_words = None, 0, []
    for tid, words in _INTENTS:
        hit = [w for w in words if w.lower() in low]
        if len(hit) > best_score:
            best, best_score, best_words = tid, len(hit), hit
    if best is None:
        return None, '', {}
    params = _PARAMS[best](intent, names)
    why = 'matched %s -> template %r' % (', '.join(best_words), best)
    return best, why, params


def plan(req):
    """Natural-language intent -> the same payload /compose returns, plus the reasoning."""
    intent = str(req.get('intent') or '').strip()
    if not intent:
        raise ValueError('intent must be a non-empty string')
    if len(intent) > 2000:
        raise ValueError('intent is too long (2000 characters)')
    links = req.get('links') or []
    ipt = int(req.get('ipt') or 2)
    tid, why, params = _route(intent, _link_names(links))
    if tid is None:
        why = 'no template matched this intent -- say what the processor should do: patrol, watch an item, point a turret, count up, print on a timer, store a value'
        return {'ok': False, 'intent': intent, 'template': None, 'reason': why,
                'candidates': [{'id': t.id, 'title': t.title} for t in TEMPLATES.values()]}
    res = compose({'template': tid, 'params': params, 'links': links, 'ipt': ipt,
                   'origin': req.get('origin')})
    res['intent'] = intent
    res['params'] = params
    res['reason'] = why
    return res

# ---------------------------------------------------------------- analysis

def _link_names(links):
    """Accept ["a","b"] or [{"name":"a","x":1,"y":2}] and return the name list."""
    names = []
    for link in links or []:
        if isinstance(link, dict):
            n = link.get('name')
        else:
            n = link
        if n:
            names.append(str(n))
    return names


def _link_objects(links):
    out = []
    for link in links or []:
        if isinstance(link, dict):
            out.append(msch.LogicLink(str(link.get('name', '')), int(link.get('x', 0)),
                                      int(link.get('y', 0))))
        else:
            out.append(msch.LogicLink(str(link), 0, 0))
    return out


def _link_stem(name):
    """message2 -> message. Link names are <blockname><ordinal> (LogicBlock.java:331-360)."""
    m = re.match(r'^(.*?)(\d+)$', str(name))
    return m.group(1) if m else str(name)


def _nearest_link(name, links, origin):
    """A bound link of the same block family as `name`, closest to the processor."""
    base = _link_stem(name)
    ox = oy = None
    if isinstance(origin, dict):
        try:
            ox, oy = int(origin.get('x') or 0), int(origin.get('y') or 0)
        except (TypeError, ValueError):
            ox = oy = None
    best, best_d = None, None
    for link in links or []:
        if isinstance(link, dict):
            n = str(link.get('name') or '')
            try:
                x, y = int(link.get('x') or 0), int(link.get('y') or 0)
            except (TypeError, ValueError):
                x = y = 0
        else:
            n, x, y = str(link), 0, 0
        if not n or _link_stem(n) != base:
            continue
        d = 0 if ox is None else (x - ox) ** 2 + (y - oy) ** 2
        if best is None or d < best_d:
            best, best_d = n, d
    return best


def _substitute_links(code, subs):
    """Rewrite whole-word occurrences of a link name; message1 must not touch message10."""
    out = code
    for old, new in subs.items():
        out = re.sub(r'(?<![A-Za-z0-9_-])' + re.escape(old) + r'(?![A-Za-z0-9_-])', lambda _m, n=new: n, out)
    return out

def _relative_links(links, origin):
    """Payload link coordinates, relative to the processor tile.

    LogicBlock.config(byte[]) -> readCompressed(data, true) adds the processor tile
    back to every link coordinate (LogicBlock.java:304-307), so the byte[] payload
    stores them RELATIVE. The mod reports absolute tile positions (LogicLink.x/y are
    absolute), so subtract origin when the caller tells us where the processor is.
    Without an origin the coordinates are passed through untouched (already relative).
    """
    ox = oy = None
    if isinstance(origin, dict):
        ox, oy = int(origin.get('x') or 0), int(origin.get('y') or 0)
    out = []
    for link in links or []:
        if isinstance(link, dict):
            x, y = int(link.get('x', 0) or 0), int(link.get('y', 0) or 0)
            if ox is not None:
                x, y = x - ox, y - oy
            out.append(msch.LogicLink(str(link.get('name', '')), x, y))
        else:
            out.append(msch.LogicLink(str(link), 0, 0))
    return out


def analyse(code, links, ipt=2):
    """Run the same analyzer the gate runs; return a JSON-ready dict.

    links is None when the caller does not know the processor's link table (a
    plain .mlog file), and [] when it knows the table is empty.  The mod reads
    LogicBlock.links, so its [] is authoritative: a link-shaped name in a link
    slot then really is unbound, and the gate must see it (v0.9.0; before this
    the empty list was collapsed to None and every hallucinated link name on a
    link-less processor slipped through).
    """
    names = _link_names(links) if links is not None else None
    lines = code.splitlines()
    # A label line is NOT a statement (LParser.java:117-125), so count slots with
    # the analyzer's own prepare() instead of counting non-blank lines.
    stmts = [(ln + 1, c) for ln, c in C.prepare(lines)]
    # Finding.line is a STATEMENT index; the panel highlights a line of the code
    # text, so translate through prepare()'s own (line, code) pairs.
    srcline = {i: ln for i, (ln, _) in enumerate(stmts)}
    findings = []
    for f in C.analyse(lines, links=names, ipt=ipt,
                       links_authoritative=links is not None):
        findings.append({'sev': f.sev, 'line': srcline.get(f.line, f.line + 1),
                         'stmt': f.line, 'rid': _rid(f), 'msg': f.msg})
    errors = sum(1 for f in findings if f['sev'] == 'ERR')
    warns = sum(1 for f in findings if f['sev'] == 'WARN')
    try:
        cost = C.cost_report(stmts, links=names)
    except Exception as exc:                       # cost model is advisory only
        cost = '  (cost model failed: %s)' % exc
    return {'ok': errors == 0, 'findings': findings, 'errors': errors, 'warns': warns,
            'statements': len(stmts), 'cost': cost}


def _rid(finding):
    try:
        from rule_ids import rule_id
        return rule_id(finding.sev, finding.msg)
    except Exception:
        return ''


def compose(req):
    """template + params + links -> code, findings, and the game's own byte payload."""
    tid = req.get('template')
    tmpl = TEMPLATES.get(tid)
    if tmpl is None:
        raise ValueError('unknown template %r (known: %s)' % (tid, ', '.join(sorted(TEMPLATES))))
    params = req.get('params') or {}
    if not isinstance(params, dict):
        raise ValueError('params must be an object')
    lines, refs = tmpl.build(params)
    code = '\n'.join(lines)
    links = req.get('links') or []
    names = _link_names(links)
    origin = req.get('origin')
    if origin is not None and not isinstance(origin, dict):
        raise ValueError('origin must be an object with x and y')
    # A template names the links it writes into the code (e.g. message1), but a player's
    # processor often holds message2 -- same block, different ordinal. Point the template
    # at the nearest bound link of the same block family instead of refusing outright,
    # and say so: silently printing into a different display would be worse than an INFO.
    subs, subs_info = {}, []
    for n in refs:
        if n in names:
            continue
        alt = _nearest_link(n, links, origin)
        if alt is not None:
            subs[n] = alt
            subs_info.append((n, alt))
    if subs:
        code = _substitute_links(code, subs)
    # v0.14.2: sign before analyzing, so the findings, the statement count and the
    # compiled payload all describe the text that actually gets written.  The
    # signature only touches the tail (the token rides in an unconditional jump's
    # value slot and the dead `end` behind it is dropped), so no line moves.
    code, sig = aigenerated.sign_text(code, SIGN_AUTHOR, force=True)
    # Anything still unbound is a hard error here: the analyzer would also say so, but a
    # template should name the link it wanted.
    missing = [n for n in refs if n not in names and n not in subs]
    res = analyse(code, links, ipt=int(req.get('ipt') or 2))
    for old, new in subs_info:
        res['findings'].insert(0, {
            'sev': 'INFO', 'line': 0, 'rid': 'template-link-substituted',
            'msg': 'template %r writes %s but this processor has no such link; used %s instead (nearest bound link of the same block)' % (tid, old, new)})
    if missing:
        res['findings'].insert(0, {
            'sev': 'ERR', 'line': 0, 'rid': 'template-needs-link',
            'msg': 'template %r writes %s into the code but the processor has no such link '
                   '(link names are <blockname><n>, e.g. container1; see docs/速查卡.md section 6)'
                   % (tid, ', '.join(missing))})
        res['errors'] += 1
        res['ok'] = False
    payload = msch.build_logic_data(code, _relative_links(links, origin))
    return {'ok': res['ok'], 'template': tid, 'code': code, 'sig': sig,
            'findings': res['findings'], 'errors': res['errors'], 'warns': res['warns'],
            'statements': res['statements'], 'cost': res['cost'],
            'links': _link_names(links),
            'origin': origin,
            'bytes_b64': base64.b64encode(payload).decode('ascii'),
            'bytes_len': len(payload)}


def capabilities():
    try:
        sem = json.load(open(os.path.join(HERE, 'instr_semantics.json'), encoding='utf-8'))
    except Exception:
        sem = {}
    ins = sem.get('instructions') or {}
    ops = sorted((sem.get('ops') or {}).keys())
    return {'ok': True, 'protocol': PROTOCOL, 'game': GAME,
            'instructions': sorted(ins.keys()),
            'ops': list(ops),
            'templates': [t.spec() for t in TEMPLATES.values()],
            'limits': {'ipt_max': IPT_MAX, 'link_range_tiles': LINK_RANGE_TILES}}


# ---------------------------------------------------------------- region

#: LogicBlock.range = 8 * 10 world units (LogicBlock.java:49) and tilesize is 8,
#: so a link is legal while centre distance <= 80 + other.size*4 (LogicBlock.java:736
#: uses other.within(this, range + other.block.size*tilesize/2f)).
TILESIZE = 8


def _block_sizes():
    try:
        return json.load(open(os.path.join(HERE, 'block_sizes.json'), encoding='utf-8'))
    except Exception:
        return {}


def _block_size(block, sizes):
    try:
        return max(1, int(sizes.get(block) or 1))
    except Exception:
        return 1


def _link_base(block):
    """LogicBlock.getLinkName() (LogicBlock.java:123-135).

    The last '-' segment, unless that segment is 'large' or numeric -- then the
    second-to-last one.  Kept in step with mschgen.link_name_for() by a test.
    """
    name = str(block)
    if '-' in name:
        parts = name.split('-')
        last = parts[-1]
        if len(parts) >= 2 and (last == 'large' or re.fullmatch(r'[0-9.]+', last)):
            name = parts[-2]
        else:
            name = last
    return name


def _taken_numbers(names, base):
    """findLinkName() (LogicBlock.java:331-360): numbers already used by base<n>."""
    taken = set()
    for n in names:
        if n.startswith(base):
            tail = n[len(base):]
            if tail.isdigit():
                taken.add(int(tail))
    return taken


def _uses(code, name):
    """True when the code reads that exact identifier (container1 != container10)."""
    return re.search(r'(?<![A-Za-z0-9_-])' + re.escape(name) + r'(?![A-Za-z0-9_])', code) is not None


def _code_idents(code):
    """Lower-case identifiers the code mentions -- possible link names."""
    return set(re.findall(r'(?<![A-Za-z0-9_@-])([a-z][a-z0-9_-]*)', code))


def _result_names(code):
    """Names the code assigns itself (result slot = token 1 on every mlog line)."""
    out = set()
    for line in code.splitlines():
        toks = line.split()
        if len(toks) >= 2:
            out.add(toks[1])
    return out


def _known_bases():
    """Every link base name the game could hand out, from the block table."""
    return {_link_base(b) for b in _block_sizes()}


def _mlog_words():
    """Instruction / subcommand / op words -- never a link reference."""
    words = set()
    # KEYWORDS / OTHER_KEYWORDS are deliberately absent: 'core' is a ulocate
    # parameter there, and 'core' is exactly the word this check exists for.
    for attr in ('OP_UNARY', 'OP_BINARY', 'OP_LEGACY'):
        v = getattr(C, attr, None)
        if isinstance(v, (set, frozenset, list, tuple)):
            words.update(str(x).lower() for x in v)
    specs = getattr(C, 'SPECS', None) or {}
    try:
        words.update(str(k).lower() for k in specs)
    except TypeError:
        pass
    words.update({'true', 'false', 'null', 'end'})
    return words


def _linkish(name, known, blocks):
    """Does this word look like a link name the game would hand out?

    'core' is the case that matters: the game never hands out 'core' (getLinkName
    turns core-shard into 'shard'), but that is exactly what people type.  So a
    word qualifies when it is a base name or a prefix of a block name.
    """
    if name in known:
        return True
    if len(name) < 3:
        return False
    return any(b.startswith(name) for b in blocks)


SNAP_DIR = os.path.join(WROOT, 'out', 'snapshots')


def snapshot_dump(req):
    """POST /snapshot -- keep one region snapshot on disk and say where it went.

    F3 in the game drags a rectangle and posts what the box contained.  P3 is
    deliberately model-free (docs/Mlog辅助-规划.md:100): the job of this endpoint is
    to prove *what the box actually contained*, so the file is the evidence, not a
    step towards code generation.
    """
    snap = req.get('snapshot')
    if not isinstance(snap, dict):
        raise ValueError('snapshot must be an object (main.js: maiSnapshot(x1,y1,x2,y2))')
    try:
        schema = int(snap.get('schema') or 0)
    except (TypeError, ValueError):
        raise ValueError('snapshot.schema must be 1')
    if schema != 1:
        raise ValueError('snapshot.schema must be 1')
    bounds = snap.get('bounds')
    if not isinstance(bounds, dict):
        raise ValueError('snapshot.bounds must be an object with x1,y1,x2,y2')
    for k in ('x1', 'y1', 'x2', 'y2'):
        v = bounds.get(k)
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError('snapshot.bounds.%s must be an integer' % k)
    body = json.dumps(snap, ensure_ascii=False, indent=1, sort_keys=True)
    data = body.encode('utf-8')
    digest = hashlib.sha1(data).hexdigest()[:8]
    stamp = time.strftime('%Y%m%d-%H%M%S')
    os.makedirs(SNAP_DIR, exist_ok=True)
    path = os.path.join(SNAP_DIR, 'snap-%s-%s.json' % (stamp, digest))
    with open(path, 'wb') as fh:
        fh.write(data)
    return {'ok': True,
            'path': os.path.relpath(path, WROOT).replace('\\', '/'),
            'bytes': len(data),
            'bounds': bounds,
            'cells': int(snap.get('cells') or 0),
            'processors': len(snap.get('processors') or [])}


def region(req):
    """A region snapshot from the game -> per-processor link candidates.

    The snapshot is main.js regionSnapshot(): blocks come back by their internal
    name, which is exactly what getLinkName() consumes.  Candidates are numbered
    the way the game numbers a freshly linked block, so 'wire container1' in the
    panel produces a name the processor will really see.

    Nothing here writes: the caller feeds a chosen candidate into /plan or
    /compose, and the payload that comes back is the game's own logic@config.
    """
    snap = req.get('snapshot')
    if not isinstance(snap, dict):
        raise ValueError('snapshot must be an object (main.js: maiSnapshot(x1,y1,x2,y2))')
    grid = snap.get('grid') or []
    procs = snap.get('processors') or []
    if not isinstance(grid, list) or not isinstance(procs, list):
        raise ValueError('snapshot.grid and snapshot.processors must be arrays')
    sizes = _block_sizes()
    out_procs = []
    for p in procs:
        if not isinstance(p, dict):
            continue
        px, py = int(p.get('x') or 0), int(p.get('y') or 0)
        pblock = str(p.get('block') or '')
        psize = _block_size(pblock, sizes)
        code = str(p.get('code') or '')
        raw_links = p.get('links') or []
        existing = []
        for link in raw_links:
            if isinstance(link, dict):
                existing.append(str(link.get('name') or ''))
            else:
                existing.append(str(link))
        pcx = px * TILESIZE + psize * TILESIZE / 2.0
        pcy = py * TILESIZE + psize * TILESIZE / 2.0
        buckets = {}
        for cell in grid:
            if not isinstance(cell, dict):
                continue
            bx, by = int(cell.get('x') or 0), int(cell.get('y') or 0)
            block = str(cell.get('block') or '')
            if not block or block == 'air':
                continue
            if bx == px and by == py:
                continue
            bsize = _block_size(block, sizes)
            bcx = bx * TILESIZE + bsize * TILESIZE / 2.0
            bcy = by * TILESIZE + bsize * TILESIZE / 2.0
            limit = 80.0 + bsize * TILESIZE / 2.0
            dist = ((pcx - bcx) ** 2 + (pcy - bcy) ** 2) ** 0.5
            if dist > limit:
                continue
            buckets.setdefault(_link_base(block), []).append(
                {'block': block, 'x': bx, 'y': by, 'dist': round(dist / TILESIZE, 2)})
        candidates = []
        for base in sorted(buckets):
            taken = _taken_numbers(existing, base)
            items = sorted(buckets[base], key=lambda c: (c['dist'], c['x'], c['y']))
            for item in items:
                n = 1
                while n in taken:
                    n += 1
                taken.add(n)
                name = '%s%d' % (base, n)
                candidates.append({'name': name, 'block': item['block'],
                                   'x': item['x'], 'y': item['y'], 'dist': item['dist'],
                                   'used': _uses(code, name)})
        candidates.sort(key=lambda c: (not c['used'], c['dist'], c['name']))
        # What the code reads but nothing answers to: not linked, and no candidate
        # with that name in range.  This is the 'core is never a link name' case --
        # the check that used to be silent (LogicBlock.getLinkName + findLinkName).
        known = _known_bases()
        blocks = set(sizes)
        words = _mlog_words()
        cand_names = {c['name'] for c in candidates}
        results = _result_names(code)
        # 'turret' is not a link just because 'turretX' (a radar result) is in the
        # code: a word followed by an upper-case letter is that longer variable.
        toks = set(code.split())
        missing = []
        for ident in sorted(_code_idents(code)):
            if any(t.startswith(ident) and len(t) > len(ident) and t[len(ident)].isupper()
                   for t in toks):
                continue
            if ident == 'air' or ident in words or ident in existing:
                continue
            if ident in cand_names or ident in results:
                continue
            base = ident.rstrip('0123456789') or ident
            if not _linkish(base, known, blocks):
                continue
            sug = ''
            for c in candidates:
                if c['name'].rstrip('0123456789') == base or str(c['block']).startswith(base):
                    sug = c['name']
                    break
            missing.append({'name': ident, 'base': base, 'suggest': sug})
        out_procs.append({'x': px, 'y': py, 'block': pblock, 'links': existing,
                          'statements': len(code.splitlines()),
                          'in_range': len(candidates),
                          'used_unlinked': [c['name'] for c in candidates if c['used']],
                          'missing': missing,
                          'candidates': candidates})
    return {'ok': True, 'schema': 1, 'map': snap.get('map'),
            'world': snap.get('world'), 'bounds': snap.get('bounds'),
            'cells': len(grid), 'truncated': bool(snap.get('truncated')),
            'processors': out_procs}

# ---------------------------------------------------------------- http

# ---------------------------------------------------------------- model tier (v0.11.0)
# 玩家在游戏里的设置页填地址 / 密钥 / 模型，运行期就能切到模型档，不用重启 sidecar。
# 密钥只进不出：GET /llm 只回 key_set 与掩码尾巴，绝不回明文。
LLM_STATE = {'kind': 'template', 'url': '', 'model': '', 'key': '', 'timeout': 60,
             'max_tokens': mlogprovider.DEFAULT_MAX_TOKENS}


def _mask_key(key):
    s = str(key or '')
    if not s:
        return ''
    if len(s) <= 4:
        return '*' * len(s)
    return '*' * 6 + s[-4:]


def llm_state():
    st = LLM_STATE
    return {'ok': True, 'kind': st['kind'], 'url': st['url'], 'model': st['model'],
            'key_set': bool(st['key']), 'key_hint': _mask_key(st['key']),
            'timeout': int(st['timeout'] or 60),
            'max_tokens': int(st.get('max_tokens') or mlogprovider.DEFAULT_MAX_TOKENS)}


def models_url(url):
    """'.../v1/chat/completions' -> '.../v1/models'; anything else gets '/models'."""
    u = str(url or '').strip()
    if not u:
        raise ValueError('model url is empty')
    for tail in ('/chat/completions', '/completions', '/responses'):
        if u.endswith(tail):
            return u[:-len(tail)] + '/models'
    u = u.rstrip('/')
    if u.endswith('/models'):
        return u
    return u + '/models'


def completion_url(url):
    """A bare API base ('.../v1') becomes the chat endpoint; full endpoints pass through."""
    u = str(url or '').strip()
    if not u:
        raise ValueError('model url is empty')
    low = u.lower().rstrip('/')
    for tail in ('/chat/completions', '/completions', '/responses'):
        if low.endswith(tail):
            return u
    return u.rstrip('/') + '/chat/completions'


def probe_model(req):
    """POST /llm/probe -- one tiny chat call, so a player can see what the endpoint really says.

    The job path only reports 'empty reply' or 'HTTP 404', which leaves a player guessing.
    This returns the raw body excerpt, finish_reason, usage and the message keys instead.
    """
    url = str(req.get('url') or LLM_STATE['url'] or '').strip()
    if not url:
        raise ValueError('a model url is required (an OpenAI compatible /chat/completions)')
    if not (url.startswith('http://') or url.startswith('https://')):
        raise ValueError('the model url must start with http:// or https://')
    url = completion_url(url)
    model = str(req.get('model') if req.get('model') is not None else LLM_STATE['model']).strip()
    key = str(req.get('key') if req.get('key') is not None else LLM_STATE['key'])
    try:
        timeout = int(req.get('timeout') or LLM_STATE['timeout'] or 60)
    except (TypeError, ValueError):
        timeout = 60
    body = {'messages': [{'role': 'user',
                          'content': 'Reply with exactly this JSON and nothing else: {"ok": true}'}],
            'temperature': 0, 'max_tokens': max(512, int(LLM_STATE.get('max_tokens')
                                                              or 512) // 4)}
    if model:
        body['model'] = model
    out = {'ok': True, 'url': url, 'model': model, 'status': 0}
    request = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), method='POST')
    request.add_header('Content-Type', 'application/json')
    if key:
        request.add_header('Authorization', 'Bearer ' + key)
    raw = ''
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', 'replace')
            out['status'] = resp.status
    except urllib.error.HTTPError as exc:
        out['status'] = exc.code
        out['error'] = 'HTTP %s %s' % (exc.code, exc.reason)
        try:
            raw = exc.read().decode('utf-8', 'replace')
        except Exception:
            raw = ''
    except urllib.error.URLError as exc:
        out['error'] = 'cannot reach the endpoint: %s' % exc
    except OSError as exc:
        out['error'] = 'the call failed: %s' % exc
    out['raw_len'] = len(raw)
    out['raw'] = raw[:1200]
    try:
        doc = json.loads(raw) if raw else None
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        choices = doc.get('choices')
        c0 = choices[0] if isinstance(choices, list) and choices else None
        if isinstance(c0, dict):
            out['finish_reason'] = c0.get('finish_reason')
            msg = c0.get('message')
            if isinstance(msg, dict):
                content = msg.get('content')
                out['content_len'] = len(content) if isinstance(content, str) else 0
                out['content'] = content[:800] if isinstance(content, str) else ''
                out['message_keys'] = sorted([str(k) for k in msg.keys()])
                for alt in ('reasoning_content', 'reasoning'):
                    v = msg.get(alt)
                    if isinstance(v, str) and v:
                        out['reasoning_len'] = len(v)
        usage = doc.get('usage')
        if isinstance(usage, dict) and usage:
            out['usage'] = usage
        if doc.get('error'):
            out['error'] = str(doc.get('error'))[:400]
    return out


def fetch_models(url, key='', timeout=20):
    """Ask the endpoint what it serves. Runs on the sidecar so the mod never needs TLS."""
    target = models_url(url)
    req = urllib.request.Request(target, method='GET')
    req.add_header('Accept', 'application/json')
    if key:
        req.add_header('Authorization', 'Bearer ' + str(key))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        raise ValueError('model list refused: HTTP %s %s' % (exc.code, exc.reason))
    except urllib.error.URLError as exc:
        raise ValueError('cannot reach the model endpoint: %s' % (exc.reason,))
    except OSError as exc:
        raise ValueError('model list request failed: %s' % (exc,))
    try:
        doc = json.loads(raw)
    except ValueError:
        raise ValueError('the model endpoint did not answer with JSON')
    ids = []
    data = doc.get('data') if isinstance(doc, dict) else doc
    if isinstance(data, list):
        for item in data:
            mid = (item.get('id') or item.get('name')) if isinstance(item, dict) else item
            if mid:
                ids.append(str(mid))
    elif isinstance(doc, dict) and isinstance(doc.get('models'), list):
        for item in doc['models']:
            mid = (item.get('id') or item.get('name')) if isinstance(item, dict) else item
            if mid:
                ids.append(str(mid))
    out = sorted({s for s in ids if s})
    return {'ok': True, 'url': target, 'models': out, 'count': len(out)}


def apply_llm(req):
    """POST /llm -- switch the running queue between the model tier and「未配置模型」.

    v0.14.4：模板档已从产品里删掉（用户 2026-11-01），kind=template/off/none 现在
    表示「没有模型」，队列换成一个只会说「未配置模型」的 provider，不生成任何代码。
    """
    kind = str(req.get('kind') or 'llm').strip().lower()
    if kind in ('template', 'off', 'none', 'default'):
        LLM_STATE['kind'] = 'none'
        jobs_queue().provider = jobqueue.unconfigured_provider
        return llm_state()
    if kind not in ('llm', 'model'):
        raise ValueError('kind must be llm or none')
    url = str(req.get('url') or LLM_STATE['url'] or '').strip()
    if not url:
        raise ValueError('a model url is required (an OpenAI compatible /chat/completions)')
    if not (url.startswith('http://') or url.startswith('https://')):
        raise ValueError('the model url must start with http:// or https://')
    url = completion_url(url)
    model = str(req.get('model') if req.get('model') is not None else LLM_STATE['model']).strip()
    key = str(req.get('key') if req.get('key') is not None else LLM_STATE['key'])
    try:
        timeout = int(req.get('timeout') or LLM_STATE['timeout'] or 60)
    except (TypeError, ValueError):
        raise ValueError('timeout must be a whole number of seconds')
    if timeout < 5 or timeout > 600:
        raise ValueError('timeout must be between 5 and 600 seconds')
    try:
        max_tokens = int(req.get('max_tokens') or LLM_STATE.get('max_tokens')
                         or mlogprovider.DEFAULT_MAX_TOKENS)
    except (TypeError, ValueError):
        raise ValueError('max_tokens must be a whole number')
    if max_tokens < 64 or max_tokens > 64000:
        raise ValueError('max_tokens must be between 64 and 64000')
    LLM_STATE.update({'kind': 'llm', 'url': url, 'model': model, 'key': key,
                      'timeout': timeout, 'max_tokens': max_tokens})
    jobs_queue().provider = mlogprovider.HttpProvider(url, model=model, key=key,
                                                      timeout=timeout,
                                                      max_tokens=max_tokens)
    return llm_state()


BRIEF_DROP = ('snapshot', 'code', 'bytes_b64', 'reasoning', 'links', 'assumptions')


def job_brief(job):
    """A job without the parts that make a listing heavy (v0.12.5).

    The manager window lists up to fifty runs and re-reads the list every couple of
    seconds; a row only needs state, sentence, findings and timings.  The full
    record (reasoning, code, snapshot) stays one GET /jobs?id= away -- a rejected
    reasoning-model run carries ~12k characters of thinking, which is pointless to
    ship fifty times over loopback every second.
    """
    if not isinstance(job, dict):
        return job
    return {k: v for k, v in job.items() if k not in BRIEF_DROP}


class Handler(BaseHTTPRequestHandler):
    server_version = 'logicd/%s' % SIDECAR_VERSION

    def log_message(self, fmt, *args):        # keep stdout clean for the ready line
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def do_GET(self):
        path = self.path.split('?')[0]
        try:
            if path == '/health':
                self._send(200, {'ok': True, 'protocol': PROTOCOL,
                                 'version': SIDECAR_VERSION})
            elif path == '/capabilities':
                self._send(200, capabilities())
            elif path == '/jobs':
                q = self._query()
                jid = q.get('id')
                if jid:
                    job = jobs_queue().get(jid)
                    if job is None:
                        self._send(404, {'ok': False, 'error': 'no such job: ' + jid})
                    else:
                        self._send(200, {'ok': True, 'job': job})
                else:
                    state = q.get('state') or None
                    if state is not None and state not in jobqueue.STATES:
                        self._send(400, {'ok': False,
                                         'error': 'state must be one of ' + ', '.join(jobqueue.STATES)})
                    else:
                        # v0.12.5: all=1 -> the job manager window's history, newest first.
                        # The rows are brief (no reasoning/code/snapshot): see job_brief.
                        want_all = str(q.get('all') or '').strip().lower() in ('1', 'true', 'yes')
                        if want_all:
                            try:
                                limit = int(q.get('limit') or 50)
                            except (TypeError, ValueError):
                                limit = 50
                            if limit < 1:
                                limit = 50
                            limit = min(limit, 200)
                            jobs = [job_brief(j) for j in
                                    jobs_queue().history(limit=limit, state=state)]
                            self._send(200, {'ok': True, 'jobs': jobs,
                                             'count': len(jobs), 'all': True})
                        else:
                            jobs = jobs_queue().list(state)
                            self._send(200, {'ok': True, 'jobs': jobs, 'count': len(jobs)})
            elif path == '/llm':
                self._send(200, llm_state())
            else:
                self._send(404, {'ok': False, 'error': 'no such endpoint: ' + path})
        except Exception as exc:
            self._send(500, {'ok': False, 'error': '%s: %s' % (type(exc).__name__, exc)})

    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > 4 * 1024 * 1024:
                self._send(413, {'ok': False, 'error': 'request too large'})
                return
            raw = self.rfile.read(n) if n else b'{}'
            try:
                req = json.loads(raw.decode('utf-8') or '{}')
            except Exception as exc:
                self._send(400, {'ok': False, 'error': 'bad json: %s' % exc})
                return
            if not isinstance(req, dict):
                self._send(400, {'ok': False, 'error': 'body must be a JSON object'})
                return
            if path == '/analyze':
                code = req.get('code')
                if not isinstance(code, str):
                    self._send(400, {'ok': False, 'error': 'code must be a string'})
                    return
                res = analyse(code, req.get('links'), ipt=int(req.get('ipt') or 2))
                self._send(200, res)
            elif path == '/compose':
                try:
                    self._send(200, compose(req))
                except ValueError as exc:
                    self._send(400, {'ok': False, 'error': str(exc)})
            elif path == '/region':
                try:
                    self._send(200, region(req))
                except ValueError as exc:
                    self._send(400, {'ok': False, 'error': str(exc)})
            elif path == '/snapshot':
                try:
                    self._send(200, snapshot_dump(req))
                except ValueError as exc:
                    self._send(400, {'ok': False, 'error': str(exc)})
            elif path == '/plan':
                try:
                    self._send(200, plan(req))
                except ValueError as exc:
                    self._send(400, {'ok': False, 'error': str(exc)})
            elif path == '/llm':
                try:
                    self._send(200, apply_llm(req))
                except ValueError as exc:
                    self._send(400, {'ok': False, 'error': str(exc)})
            elif path == '/llm/probe':
                try:
                    self._send(200, probe_model(req))
                except ValueError as exc:
                    self._send(400, {'ok': False, 'error': str(exc)})
            elif path == '/models':
                murl = str(req.get('url') or LLM_STATE['url'] or '')
                mkey = req.get('key') if req.get('key') is not None else LLM_STATE['key']
                try:
                    mto = int(req.get('timeout') or 20)
                except (TypeError, ValueError):
                    mto = 20
                try:
                    self._send(200, fetch_models(murl, mkey, mto))
                except ValueError as exc:
                    self._send(400, {'ok': False, 'error': str(exc)})
            elif path == '/jobs':
                body = req.get('job') if isinstance(req.get('job'), dict) else req
                try:
                    job, created = jobs_queue().enqueue(body)
                except ValueError as exc:
                    self._send(400, {'ok': False, 'error': str(exc)})
                else:
                    self._send(200, {'ok': True, 'id': job['id'], 'state': job['state'],
                                     'created': created, 'job': job})
            else:
                self._send(404, {'ok': False, 'error': 'no such endpoint: ' + path})
        except Exception:
            self._send(500, {'ok': False, 'error': traceback.format_exc(limit=3)})

    def do_DELETE(self):
        path = self.path.split('?')[0]
        try:
            if path != '/jobs':
                self._send(404, {'ok': False, 'error': 'no such endpoint: ' + path})
                return
            jid = self._query().get('id')
            if not jid:
                self._send(400, {'ok': False, 'error': 'DELETE /jobs needs ?id='})
                return
            job = jobs_queue().cancel(jid)
            if job is None:
                self._send(404, {'ok': False, 'error': 'no such job: ' + jid})
            else:
                self._send(200, {'ok': True, 'id': job['id'], 'state': job['state'],
                                 'job': job})
        except Exception:
            self._send(500, {'ok': False, 'error': traceback.format_exc(limit=3)})


#: one queue per process; main() may replace it (--jobs-provider / --jobs-dir)
JOBS = None


def jobs_queue():
    global JOBS
    if JOBS is None:
        JOBS = jobqueue.Queue().start()
    return JOBS


def serve(host, port):
    srv = ThreadingHTTPServer((host, port), Handler)
    real = srv.server_address
    print('logicd listening on http://%s:%d' % (real[0], real[1]), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser(description='local half of the in-game logic-writing service')
    ap.add_argument('--host', default='127.0.0.1', help='bind address (loopback by default)')
    ap.add_argument('--port', type=int, default=8765, help='0 = pick a free port')
    ap.add_argument('--print-template', metavar='ID', help='dump one template and exit')
    ap.add_argument('--plan', metavar='TEXT', help='route one intent, print the JSON result, exit')
    ap.add_argument('--jobs-dir', metavar='DIR',
                    help='where job files live (default out/jobs, or $MLOG_JOBS_DIR)')
    ap.add_argument('--jobs-provider', choices=('none', 'template', 'fake', 'llm'),
                    default='none',
                    help='none = 未配置模型, the product default since v0.14.4; '
                         'template = the offline intent router (kept for tests); '
                         'fake = test table; llm = a real model endpoint (or a recorded replay)')
    ap.add_argument('--fake-provider', metavar='FILE',
                    help='JSON table for --jobs-provider fake: {"<desc substring>": output}')
    ap.add_argument('--llm-url', metavar='URL',
                    help='OpenAI-compatible /chat/completions URL for --jobs-provider llm')
    ap.add_argument('--llm-model', metavar='NAME', default='', help='model name to send, if any')
    ap.add_argument('--llm-key-env', metavar='VAR', default='',
                    help='env var holding the bearer token (never passed on the command line)')
    ap.add_argument('--llm-timeout', type=int, default=60, help='model call timeout in seconds')
    ap.add_argument('--llm-max-tokens', type=int, default=mlogprovider.DEFAULT_MAX_TOKENS,
                    help='reply budget per call; a reasoning model spends it on '
                         'reasoning_content before it writes anything (default %d)'
                         % mlogprovider.DEFAULT_MAX_TOKENS)
    ap.add_argument('--llm-replay', metavar='FILE',
                    help='replay recorded replies instead of calling a model (offline tests)')
    ap.add_argument('--llm-record', metavar='FILE',
                    help='append every {prompt, reply} to this JSONL file (real transcripts)')
    ap.add_argument('--jobs-drain', action='store_true',
                    help='run the queued jobs to completion, print them, exit')
    a = ap.parse_args(argv)
    if a.plan:
        out = plan({'intent': a.plan})
        out.pop('bytes_b64', None)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if a.jobs_provider == 'fake' and not a.fake_provider:
        print('--jobs-provider fake needs --fake-provider FILE')
        return 2
    try:
        provider = mlogprovider.provider_from_args(a)
    except ValueError as exc:
        print(str(exc))
        return 2
    LLM_STATE.update({'kind': a.jobs_provider if a.jobs_provider in ('llm', 'fake') else 'none',
                      'url': a.llm_url or '', 'model': a.llm_model or '',
                      'key': (os.environ.get(a.llm_key_env) if a.llm_key_env else '') or '',
                      'timeout': int(a.llm_timeout or 60),
                      'max_tokens': int(a.llm_max_tokens or mlogprovider.DEFAULT_MAX_TOKENS)})
    global JOBS
    JOBS = jobqueue.Queue(jobqueue.Store(a.jobs_dir), provider=provider)
    if a.jobs_drain:
        JOBS.drain()
        JOBS.stop()
        print(json.dumps({'ok': True, 'jobs': JOBS.list(None)}, ensure_ascii=False, indent=1))
        return 0
    if a.print_template:
        t = TEMPLATES.get(a.print_template)
        if t is None:
            print('unknown template %r; known: %s' % (a.print_template, ', '.join(sorted(TEMPLATES))))
            return 2
        print(t.build({})[0] and '\n'.join(t.build({})[0]))
        return 0
    JOBS.start()
    try:
        serve(a.host, a.port)
    finally:
        JOBS.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
