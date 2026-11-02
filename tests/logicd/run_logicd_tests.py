#!/usr/bin/env python3
"""Contract tests for the in-game logic-writing service (mod <-> tools/logicd.py).

Two halves, because they fail for different reasons:

  A. protocol   -- pure python: start logicd on an ephemeral port, drive it over
                   HTTP, assert the JSON shape the mod codes against.  This is the
                   half that must run everywhere, so it needs no JRE and no jars.
  B. write path -- one real v160.7 processor: take /compose's bytes_b64, hand them
                   to build.configure(bytes) exactly as the mod will, and read the
                   processor back.  This is what proves the payload is not merely
                   "our" encoding but the game's own -- the instruction classes the
                   game compiled (UnitBindI, RadarI, ...) are the evidence, plus
                   b.code and the link table surviving the round trip.

Why configure() and not "write the code field": LogicBlock.config(byte[].class)
is the vanilla channel a client uses to push code to the server
(Mindustry-Source/core/src/mindustry/world/blocks/logic/LogicBlock.java:65-69),
and the mod writes through it so multiplayer works without inventing sync.

Exit: 0 everything passed, 1 a check failed, 3 half B could not run (no JRE or no
versioned jar -- the jars live in out/ and are not in git, so a fresh clone SKIPs
half B instead of failing).

Usage
-----
    python tests/logicd/run_logicd_tests.py            # both halves
    python tests/logicd/run_logicd_tests.py --no-sim   # protocol half only
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

LINKS = [{'name': 'container1', 'x': 1, 'y': 2}, {'name': 'message1', 'x': 3, 'y': 4},
         {'name': 'turret1', 'x': 5, 'y': 6}, {'name': 'cell1', 'x': 7, 'y': 8}]

GOOD = 'sensor n container1 @copper\njump ok greaterThanEq n 1\nok:\nprintflush message1\nend'
# zzz is read in control shootp's target slot, which takes an OBJECT: null there
# does not become 0, so this must be refused (rule undefined-var-read, ERR).
BAD = 'control shootp turret1 zzz 1\nend'


class Fail(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise Fail(msg)


class Server:
    """logicd on an ephemeral port; the ready line carries the real one."""

    def __init__(self, extra=()):
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / 'tools' / 'logicd.py'), '--port', '0'] + list(extra),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        self.port = None
        deadline = time.time() + 40
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            if 'listening on' in line:
                self.port = int(line.strip().rsplit(':', 1)[1])
                break
        if self.port is None:
            self.close()
            raise Fail('logicd never printed its ready line')

    @property
    def base(self):
        return 'http://127.0.0.1:%d' % self.port

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=30) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def post(self, path, obj):
        req = urllib.request.Request(self.base + path, data=json.dumps(obj).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def delete(self, path):
        req = urllib.request.Request(self.base + path, method='DELETE')
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# ---------------------------------------------------------------- half A

def test_protocol():
    """Returns the capabilities payload so half B reuses the same template list."""
    checks = 0
    srv = Server()
    try:
        st, health = srv.get('/health')
        need(st == 200 and health.get('ok') is True, 'health: %r' % (health,))
        need(health.get('protocol') == 1, 'health protocol: %r' % (health,))
        # a stale process answers 200 too, so /health has to name the build it came from
        ver = health.get('version')
        need(isinstance(ver, str) and ver.count('.') == 2,
             '/health does not say which sidecar build answered: %r' % (health,))
        checks += 1

        st, cap = srv.get('/capabilities')
        need(st == 200 and cap.get('ok') is True, 'capabilities status %s' % st)
        need(len(cap.get('instructions') or []) >= 50,
             'capabilities instructions: %d' % len(cap.get('instructions') or []))
        need(len(cap.get('ops') or []) >= 40, 'capabilities ops: %d' % len(cap.get('ops') or []))
        need(cap['limits']['ipt_max'] == 40, 'ipt_max: %r' % (cap['limits'],))
        tids = [t['id'] for t in cap.get('templates') or []]
        need(len(tids) >= 6, 'templates: %r' % (tids,))
        for t in cap['templates']:
            need(t.get('title') and isinstance(t.get('params'), list),
                 'template %r has no title/params' % t.get('id'))
        checks += 1

        st, res = srv.post('/analyze', {'code': GOOD, 'links': LINKS, 'ipt': 2})
        need(st == 200 and res['ok'] is True, 'analyze good code refused: %r' % (res.get('findings'),))
        need(res['statements'] == 4, 'statements: %r (labels are not statements)' % res['statements'])
        need(bool(res.get('cost')), 'analyze returned no cost report')
        checks += 1

        st, res = srv.post('/analyze', {'code': BAD, 'links': LINKS, 'ipt': 2})
        need(st == 200 and res['ok'] is False, 'analyze accepted an ERR program')
        need(res['errors'] >= 1, 'errors: %r' % res['errors'])
        rids = [f['rid'] for f in res['findings'] if f['sev'] == 'ERR']
        need('undefined-var-read' in rids, 'ERR findings: %r' % rids)
        checks += 1

        import aigenerated
        import msch
        for tid in tids:
            st, res = srv.post('/compose', {'template': tid, 'params': {}, 'links': LINKS})
            need(st == 200, 'compose %s status %s' % (tid, st))
            need(res['ok'] is True and res['errors'] == 0,
                 'compose %s not clean: %r' % (tid, res.get('findings')))
            need(not res['findings'], 'compose %s findings: %r' % (tid, res['findings']))
            parsed = msch.parse_logic(base64.b64decode(res['bytes_b64']))
            need(parsed.code.strip() == res['code'].strip(),
                 'compose %s bytes decode to a different code' % tid)
            need([l.name for l in parsed.links] == [l['name'] for l in LINKS],
                 'compose %s link table: %r' % (tid, [l.name for l in parsed.links]))
            # v0.14.2: what /compose hands out is signed, so the mod can write it
            # and read it back unchanged (armWriteCheck compares b.code verbatim)
            need(re.match(r'^[0-9a-f]{12}$', res.get('sig') or ''),
                 'compose %s: sig is not a 12-hex id: %r' % (tid, res.get('sig')))
            need(res['code'].count('"aigenerated:') == 1,
                 'compose %s: %d @aigenerated token(s) in the code, want 1'
                 % (tid, res['code'].count('"aigenerated:')))
            need(aigenerated.verify_text(res['code']) == [],
                 'compose %s: the signature does not verify: %r'
                 % (tid, aigenerated.verify_text(res['code'])))
            checks += 1

        # Link coordinates in the payload are RELATIVE to the processor
        # (LogicBlock.config -> readCompressed(data, true), LogicBlock.java:304-307),
        # while the mod reports absolute tile positions. /compose has to subtract the
        # origin; before v0.4.6 it wrote absolute numbers and every link pointed at an
        # empty tile (the game kept the entry, silently with valid=false).
        abs_links = [{'name': 'container1', 'x': 30, 'y': 40},
                     {'name': 'message1', 'x': 32, 'y': 40}]
        st, res = srv.post('/compose', {'template': 'item-threshold',
                                        'params': {'item': 'copper', 'threshold': 100},
                                        'links': abs_links, 'origin': {'x': 10, 'y': 10}})
        need(st == 200 and res['ok'] is True, 'compose with origin: %r' % (res.get('findings'),))
        got = msch.parse_logic(base64.b64decode(res['bytes_b64']), relative_to=(10, 10))
        need([(l.name, l.x, l.y) for l in got.links]
             == [('container1', 30, 40), ('message1', 32, 40)],
             'payload link coords are not relative to the origin: %r'
             % ([(l.name, l.x, l.y) for l in got.links],))
        raw = msch.parse_logic(base64.b64decode(res['bytes_b64']))
        need([(l.x, l.y) for l in raw.links] == [(20, 30), (22, 30)],
             'origin was not subtracted in the payload: %r' % ([(l.x, l.y) for l in raw.links],))
        st, res = srv.post('/compose', {'template': 'item-threshold',
                                        'params': {'item': 'copper', 'threshold': 100},
                                        'links': [{'name': 'container1', 'x': 3, 'y': 4}]})
        raw = msch.parse_logic(base64.b64decode(res['bytes_b64']))
        need([(l.x, l.y) for l in raw.links] == [(3, 4)],
             'origin-less compose moved the coordinates: %r' % ([(l.x, l.y) for l in raw.links],))
        st, res = srv.post('/compose', {'template': 'item-threshold',
                                        'params': {'item': 'copper', 'threshold': 100},
                                        'links': abs_links, 'origin': 7})
        need(st == 400 and res['ok'] is False, 'a non-object origin was accepted: %s' % st)
        checks += 1

        st, res = srv.post('/compose', {'template': 'radar-fire', 'links': ['message1']})
        need(st == 200 and res['ok'] is False, 'compose with a missing link was accepted')
        rids = [f['rid'] for f in res['findings']]
        need('template-needs-link' in rids, 'missing-link findings: %r' % rids)
        checks += 1

        # a processor that holds message2 instead of message1 is the common case; the
        # template is pointed at the nearest bound link of the same block and says so,
        # rather than refusing to print at all.
        st, res = srv.post('/compose', {'template': 'item-threshold',
                                        'params': {'item': 'copper', 'threshold': 120},
                                        'links': [{'name': 'container1', 'x': 198, 'y': 190},
                                                  {'name': 'message2', 'x': 260, 'y': 190},
                                                  {'name': 'message3', 'x': 199, 'y': 190}],
                                        'origin': {'x': 197, 'y': 190}})
        need(st == 200 and res['ok'] is True, 'message2 instead of message1 was refused: %r'
             % (res.get('findings'),))
        need('message3' in res['code'] and 'message1' not in res['code'],
             'nearest message link was not substituted: %r' % (res['code'],))
        need(any(f['rid'] == 'template-link-substituted' for f in res['findings']),
             'substitution was silent: %r' % (res['findings'],))
        checks += 1
        st, res = srv.post('/compose', {'template': 'no-such-template'})
        need(st == 400 and res['ok'] is False, 'unknown template status %s' % st)
        st, res = srv.post('/compose', {'template': 'patrol', 'params': {'x1': 'abc'}, 'links': LINKS})
        need(st == 400 and res['ok'] is False, 'bad param status %s' % st)
        st, res = srv.post('/analyze', {'code': 42})
        need(st == 400, 'non-string code status %s' % st)
        st, res = srv.get('/nope')
        need(st == 404, 'unknown endpoint status %s' % st)
        checks += 1

        # a real link typo is a WARN, not an ERR: the write is allowed but the panel
        # has to show it, so pin that the finding reaches the client at all.
        st, res = srv.post('/analyze', {'code': 'sensor n vault9 @copper\nprint n\nend', 'links': LINKS})
        need(st == 200 and any(f['rid'] == 'link-name-not-bound' for f in res['findings']),
             'a typo link name produced no link-name-not-bound finding')
        checks += 1

        # /plan: one sentence in, a template choice + params + the same payload out.
        # This is the seam a language model would take over, so the shape has to be
        # pinned: template, params, reason, and a payload that is already checked.
        cases = [
            ('let @flare patrol between 10 10 and 30 12', 'patrol'),
            ('watch copper in the container, shout when it reaches 250', 'item-threshold'),
            ('point the turret at the nearest enemy and fire', 'radar-fire'),
            ('count up in the cell by 5 every second', 'counter'),
            ('at index 3 store 42 in the memory cell', 'memory-table'),
            ('print a line every 2 seconds', 'timer-flush'),
        ]
        for text, want in cases:
            st, res = srv.post('/plan', {'intent': text, 'links': LINKS, 'ipt': 2})
            need(st == 200, 'plan %r status %s' % (text, st))
            need(res.get('template') == want,
                 'plan %r chose %r, wanted %r (reason %r)' % (text, res.get('template'), want, res.get('reason')))
            need(isinstance(res.get('params'), dict), 'plan %r params: %r' % (text, res.get('params')))
            need(res.get('reason'), 'plan %r gave no reason' % text)
            need(res.get('bytes_b64') and res.get('bytes_len', 0) > 0, 'plan %r returned no payload' % text)
            need(res.get('errors') == 0, 'plan %r produced ERR: %r' % (text, res.get('findings')))
            checks += 1
        # the params are extracted, not just the template name
        st, res = srv.post('/plan', {'intent': cases[0][0], 'links': LINKS})
        need(res['params'].get('unit') == 'flare' and res['params'].get('x2') == 30,
             'plan extracted patrol params: %r' % (res.get('params'),))
        st, res = srv.post('/plan', {'intent': cases[1][0], 'links': LINKS})
        need(res['params'].get('item') == 'copper' and res['params'].get('threshold') == 250
             and res['params'].get('container') == 'container1',
             'plan extracted item params: %r' % (res.get('params'),))
        checks += 1
        # Chinese intents: the sentence is Chinese, the game speaks English.  Both the
        # item name and the link kind have to survive the trip, or every Chinese
        # sentence silently lands on the defaults and still looks like it worked.
        st, res = srv.post('/plan', {'intent': '容器里铜够 100 就打印', 'links': LINKS})
        need(st == 200 and res.get('template') == 'item-threshold',
             'zh plan chose %r' % (res.get('template'),))
        zp = res.get('params') or {}
        need(zp.get('item') == 'copper' and zp.get('threshold') == 100
             and zp.get('container') == 'container1', 'zh item params: %r' % (zp,))
        st, res = srv.post('/plan', {'intent': '让 @flare 从 10 10 来回巡逻到 30 12', 'links': LINKS})
        zp = res.get('params') or {}
        need(res.get('template') == 'patrol' and zp.get('unit') == 'flare' and zp.get('x2') == 30,
             'zh patrol: %r %r' % (res.get('template'), zp))
        st, res = srv.post('/plan', {'intent': '每 2 秒报时一次', 'links': LINKS})
        need(res.get('template') == 'timer-flush' and (res.get('params') or {}).get('seconds') == 2,
             'zh timer: %r %r' % (res.get('template'), res.get('params')))
        checks += 1
        # a vague sentence is refused with the menu, not guessed at
        st, res = srv.post('/plan', {'intent': 'make something nice', 'links': LINKS})
        need(st == 200 and res['ok'] is False and res.get('template') is None,
             'plan on a vague intent: %r' % (res,))
        need(len(res.get('candidates') or []) >= 6, 'plan candidates: %r' % (res.get('candidates'),))
        st, res = srv.post('/plan', {'intent': '   '})
        need(st == 400 and res['ok'] is False, 'plan with an empty intent status %s' % st)
        checks += 1


        # /region: the in-game snapshot -> link candidates, numbered the way the game
        # numbers them (LogicBlock.findLinkName).  Still read-only: the artifact the
        # workflow ends on is logic@config, produced by /compose from the same links.
        snap = {
            'schema': 1, 'map': 'test', 'world': {'width': 200, 'height': 200},
            'bounds': {'x1': 8, 'y1': 8, 'x2': 24, 'y2': 12}, 'truncated': False,
            'grid': [
                {'x': 10, 'y': 10, 'block': 'micro-processor'},
                {'x': 13, 'y': 10, 'block': 'container'},
                {'x': 20, 'y': 10, 'block': 'container'},
                {'x': 21, 'y': 10, 'block': 'switch'},
                {'x': 40, 'y': 40, 'block': 'container'},
                {'x': 15, 'y': 10, 'block': 'core-shard'},
            ],
            'processors': [
                {'x': 10, 'y': 10, 'block': 'micro-processor',
                 'code': 'sensor n container1 @copper\nprintflush message1\nend',
                 'links': [{'name': 'message1', 'x': 11, 'y': 10}]},
            ],
        }
        st, res = srv.post('/region', {'snapshot': snap})
        need(st == 200 and res.get('ok') is True, 'region status %s: %r' % (st, res))
        procs = res.get('processors') or []
        need(len(procs) == 1, 'region processors: %r' % (procs,))
        cand = {c['name']: c for c in procs[0]['candidates']}
        need('container1' in cand, 'region candidates: %r' % (sorted(cand),))
        need(cand['container1']['used'] is True, 'container1 was not marked as used by the code')
        need(cand['container1']['x'] == 13 and cand['container1']['y'] == 10,
             'container1 points at %r' % (cand['container1'],))
        # range is the game's: 80 units + half the target block (LogicBlock.java:736),
        # so a 2x2 container 10.5 tiles away is linkable while a 1x1 switch at 11 is not.
        need('container2' in cand, 'a 2x2 container at 10.5 tiles should be linkable: %r' % (sorted(cand),))
        need('switch1' not in cand, 'a 1x1 switch at 11 tiles is out of range but was offered')
        need(procs[0]['used_unlinked'] == ['container1'],
             'used_unlinked: %r' % (procs[0]['used_unlinked'],))
        # a name the code reads that nothing answers to: 'core' is never a link
        # name (LogicBlock.getLinkName), so the panel must say so instead of
        # showing a candidate list that silently omits it.
        snap_m = json.loads(json.dumps(snap))
        snap_m['processors'][0]['code'] = ('sensor n core @copper\n'
                                           'printflush message1\nend')
        st, res = srv.post('/region', {'snapshot': snap_m})
        miss = res['processors'][0].get('missing') or []
        need([m['name'] for m in miss] == ['core'], 'region missing: %r' % (miss,))
        need(miss[0]['suggest'] == 'shard1',
             'missing did not suggest the same-kind candidate: %r' % (miss[0],))
        # a variable is not a link: the result slot never counts as a link name
        snap_v = json.loads(json.dumps(snap))
        snap_v['processors'][0]['code'] = 'set core 1\nprint core\nend'
        st, res = srv.post('/region', {'snapshot': snap_v})
        need((res['processors'][0].get('missing') or []) == [],
             'a locally assigned variable was reported as an unbound link')
        # an existing link keeps its number: the next container becomes 2, not 1
        snap2 = json.loads(json.dumps(snap))
        snap2['processors'][0]['links'].append({'name': 'container1', 'x': 13, 'y': 10})
        st, res = srv.post('/region', {'snapshot': snap2})
        names = sorted(c['name'] for c in res['processors'][0]['candidates'])
        need('container2' in names and 'container1' not in names,
             'numbering ignored an existing link name: %r' % (names,))
        st, res = srv.post('/region', {'snapshot': 42})
        need(st == 400 and res['ok'] is False, 'region with a non-object snapshot: %s' % st)
        # link naming must stay in step with mschgen.link_name_for (getLinkName)
        import logicd as LD
        import mschgen as G
        drift = [(b, G.link_name_for(b, 1), LD._link_base(b) + '1')
                 for b in sorted(LD._block_sizes())
                 if G.link_name_for(b, 1) != LD._link_base(b) + '1']
        need(not drift, 'link naming drifted from mschgen.link_name_for: %r' % (drift[:5],))
        # F3 框选落盘（v0.4.8）：/snapshot 只做一件事 —— 把框到的东西留在盘上，好回答
        # 「框选到底拿到了什么」（docs/Mlog辅助-规划.md:100）。它不生成代码，也不接受
        # 任何「大概像快照」的东西：schema 与 bounds 是硬门槛。
        st, res = srv.post('/snapshot', {'snapshot': snap})
        need(st == 200 and res.get('ok') is True, 'snapshot status %s: %r' % (st, res))
        need(res['bounds'] == snap['bounds'], 'snapshot bounds echoed: %r' % (res.get('bounds'),))
        need(res['processors'] == 1, 'snapshot processors: %r' % (res.get('processors'),))
        need(res['cells'] == 0, 'snapshot cells must come from the payload: %r' % (res.get('cells'),))
        path = ROOT / res['path']
        need(path.is_file(), 'snapshot file missing: %s' % path)
        saved = json.loads(path.read_text(encoding='utf-8'))
        need(saved == snap, 'the file on disk is not the snapshot that was posted')
        need(res['bytes'] == path.stat().st_size, 'bytes does not match the file size')
        path.unlink()   # out/ 是草稿区，但测试不该每次跑都留一份
        for bad, why in (({'snapshot': 42}, 'a non-object snapshot'),
                         ({'snapshot': dict(snap, schema=2)}, 'schema 2'),
                         ({'snapshot': {k: v for k, v in snap.items() if k != 'bounds'}}, 'no bounds'),
                         ({'snapshot': dict(snap, bounds={'x1': 1.5, 'y1': 0, 'x2': 1, 'y2': 1})},
                          'a float bound')):
            st, res = srv.post('/snapshot', bad)
            need(st == 400 and res['ok'] is False, 'snapshot with %s: %s %r' % (why, st, res))
        checks += 1
        checks += 1   # 上面这一段是独立的契约：/snapshot 的落盘与拒绝
        return cap, checks
    finally:
        srv.close()


# ---------------------------------------------------------------- half B

JS = """
var b = Vars.world.build(%d, %d);
if(b === null || b.executor === null){ print('NOPROC'); } else {
    var arr = Packages.java.util.Base64.getDecoder().decode('%s');
    b.configure(arr);
    var ins = b.executor.instructions;
    var cls = ins.length > 0 ? String(ins[0].getClass().getSimpleName()) : '-';
    /* every link has to resolve to a building: a wrong coordinate is not an error
       in the game, it just leaves link.valid = false and the name reads null. */
    var W = Vars.world, res = [];
    for(var i = 0; i < b.links.size; i++){
        var l = b.links.get(i);
        var bb = W.build(l.x, l.y);
        res.push(l.name + '>' + (bb === null ? 'null' : String(bb.block.name)));
    }
    print('CFG\\tlen=' + ins.length + '\\tcodeok=' + (b.code === %s) + '\\tlinks=' + b.links.size + '\\tfirst=' + cls + '\\tresolved=' + res.join(','));
}
void 0
"""


def test_write_path(cap):
    """compose -> bytes_b64 -> build.configure(bytes) -> read the processor back."""
    import mlogsim as M
    java = M._default_java()
    jar = M.V160_JAR
    if not java or not jar or not pathlib.Path(jar).is_file():
        print('SKIP write path: no JRE or missing jar %s' % jar)
        return 0, 'skip'
    tids = [t['id'] for t in cap['templates']]
    checks = 0
    srv = M.Server(java, jar)
    srv._core_spec = 'copper:100'
    srv._unit_spec = [['flare', 1]]
    srv.prepare()
    srvd = None
    try:
        # materialize the processor first, then reconfigure that same building for
        # every template: the game's own assembler runs on each configure(), so a
        # template the game cannot parse would come back with len=0.
        M.run_mlog(srv, 'p', 'noop', steps=1, update='none', processor='auto')
        # The processor has to sit away from (0,0): at the origin absolute and
        # relative coordinates are the same number, which is exactly why v0.4.5's
        # bug stayed invisible in this test.
        bx, by = srv.base
        px, py = bx + 16, by + 16
        srv.js(M.place_js(px, py, 'micro-processor'))
        # one real block per link name, at the absolute tile the mod would report
        spots = [('container1', 1, 2, 'container'), ('message1', 3, 4, 'message'),
                 ('turret1', 5, 6, 'repair-turret'), ('cell1', 7, 8, 'memory-cell')]
        for _name, dx, dy, blk in spots:
            srv.js(M.place_js(px + dx, py + dy, blk))
        abs_links = [{'name': n, 'x': px + dx, 'y': py + dy} for n, dx, dy, _b in spots]
        origin = {'x': px, 'y': py}
        want_resolved = ','.join('%s>%s' % (n, b) for n, _dx, _dy, b in spots)
        # the same process the mod talks to, reached over HTTP -- no shortcut call
        # into compose() here, the bytes must travel the real path.
        srvd = Server()
        for tid in tids:
            st, res = srvd.post('/compose', {'template': tid, 'params': {},
                                             'links': abs_links, 'origin': origin})
            need(st == 200 and res['ok'], 'compose %s: %r' % (tid, res.get('findings')))
            out = srv.js(JS % (px, py, res['bytes_b64'], json.dumps(res['code'])))
            lines = out if isinstance(out, list) else str(out).splitlines()
            line = next((str(l) for l in lines if str(l).startswith('CFG\t')), None)
            need(line is not None, 'write path %s: no CFG line (got %r)' % (tid, lines))
            fields = dict(kv.split('=', 1) for kv in line.split('\t')[1:] if '=' in kv)
            need(fields.get('codeok') == 'true', '%s: b.code differs after configure' % tid)
            need(int(fields.get('len') or 0) > 0,
                 '%s: the game compiled 0 instructions (it rejected the payload)' % tid)
            need(int(fields.get('links') or 0) == len(LINKS),
                 '%s: link table want %d got %s' % (tid, len(LINKS), fields.get('links')))
            need(fields.get('resolved') == want_resolved,
                 '%s: links do not resolve to the placed blocks: %r (want %r)'
                 % (tid, fields.get('resolved'), want_resolved))
            checks += 1
        # the regression itself: the same absolute coordinates WITHOUT the origin are
        # what v0.4.5 sent, and they must not resolve -- if this ever passes, the
        # relative-coordinate conversion has been lost again.
        st, res = srvd.post('/compose', {'template': tids[0], 'params': {},
                                         'links': abs_links})
        out = srv.js(JS % (px, py, res['bytes_b64'], json.dumps(res['code'])))
        lines = out if isinstance(out, list) else str(out).splitlines()
        line = next((str(l) for l in lines if str(l).startswith('CFG\t')), None)
        need(line is not None, 'no-origin probe: no CFG line')
        fields = dict(kv.split('=', 1) for kv in line.split('\t')[1:] if '=' in kv)
        need('>null' in (fields.get('resolved') or ''),
             'absolute coords without an origin resolved anyway: %r' % (fields.get('resolved'),))
        checks += 1
        return checks, 'ok'
    finally:
        if srvd is not None:
            srvd.close()
        srv.close()


# ---------------------------------------------------------------- half C

FAKE = {
    '好': {'code': 'sensor n container1 @copper\nprint n\nend', 'explanation': 'fake good'},
    '幻觉': {'code': 'sensor x container9 @copper\nend', 'explanation': 'fake bad'},
    '修好': [{'code': 'sensor x container9 @copper\nend'},
             {'code': 'sensor n container1 @copper\nprint n\nend'}],
    '慢': {'sleep': 2.0, 'code': 'noop\nend'},
    '__default__': {'error': 'no fake response'},
}

JLINKS = [{'name': 'container1', 'x': 1, 'y': 2}, {'name': 'message1', 'x': 3, 'y': 4}]
JTERMINAL = ('done', 'rejected', 'failed', 'cancelled')


def wait_job(srv, jid, want=JTERMINAL, timeout=60):
    # 30 s once timed out during a full parallel gate (five heavy tiers at once) even
    # though the fake provider answers in milliseconds; a real hang still fails here.
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        st, res = srv.get('/jobs?id=' + jid)
        need(st == 200 and res.get('ok'), 'GET /jobs?id=%s: %s %r' % (jid, st, res))
        job = res['job']
        if job['state'] in want:
            return job
        time.sleep(0.1)
    raise Fail('job %s never reached %r (last state %r)' % (jid, want, (job or {}).get('state')))


def test_jobs():
    """P4 half: the queue state machine over HTTP (docs/Mlog辅助-规划.md section 4.5).

    No JRE, no model: a fake provider answers from FAKE, so this half runs anywhere.
    """
    import aigenerated
    import jobqueue
    checks = 0
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='logicd-jobs-', dir=str(ROOT / 'out')))
    fake = tmp / 'fake.json'
    fake.write_text(json.dumps(FAKE, ensure_ascii=False), encoding='utf-8')
    qdir = tmp / 'queue'
    srv = Server(['--jobs-dir', str(qdir), '--jobs-provider', 'fake',
                  '--fake-provider', str(fake)])
    try:
        # a clean program -> done on the first attempt, and the job file is on disk
        st, res = srv.post('/jobs', {'desc': '把铜打印出来（好）', 'links': JLINKS, 'ipt': 2})
        need(st == 200 and res.get('ok') is True and res.get('created') is True,
             'POST /jobs: %s %r' % (st, res))
        jid = res['id']
        need(len(jid) == 16 and all(c in '0123456789abcdef' for c in jid),
             'job id is not sha1[:16]: %r' % (jid,))
        job = wait_job(srv, jid, ('done',))
        need(job['attempts'] == 1, 'clean job took %r attempt(s)' % (job['attempts'],))
        need(job['code'].strip() and job.get('bytes_b64'),
             'done job carries no code/payload: %r'
             % ({k: job.get(k) for k in ('code', 'bytes_b64')},))
        need((qdir / (jid + '.json')).is_file(), 'job was not written to %s' % qdir)
        # v0.14.2: the queue signs before it gates, so the job's code IS the program
        # the mod writes -- and the mod compares it with b.code character by
        # character, so the token has to be in the job, not added at write time.
        need(job['code'].count('"aigenerated:') == 1,
             'a done job carries %d @aigenerated token(s), want 1: %r'
             % (job['code'].count('"aigenerated:'), job['code']))
        need(aigenerated.verify_text(job['code']) == [],
             'the job code does not verify: %r' % (aigenerated.verify_text(job['code']),))
        need(re.match(r'^[0-9a-f]{12}$', job.get('sig') or ''),
             'a done job has no 12-hex sig: %r' % (job.get('sig'),))
        need(job['sig'] == aigenerated.sign_text(job['code'], jobqueue.SIGN_AUTHOR)[1],
             'job sig %r is not the id of the code it wrote' % (job['sig'],))
        checks += 1

        # section 4.2: the same body is a no-op while that job is still in flight
        # (the provider sleeps 2 s, so the second POST cannot race the worker)
        slow_body = {'desc': '慢一点', 'links': JLINKS}
        st, first = srv.post('/jobs', slow_body)
        need(st == 200 and first.get('created') is True, 'POST /jobs (slow): %s %r' % (st, first))
        st, twin = srv.post('/jobs', slow_body)
        need(st == 200 and twin['id'] == first['id'] and twin['created'] is False,
             'a second POST while the job was in flight was not a no-op: %r' % (twin,))
        srv.delete('/jobs?id=' + first['id'])
        wait_job(srv, first['id'], ('cancelled',))
        checks += 1

        # v0.12.2: a *terminal* job must not keep its content id.  The id is a hash of
        # the request, so replaying a finished job answered every retry with the old
        # verdict -- a job rejected on 'provider-error: empty reply' kept saying
        # 'empty reply' after the endpoint was fixed, and the model was never asked.
        st, rerun = srv.post('/jobs', {'desc': '把铜打印出来（好）', 'links': JLINKS, 'ipt': 2})
        need(st == 200 and rerun.get('created') is True and rerun['id'] == jid + '-2',
             'a terminal job swallowed the retry: %r' % (rerun,))
        need(rerun['job'].get('retry_of') == jid,
             'the retry does not point at the first run: %r' % (rerun['job'].get('retry_of'),))
        rerun_job = wait_job(srv, rerun['id'], ('done',))
        need(rerun_job['attempts'] == 1 and rerun_job['code'].strip(),
             'the retry did not run again: %r'
             % ({k: rerun_job.get(k) for k in ('attempts', 'code')},))
        need((qdir / (rerun['id'] + '.json')).is_file(),
             'the retry was not written to disk as its own job file')
        checks += 1

        # a hallucinated link name is a refusal, never a write
        st, res = srv.post('/jobs', {'desc': '幻觉链接', 'links': JLINKS})
        bad = wait_job(srv, res['id'], ('rejected',))
        need(bad['code'] == '', 'a rejected job still carries code: %r' % (bad['code'],))
        need(bad['attempts'] == 3, 'rejected after %r attempt(s), want 3' % (bad['attempts'],))
        rids = [f['rid'] for f in bad['findings']]
        need('link-name-not-bound' in rids, 'findings lost the rule: %r' % (rids,))
        need(all(f['sev'] == 'ERR' for f in bad['findings'] if f['rid'] == 'link-name-not-bound'),
             'link-name-not-bound was not promoted to ERR for a job')
        checks += 1

        # v0.9.0: an empty link table is authoritative, so the hallucinated name is
        # caught even when the processor links to nothing (the v0.8.0 blind spot:
        # [] was collapsed to None and the whole link check was skipped).
        st, res = srv.post('/jobs', {'desc': '幻觉零链接', 'links': []})
        zero = wait_job(srv, res['id'], ('rejected',))
        need(any(f['rid'] == 'link-name-not-bound' for f in zero['findings']),
             'a link-less processor let a hallucinated link name through: %r'
             % (zero['findings'],))
        need(zero['code'] == '', 'a rejected zero-link job still carries code')
        checks += 1

        # links=null means "unknown", and the link check is skipped on purpose
        st, res = srv.post('/jobs', {'desc': '幻觉未知链接', 'links': None})
        unknown = wait_job(srv, res['id'], ('done',))
        need(unknown['code'].strip(), 'an unknown link table refused on its own')
        checks += 1

        # self-repair: the first attempt is refused, the second one lands
        st, res = srv.post('/jobs', {'desc': '先坏后修好', 'links': JLINKS})
        fixed = wait_job(srv, res['id'], ('done',))
        need(fixed['attempts'] == 2, 'self-repair used %r attempt(s), want 2' % (fixed['attempts'],))
        need(fixed['code'].strip(), 'self-repaired job has no code')
        checks += 1

        # cancel works while the job is running (the worker checks a checkpoint)
        st, res = srv.post('/jobs', {'desc': '慢任务', 'links': JLINKS})
        slow = res['id']
        time.sleep(0.3)
        st, c = srv.delete('/jobs?id=' + slow)
        need(st == 200 and c.get('ok'), 'DELETE /jobs: %s %r' % (st, c))
        gone = wait_job(srv, slow, ('cancelled',))
        need(gone['state'] == 'cancelled', 'cancelled job came back as %r' % (gone['state'],))
        checks += 1

        # the listing endpoints and their negative controls
        st, lst = srv.get('/jobs?state=done')
        need(st == 200 and lst['count'] >= 2 and all(j['state'] == 'done' for j in lst['jobs']),
             'GET /jobs?state=done: %s %r' % (st, lst.get('count')))
        st, lst = srv.get('/jobs')
        need(st == 200 and all(j['state'] not in JTERMINAL for j in lst['jobs']),
             'GET /jobs leaked a finished job: %r' % ([j['state'] for j in lst['jobs']],))
        st, _ = srv.get('/jobs?id=deadbeefdeadbeef')
        need(st == 404, 'GET /jobs?id=<unknown> -> %s, want 404' % st)
        st, _ = srv.get('/jobs?state=bogus')
        need(st == 400, 'GET /jobs?state=bogus -> %s, want 400' % st)
        st, _ = srv.post('/jobs', {'desc': ''})
        need(st == 400, 'POST /jobs with an empty desc -> %s, want 400' % st)
        checks += 1

        # v0.12.5: the manager window needs the opposite of GET /jobs -- what ran,
        # newest first, and without dragging 12k of reasoning into a fifty-row list
        st, hist = srv.get('/jobs?all=1&limit=3')
        need(st == 200 and hist.get('all') is True and 0 < hist['count'] <= 3,
             'GET /jobs?all=1: %s %r' % (st, hist.get('count')))
        need(any(j['state'] in JTERMINAL for j in hist['jobs']),
             'the history left the finished runs out: %r'
             % ([j['state'] for j in hist['jobs']],))
        created = [j.get('created') or 0 for j in hist['jobs']]
        need(created == sorted(created, reverse=True),
             'the history is not newest first: %r' % (created,))
        need(all('reasoning' not in j and 'code' not in j and 'snapshot' not in j
                 for j in hist['jobs']),
             'a history row drags the whole job along: %r'
             % (sorted(hist['jobs'][0]) if hist['jobs'] else [],))
        st, one = srv.get('/jobs?id=' + jid)
        need(st == 200 and 'code' in one['job'],
             'GET /jobs?id= stopped returning the full record: %s' % st)
        st, _ = srv.get('/jobs?all=1&limit=bogus')
        need(st == 200, 'GET /jobs?all=1&limit=bogus -> %s, want a defaulted 200' % st)
        checks += 1
    finally:
        srv.close()

    # v0.14.4（用户 2026-11-01「删除sidecar和模版档」）：产品默认不再是模板档。
    # 没配模型的队列必须把「未配置模型」说出来，一次就失败 —— 不能悄悄套个样板交差。
    srv2 = Server(['--jobs-dir', str(tmp / 'queue2')])
    try:
        st, res = srv2.post('/jobs', {'desc': '容器里铜够 100 就打印', 'links': JLINKS, 'ipt': 2})
        need(st == 200 and res.get('ok'), 'POST /jobs (unconfigured): %s %r' % (st, res))
        none = wait_job(srv2, res['id'], ('failed',))
        need('未配置模型' in (none.get('explanation') or ''),
             'an unconfigured queue did not say so: %r' % (none.get('explanation'),))
        need(none.get('code') == '', 'an unconfigured queue wrote code: %r' % (none.get('code'),))
        need(none.get('attempts') == 1,
             'the unconfigured error was retried %r time(s), want 1' % (none.get('attempts'),))
        checks += 1
    finally:
        srv2.close()

    # 模板档没死透：它退化成显式的开发/测试档位（--jobs-provider template），
    # 意图路由照旧能把一句中文变成真 mlog —— 只是产品路径不再走它。
    srv3 = Server(['--jobs-dir', str(tmp / 'queue3'), '--jobs-provider', 'template'])
    try:
        st, res = srv3.post('/jobs', {'desc': '容器里铜够 100 就打印', 'links': JLINKS, 'ipt': 2})
        need(st == 200 and res.get('ok'), 'POST /jobs (template tier): %s %r' % (st, res))
        auto = wait_job(srv3, res['id'], ('done',))
        need(auto.get('template') == 'item-threshold',
             'template tier picked %r, want item-threshold' % (auto.get('template'),))
        need('sensor' in auto['code'] and 'container1' in auto['code'],
             'template tier code looks wrong: %r' % (auto['code'],))
        # the template tier goes through compose(), which signs too -- a job that
        # reaches a processor must never be the one unsigned path
        need(auto['code'].count('"aigenerated:') == 1
             and aigenerated.verify_text(auto['code']) == []
             and re.match(r'^[0-9a-f]{12}$', auto.get('sig') or ''),
             'the template tier wrote an unsigned program: sig=%r code=%r'
             % (auto.get('sig'), auto['code']))
        checks += 1
    finally:
        srv3.close()

    # 双写可续: a job left in "running" by a crash is picked up again on startup
    store = jobqueue.Store(str(tmp / 'resume'))
    job = jobqueue.normalize({'desc': '把铜打印出来（好）', 'links': JLINKS, 'ipt': 2})
    job['state'] = 'running'
    job['attempts'] = 1
    store.save(job)
    q = jobqueue.Queue(store, provider=jobqueue.FakeProvider(
        {'把铜': {'code': 'sensor n container1 @copper\nend'}}))
    need(q.resumed == 1, 'resumed %r job(s), want 1' % (q.resumed,))
    need(q.get(job['id'])['state'] == 'queued', 'a crashed running job was not requeued')
    q.drain()
    need(q.get(job['id'])['state'] == 'done', 'resumed job did not finish')
    checks += 1

    # the promotion itself, without the HTTP layer in the way
    g = jobqueue.gate('sensor x container9 @copper\nend', ['container1'], ipt=2)
    need(g['ok'] is False and g['errors'] >= 1, 'gate accepted an unbound link: %r' % (g,))
    need(any(f.get('fatal') and f['sev'] == 'ERR' and f['rid'] == 'link-name-not-bound'
             for f in g['fatal']), 'gate did not promote link-name-not-bound: %r' % (g['fatal'],))
    checks += 1

    # v0.9.0: the empty table is authoritative, and a >200-statement fragment
    # heuristic no longer demotes the hit to INFO when the caller knows the table
    g0 = jobqueue.gate('sensor x container9 @copper\nend', [], ipt=2)
    need(g0['ok'] is False and any(f.get('fatal') for f in g0['fatal']),
         'gate accepted an unbound link on a link-less processor: %r' % (g0,))
    g1 = jobqueue.gate('sensor x container9 @copper\nend', None, ipt=2)
    need(g1['ok'] is True, 'gate refused when the link table is unknown: %r' % (g1,))
    long_code = '\n'.join(['set a %d' % i for i in range(201)]
                          + ['sensor x container9 @copper', 'end'])
    g2 = jobqueue.gate(long_code, [], ipt=2)
    need(any(f['rid'] == 'link-name-not-bound' for f in g2['fatal']),
         'a long link-less program lost the link refusal: %r' % (g2['findings'][:3],))
    checks += 1

    # v0.12.2: a save that loses the replace to something else holding the file
    # for a moment (OneDrive, a scanner) is retried instead of costing the job
    raw_replace = jobqueue._replace
    tries = {'n': 0}

    def stubborn(src, dst):
        tries['n'] += 1
        if tries['n'] <= 2:
            raise PermissionError(5, 'another handle held the file for a moment')
        return raw_replace(src, dst)

    jobqueue._replace = stubborn
    try:
        retry_store = jobqueue.Store(str(tmp / 'retry'))
        retry_job = jobqueue.normalize({'desc': '把铜打印出来（好）', 'links': JLINKS, 'ipt': 2})
        retry_store.save(retry_job)
    finally:
        jobqueue._replace = raw_replace
    need(retry_store.load(retry_job['id']) is not None, 'a collided save lost the job')
    need(tries['n'] == 3, 'a contended save was not retried: %d replace(s)' % tries['n'])
    checks += 1

    # v0.12.2: and when the write keeps failing, the job must not stay 'running'
    # for ever -- from the panel that looks like work still in progress
    strand_store = jobqueue.Store(str(tmp / 'strand'))
    real_save = jobqueue.Store.save
    poison = {'left': 1}

    def lose_one(self, obj):
        if poison['left'] and obj.get('state') == 'running':
            poison['left'] -= 1
            raise FileNotFoundError(2, 'the disk refused the write')
        return real_save(self, obj)

    jobqueue.Store.save = lose_one
    noise = io.StringIO()
    try:
        with contextlib.redirect_stderr(noise):
            stranded = jobqueue.Queue(strand_store, provider=jobqueue.FakeProvider(
                {'把铜': {'code': 'sensor n container1 @copper\nend'}}))
            stranded.start()
            try:
                one, _ = stranded.enqueue({'desc': '把铜打印出来（好）', 'links': JLINKS, 'ipt': 2})
                deadline = time.time() + 30
                while time.time() < deadline:
                    got = stranded.get(one['id'])
                    if got['state'] in JTERMINAL:
                        break
                    time.sleep(0.05)
                got = stranded.get(one['id'])
            finally:
                stranded.stop()
    finally:
        jobqueue.Store.save = real_save
    # the worker prints the traceback it swallowed; that is the path, not a failure
    need('FileNotFoundError' in noise.getvalue(),
         'the worker died without saying why: %r' % (noise.getvalue()[:200],))
    need(got['state'] == 'failed',
         'a job whose write kept failing ended as %r, want failed' % (got['state'],))
    need(any(f.get('rid') == 'worker-error' for f in got.get('findings') or []),
         'the stranded job did not carry worker-error: %r' % (got.get('findings'),))
    need(strand_store.load(one['id'])['state'] == 'failed',
         'the stranded job was never written down as failed')
    checks += 1

    shutil.rmtree(tmp, ignore_errors=True)
    return checks


# ---- half D: the model-facing provider (offline -- scripted replies, no network)

def test_provider():
    """Prompt shape, reply parsing, and scripted replay through the real queue."""
    import os
    import jobqueue
    import mlogprovider as MP

    checks = 0
    snap = {'bounds': {'x1': 4, 'y1': 14, 'x2': 16, 'y2': 26}, 'cells': 3,
            'truncated': False,
            'grid': [{'x': 10, 'y': 20, 'block': 'micro-processor'},
                     {'x': 11, 'y': 20, 'block': 'container'},
                     {'x': 12, 'y': 20, 'block': 'air'}],
            'processors': [{'x': 10, 'y': 20}],
            'map': {'name': 'groundZero'}}
    job = {'desc': '容器里铜够 100 就打印', 'links': LINKS, 'origin': {'x': 10, 'y': 20},
           'ipt': 2, 'snapshot': snap}

    # 1. the three sections of section 4.4, and the whitelist being explicit
    p = MP.build_prompt(job)
    for sec in ('=== world facts ===', '=== rules ===', '=== task ==='):
        need(sec in p, 'prompt lost the %s section' % sec)
    need('container1 @ (1, 2)' in p, 'prompt lost the link whitelist: %r' % p[:400])
    need('the player asked for: 容器里铜够 100 就打印' in p, 'prompt lost the task')
    need('needs_links' in p and '"code"' in p, 'prompt lost the JSON contract')
    need('container x1' in p, 'prompt lost the nearby-block summary')
    hint = [{'sev': 'ERR', 'line': 1, 'rid': 'link-name-not-bound', 'msg': 'x is not bound'}]
    p2 = MP.build_prompt(job, hint)
    need('REJECTED by the gate' in p2 and 'line 1: [ERR] x is not bound' in p2,
         'the self-repair hint is not in the prompt: %r' % p2[-400:])
    need('link whitelist: UNKNOWN' in MP.build_prompt(dict(job, links=None)),
         'an unknown link table is not called out')
    need('link whitelist: EMPTY' in MP.build_prompt(dict(job, links=[])),
         'an authoritative empty table is not called out')
    # v0.12.9: the snapshot carries stock / units / enemies now, and the model has to see
    # them; a mod-provided block count must win over the old grid fallback.
    snap2 = dict(snap, blocks={'container': 4, 'micro-processor': 1},
                 items={'copper': 100, 'lead': 12.5}, liquids={'water': 3},
                 units={'total': 2, 'types': {'dagger': 2}, 'teams': {'sharded': 2}},
                 enemies={'units': 1, 'blocks': 1, 'types': {'flare': 1}})
    p3 = MP.build_prompt(dict(job, snapshot=snap2))
    need('container x4' in p3,
         'v0.12.9: the mod block counts are ignored: %r' % p3[:700])
    need('items stored in the area: copper 100, lead 12.5' in p3,
         'v0.12.9: the item stock never reaches the prompt: %r' % p3[:900])
    need('liquids stored in the area: water 3' in p3,
         'v0.12.9: the liquid stock never reaches the prompt')
    need('units in the area: 2 (dagger x2; teams: sharded 2)' in p3,
         'v0.12.9: the unit counts never reach the prompt')
    need('enemies in the area: 1 unit(s) flare x1, 1 enemy building(s)' in p3,
         'v0.12.9: the enemy counts never reach the prompt')
    checks += 1

    # 2. parse_reply tolerates fences and prose, and refuses junk
    rep = MP.parse_reply('sure:\n```json\n{"code": "set n 0\\nend", "explanation": "ok",'
                         ' "assumptions": ["a"], "needs_links": ["container1"]}\n```\ndone')
    need(rep['code'] == 'set n 0\nend', 'fenced reply did not parse: %r' % (rep,))
    need(rep['needs_links'] == ['container1'] and rep['assumptions'] == ['a'],
         'reply fields lost: %r' % (rep,))
    need('template' not in rep, 'a free-tier reply grew a template')
    anchored = MP.parse_reply('{"template": "item-threshold", "params": {"item": "copper"}}')
    need(anchored.get('template') == 'item-threshold' and anchored['params']['item'] == 'copper',
         'anchored reply did not parse: %r' % (anchored,))
    for bad in ('', 'no json here', '{"code": 3}', '{"explanation": "x"}',
                '{"code": "set n 0", "needs_links": "container1"}'):
        try:
            MP.parse_reply(bad)
        except MP.ProviderError:
            continue
        raise Fail('parse_reply accepted junk: %r' % (bad,))
    checks += 1

    tmp = tempfile.mkdtemp(prefix='mlogprov-')
    try:
        # 3. a fenced scripted reply drives the real queue to done
        prov = MP.ScriptedProvider(['```json\n{"code": "set n 0\\nop add n n 1\\nprint n\\nend"}\n```'])
        q = jobqueue.Queue(jobqueue.Store(os.path.join(tmp, 'q1')), provider=prov)
        j, created = q.enqueue({'desc': 'scripted clean', 'links': LINKS,
                                'origin': {'x': 1, 'y': 1}, 'snapshot': snap})
        q.drain()
        got = q.get(j['id'])
        need(got['state'] == 'done' and got['code'].startswith('set n 0') and got['bytes_b64'],
             'a scripted clean reply did not reach done: %r' % (got,))
        need('=== world facts ===' in prov.prompts[0] and 'scripted clean' in prov.prompts[0],
             'the queue did not send the three-section prompt')
        checks += 1

        # 4. a hallucinated link is refused (3 attempts, no code, fatal finding)
        prov2 = MP.ScriptedProvider([{'code': 'sensor x container9 @copper\nend'}] * 3)
        q2 = jobqueue.Queue(jobqueue.Store(os.path.join(tmp, 'q2')), provider=prov2)
        j2, _ = q2.enqueue({'desc': 'scripted hallucinated', 'links': LINKS,
                            'origin': {'x': 1, 'y': 1}})
        q2.drain()
        got2 = q2.get(j2['id'])
        need(got2['state'] == 'rejected' and got2['code'] == '' and got2['attempts'] == 3,
             'a hallucinated link was not refused: %r' % (got2,))
        need(any(f.get('rid') == 'link-name-not-bound' and f['sev'] == 'ERR'
                 for f in got2['findings']),
             'the refusal lost its rule id: %r' % (got2['findings'],))
        checks += 1

        # 5. self-repair: the second prompt carries the findings of the first attempt
        prov3 = MP.ScriptedProvider([{'code': 'jump 99 always x 0\nend'},
                                     {'code': 'set n 0\nprint n\nend'}])
        q3 = jobqueue.Queue(jobqueue.Store(os.path.join(tmp, 'q3')), provider=prov3)
        j3, _ = q3.enqueue({'desc': 'scripted repair', 'links': LINKS})
        q3.drain()
        got3 = q3.get(j3['id'])
        need(got3['state'] == 'done' and got3['attempts'] == 2,
             'self-repair did not converge: %r' % (got3,))
        need(len(prov3.prompts) == 2 and 'REJECTED by the gate' in prov3.prompts[1],
             'the repair attempt was not told what was wrong')
        checks += 1

        # 6. needs_links that the processor does not have is a refusal by itself
        prov4 = MP.ScriptedProvider([{'code': 'set n 0\nend',
                                     'needs_links': ['container9']}] * 3)
        q4 = jobqueue.Queue(jobqueue.Store(os.path.join(tmp, 'q4')), provider=prov4)
        j4, _ = q4.enqueue({'desc': 'scripted needs', 'links': LINKS})
        q4.drain()
        got4 = q4.get(j4['id'])
        need(got4['state'] == 'rejected' and got4['code'] == '',
             'a reply asking for a link it does not own was accepted: %r' % (got4,))
        need(any('container9' in f.get('msg', '') for f in got4['findings']),
             'the needs_links refusal is not explained: %r' % (got4['findings'],))
        checks += 1

        # 7. the llm provider is opt-in: no endpoint and no replay is a usage error
        r = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'tools' / 'logicd.py'),
                            '--jobs-provider', 'llm', '--jobs-dir', os.path.join(tmp, 'q5')],
                           capture_output=True, text=True)
        need(r.returncode == 2 and 'llm' in (r.stdout + r.stderr),
             'llm without an endpoint did not exit 2: rc=%r %r' % (r.returncode,
                                                                  (r.stdout + r.stderr)[:200]))
        checks += 1

        # 8. a replay file round-trips through load_script
        path = os.path.join(tmp, 'replay.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'replies': [{'code': 'set n 0\nend'}]}, fh)
        prov5 = MP.load_script(path)
        out5 = prov5({'desc': 'replay', 'links': []})
        need(out5.get('code') == 'set n 0\nend', 'a replay file did not load: %r' % (out5,))
        checks += 1

        # 9. the CLI can drain a queue from a replay file -- the whole seam end to end
        qdir = os.path.join(tmp, 'q6')
        q6 = jobqueue.Queue(jobqueue.Store(qdir))
        j6, _ = q6.enqueue({'desc': 'cli replay', 'links': []})
        r6 = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'tools' / 'logicd.py'),
                             '--jobs-provider', 'llm', '--llm-replay', path,
                             '--jobs-dir', qdir, '--jobs-drain'],
                            capture_output=True, text=True, timeout=120)
        need(r6.returncode == 0, 'the CLI replay drain failed: rc=%r %r'
             % (r6.returncode, (r6.stdout + r6.stderr)[-300:]))
        with open(os.path.join(qdir, j6['id'] + '.json'), encoding='utf-8') as fh:
            drained = json.load(fh)
        need(drained['state'] == 'done' and drained['code'].startswith('set n 0'),
             'the CLI replay drain did not produce the scripted code: %r' % (drained,))
        checks += 1
        # 10. a blank model reply says why it was blank -- the player saw only 'empty reply'
        need(MP.HttpProvider('http://127.0.0.1:1/chat/completions').max_tokens
             == MP.DEFAULT_MAX_TOKENS >= 4096,
             'the provider budget is too small for a reasoning model to answer')
        stub2 = _StubEndpoint()
        try:
            stub2.blank = True
            got10 = MP.HttpProvider(stub2.chat_url, model='m-a', timeout=20)({'desc': 'blank',
                                                                             'links': []})
            msg10 = str(got10.get('error') or '')
            need('empty message' in msg10, 'a blank reply was not called blank: %r' % (msg10,))
            need('finish_reason=length' in msg10, 'a blank reply lost its diagnosis: %r' % (msg10,))
            # v0.12.6: say which budget ran out, and that this is truncation, not refusal
            need('budget=' in msg10,
                 'a blank reply did not say which budget ran out: %r' % (msg10,))
            need('ran out of room' in msg10,
                 'a truncated reply did not say it was truncated: %r' % (msg10,))
            checks += 1
        finally:
            stub2.close()

        # 11. a 404 says the url is probably the API base, not the full chat route
        stub3 = _StubEndpoint()
        try:
            stub3.strict = True
            got11 = MP.HttpProvider('http://127.0.0.1:%d/v1' % stub3.port, model='m-a',
                                    timeout=20)({'desc': 'base url', 'links': []})
            msg11 = str(got11.get('error') or '')
            need('404' in msg11, 'a 404 was not reported as a 404: %r' % (msg11,))
            need('/chat/completions' in msg11, 'a 404 lost its hint: %r' % (msg11,))
            checks += 1
        finally:
            stub3.close()

        # 12. v0.12.4: whatever the model was thinking rides along with the reply
        #     and is kept on the job, so the panel can show why it wrote that
        prov6 = MP.ScriptedProvider([{'code': 'set n 0\nend',
                                      'reasoning': 'first think, then write'}])
        q7 = jobqueue.Queue(jobqueue.Store(os.path.join(tmp, 'q7')), provider=prov6)
        j7, _ = q7.enqueue({'desc': 'scripted thinking', 'links': LINKS})
        q7.drain()
        got7 = q7.get(j7['id'])
        need(got7['state'] == 'done' and 'first think' in str(got7.get('reasoning')),
             'the thinking was not kept on the job: %r' % (got7.get('reasoning'),))
        need(got7['findings'] == [] and got7['code'].startswith('set n 0'),
             'a reply carrying reasoning broke the job: %r' % (got7,))
        checks += 1

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return checks

# ---------------------------------------------------------------- half E: v0.14.0 improve

def test_improve():
    """'Continue improving': the workspace rides on the job, the id follows the
    previous code, and the prompt shows the version that is being improved.

    Offline: normalize()/job_id()/build_prompt() directly, then one job through
    the real HTTP surface with the fake provider.
    """
    import jobqueue
    import mlogprovider as MP

    checks = 0
    prev = ('loop:\nsensor n container1 @copper\njump done lessThan n 50\n'
            'jump loop always\ndone:\nend')
    ws = {'id': 'ws1793456316430_1', 'name': '区域 (12,34)', 'notes': '省电优先',
          'prevCode': prev, 'prevJob': '3f2a1b9c',
          'prevFindings': ['line 3: [WARN] this statement can never run: end']}
    body = {'mode': 'improve', 'desc': '把铜矿传送带改成低于 50 就停', 'links': JLINKS,
            'ipt': 2, 'origin': {'x': 10, 'y': 20}, 'workspace': ws}
    title = '=== previous version (same area) ==='

    # 1. normalize accepts mode=improve and keeps every workspace field
    job = jobqueue.normalize(body)
    need(job['mode'] == 'improve', 'normalize refused mode=improve: %r' % (job.get('mode'),))
    got = job['workspace']
    need(isinstance(got, dict), 'normalize dropped the workspace: %r' % (got,))
    for k, want in (('id', 'ws1793456316430_1'), ('name', '区域 (12,34)'),
                    ('notes', '省电优先'), ('prevCode', prev), ('prevJob', '3f2a1b9c')):
        need(got.get(k) == want, 'workspace.%s came back as %r' % (k, got.get(k)))
    rich = jobqueue.normalize({'desc': 'x', 'mode': 'improve',
                               'workspace': {'id': 'ws1', 'name': 'n', 'prevCode': 'set n 1\n',
                                             'prevFindings': [{'sev': 'WARN',
                                                               'rid': 'unreachable-statement',
                                                               'msg': 'this can never run'}]}})
    line = rich['workspace']['prevFindings'][0]
    need('{' not in line and 'unreachable-statement' in line,
         'a dict finding leaked Python repr into the prompt: %r' % (line,))
    need(line.startswith('WARN unreachable-statement:'),
         'a dict finding lost its sev/rid prefix: %r' % (line,))
    need(got['prevFindings'] == ws['prevFindings'],
         'workspace.prevFindings came back as %r' % (got.get('prevFindings'),))
    checks += 1

    # 2. a 4000-character previous version is truncated to the cap, head kept
    big = '\n'.join('set v%d %d' % (i, i) for i in range(400))
    need(len(big) > 4000, 'the test code is not long enough: %d' % len(big))
    cut = jobqueue.normalize(dict(body, workspace=dict(ws, prevCode=big)))['workspace']
    need(len(cut['prevCode']) <= 4000, 'prevCode was not capped: %d'
         % len(cut['prevCode']))
    need(cut['prevCode'].startswith('set v0 0'), 'truncation did not keep the head')
    need(cut['prevCode'].endswith('... (truncated)'),
         'a truncated prevCode does not say it was cut: %r' % cut['prevCode'][-40:])
    checks += 1

    # 3. missing / junk workspace is None, never an error (an older mod keeps posting)
    need(jobqueue.normalize({'desc': 'x'})['workspace'] is None,
         'a job without a workspace grew one')
    need(jobqueue.normalize({'desc': 'x', 'workspace': 'nope'})['workspace'] is None,
         'a non-object workspace was not ignored')
    need(jobqueue.normalize({'desc': 'x', 'workspace': {'id': 7, 'prevFindings': 'x'}})
         ['workspace'] == {'id': '', 'name': '', 'notes': '', 'prevCode': '',
                           'prevJob': '', 'prevFindings': []},
         'a half-filled workspace was not defaulted')
    checks += 1

    # 4. the id follows prevCode -- but only for improve, so the second round of
    #    "continue improving" on the same sentence is a different job
    a = jobqueue.normalize(body)['id']
    b = jobqueue.normalize(dict(body, workspace=dict(ws, prevCode='end')))['id']
    need(a != b, 'two improve jobs with different prevCode share the id %r' % (a,))
    need(jobqueue.normalize(body)['id'] == a,
         'the same improve body is no longer content-addressed')
    # 'new'/'edit' ids are what they were before v0.14.0: a saved job must not
    # change identity under the mod when the sidecar is upgraded
    need(jobqueue.job_id({}, 'keep this id', 'new') == '5e64562ed52ae875',
         "v0.14.0 changed the 'new' id")
    need(jobqueue.job_id({}, 'keep this id', 'edit') == '38968079334a4679',
         "v0.14.0 changed the 'edit' id")
    checks += 1

    # 5. the prompt shows the previous version, before === task ===, within budget
    snap = {'bounds': {'x1': 4, 'y1': 14, 'x2': 16, 'y2': 26}, 'cells': 3,
            'truncated': False, 'blocks': {'container': 2},
            'map': {'name': 'groundZero'}}
    pjob = {'mode': 'improve', 'desc': body['desc'], 'links': LINKS,
            'origin': {'x': 10, 'y': 20}, 'ipt': 2, 'snapshot': snap,
            'workspace': job['workspace']}
    p = MP.build_prompt(pjob)
    need(title in p, 'the improve prompt has no previous-version section')
    need('  jump done lessThan n 50' in p, 'the previous code is not in the prompt')
    need('previous job: 3f2a1b9c' in p, 'the previous job id is not in the prompt')
    need('  - line 3: [WARN] this statement can never run: end' in p,
         'the previous findings are not in the prompt')
    need('- The player wants THIS version improved, not a rewrite from zero.' in p,
         'the prompt does not say "improve this, do not rewrite"')
    need('Do not go back to a shape that already failed.' in p,
         'the prompt does not warn about the failed shape')
    need(p.index(title) < p.index('=== task ==='),
         'the previous-version section is not before === task ===')
    seg = p[p.index(title):p.index('=== task ===')]
    need(len(seg) <= 1200, 'the section is %d characters, cap is 1200' % len(seg))
    # a 4000-character previous version must still fit in the same budget
    pb = MP.build_prompt(dict(pjob, workspace=cut))
    need(title in pb, 'a long previous version dropped the section')
    segb = pb[pb.index(title):pb.index('=== task ===')]
    need(len(segb) <= 1200, 'the section is %d characters with a 4000-character '
         'prevCode, cap is 1200' % len(segb))
    need(segb.rstrip().endswith('... (truncated)'), 'a clipped section does not say so')
    checks += 1

    # 6. and it is strictly an improve thing: 'new' never grows it, and an improve
    #    job with nothing to improve yet is a plain job
    need(title not in MP.build_prompt(dict(pjob, mode='new')),
         "a 'new' job grew the previous-version section")
    need(title not in MP.build_prompt(dict(pjob, workspace={'prevCode': ''})),
         'an empty prevCode still produced a section')
    need(title not in MP.build_prompt(dict(pjob, workspace=None)),
         'a missing workspace still produced a section')
    checks += 1

    # 7. the workspace survives the queue, _set(), the disk and GET /jobs?id=
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='logicd-improve-', dir=str(ROOT / 'out')))
    fake = tmp / 'fake.json'
    fake.write_text(json.dumps(FAKE, ensure_ascii=False), encoding='utf-8')
    qdir = tmp / 'queue'
    srv = Server(['--jobs-dir', str(qdir), '--jobs-provider', 'fake',
                  '--fake-provider', str(fake)])
    try:
        st, res = srv.post('/jobs', {'job': dict(body,
                                                 desc='把铜矿传送带改成低于 50 就停（好）')})
        need(st == 200 and res.get('ok'), 'POST /jobs (improve): %s %r' % (st, res))
        jid = res['id']
        st, one = srv.get('/jobs?id=' + jid)
        need(st == 200 and one.get('ok'), 'GET /jobs?id=%s: %s %r' % (jid, st, one))
        need((one['job'].get('workspace') or {}).get('id') == 'ws1793456316430_1',
             'GET /jobs?id= lost workspace.id: %r' % (one['job'].get('workspace'),))
        need((one['job'].get('workspace') or {}).get('prevCode') == prev,
             'GET /jobs?id= lost workspace.prevCode')
        done = wait_job(srv, jid, ('done',))
        need((done.get('workspace') or {}).get('id') == 'ws1793456316430_1',
             'a finished job lost its workspace (a _set() update dropped it)')
        on_disk = json.loads((qdir / (jid + '.json')).read_text(encoding='utf-8'))
        need((on_disk.get('workspace') or {}).get('id') == 'ws1793456316430_1',
             'the job file on disk has no workspace: %r' % (sorted(on_disk),))
        checks += 1
    finally:
        srv.close()
        shutil.rmtree(tmp, ignore_errors=True)

    # 8. v0.14.2: prevCode is signed now, so an improve reply that echoes the
    #    previous version comes back with the OLD token still in it -- re-signing
    #    has to replace it (force=True) instead of raising SignError, which would
    #    turn every "keep it as it is" answer into a failed job
    import aigenerated
    prev_signed, prev_sig = aigenerated.sign_text(prev, jobqueue.SIGN_AUTHOR)
    need(aigenerated.verify_text(prev_signed) == [],
         'the previous version is not signed: %r' % (aigenerated.verify_text(prev_signed),))
    echo_tmp = pathlib.Path(tempfile.mkdtemp(prefix='logicd-echo-', dir=str(ROOT / 'out')))
    try:
        echo_job = jobqueue.normalize(dict(body, workspace=dict(ws, prevCode=prev_signed)))
        q = jobqueue.Queue(jobqueue.Store(str(echo_tmp / 'queue')),
                           provider=jobqueue.FakeProvider(
                               {'把铜': {'code': prev_signed,
                                        'explanation': 'echoed the previous version'}}))
        q.enqueue(echo_job)
        q.drain()
        echoed = q.get(echo_job['id'])
        need(echoed['state'] == 'done',
             'a reply that echoes the signed previous version did not land: %r'
             % ({k: echoed.get(k) for k in ('state', 'findings', 'explanation')},))
        need(not any(f.get('rid') == 'sign-error' for f in echoed['findings']),
             're-signing the echoed version reported a sign-error: %r'
             % (echoed['findings'],))
        # v0.16.0（B3）：签名 payload 里现在带作业上下文（meta v2），所以「同一个程序」
        # 的比较要把签名行里的 payload 摘掉再看：id 与语句必须一模一样。
        def _no_payload(text):
            return re.sub(r'(aigenerated:[^:\"]+:[0-9a-f]{12}):[^\"]*', r'\1', text)

        need(echoed.get('sig') == prev_sig and _no_payload(echoed['code']) == _no_payload(prev_signed),
             're-signing the echoed version did not reproduce the same program: '
             'sig %r -> %r' % (prev_sig, echoed.get('sig')))
        _m = re.search(r'aigenerated:[^:\"]+:[0-9a-f]{12}:([^\"]*)', echoed['code'])
        need(_m is not None, 'the echoed program lost its signature payload')
        _meta = aigenerated.decode_meta(_m.group(1))
        need(isinstance(_meta, dict) and _meta.get('ws') == ws['id'],
             'the signed program does not carry its workspace context: %r' % (_meta,))
        checks += 1
    finally:
        shutil.rmtree(echo_tmp, ignore_errors=True)

    return checks

# ---------------------------------------------------------------- half A3: model tier

#: what the stub puts in reasoning_content -- the panel shows this text (v0.12.4:
#: a reasoning model thinks before it writes, so a refusal explains itself only if
#: the thinking comes back with the job)
STUB_THINK = 'think first: the player asked for one printed number'


class _StubEndpoint:
    """A tiny OpenAI-ish endpoint: /v1/models lists two ids, chat answers a clean program."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.seen = []
        self.posts = []
        self.truncate = 0          # answer this many replies with finish_reason=length
        self.blank = False
        self.strict = False
        self.status = 0            # answer every POST with this HTTP status (v0.15.0)
        self.empty = False         # answer with an empty message, finish_reason=stop
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append((self.path, self.headers.get('Authorization')))
                if self.path == '/v1/models':
                    body = json.dumps({'data': [{'id': 'm-b'}, {'id': 'm-a'}]}).encode()
                    self.send_response(200)
                else:
                    body = b'{"error": "nope"}'
                    self.send_response(401)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(n) if n else b'{}'
                outer.seen.append((self.path, self.headers.get('Authorization'), len(raw)))
                if outer.strict and self.path != '/v1/chat/completions':
                    body = b'{"error": "no such route"}'
                    self.send_response(404)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                try:
                    outer.posts.append(json.loads(raw.decode('utf-8')))
                except ValueError:
                    outer.posts.append({})
                if outer.status:
                    body = json.dumps({'error': 'stub refused'}).encode()
                    self.send_response(outer.status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                content = json.dumps({'code': 'set n 0' + chr(10) + 'end'})
                cut = False
                if outer.truncate > 0:
                    outer.truncate -= 1
                    cut = True
                if outer.empty and not cut:
                    payload = {'choices': [{'message': {'content': ''},
                                            'finish_reason': 'stop'}],
                               'usage': {'prompt_tokens': 11, 'completion_tokens': 0}}
                elif outer.blank or cut:
                    content = ''
                    payload = {'choices': [{'message': {'content': content},
                                            'finish_reason': 'length'}],
                               'usage': {'prompt_tokens': 11, 'completion_tokens': 1600}}
                else:
                    payload = {'model': 'm-a',
                               'choices': [{'message': {'content': content,
                                                        'reasoning_content': STUB_THINK},
                                            'finish_reason': 'stop'}],
                               'usage': {'prompt_tokens': 11, 'completion_tokens': 40,
                                         'total_tokens': 51}}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def chat_url(self):
        return 'http://127.0.0.1:%d/v1/chat/completions' % self.port

    @property
    def models_url(self):
        return 'http://127.0.0.1:%d/v1/models' % self.port

    def close(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass


def test_llm():
    """The settings page fills url / key / model; the sidecar switches tier at runtime."""
    import mlogprovider as MP
    checks = 0
    stub = _StubEndpoint()
    # a private queue dir: job ids are sha1(desc+snapshot+mode), so a shared out/jobs
    # would hand this run a *previous* done job and the model would never be called
    tmp = tempfile.mkdtemp(prefix='logicd-llm-')
    srv = Server(['--jobs-dir', os.path.join(tmp, 'q')])
    try:
        # 1. boot state is「未配置模型」(kind none), and the key is never echoed
        code, doc = srv.get('/llm')
        need(code == 200 and doc.get('ok') is True and doc.get('kind') == 'none',
             'GET /llm did not report the unconfigured state: %r' % (doc,))
        need('key' not in doc and doc.get('key_set') is False,
             'GET /llm leaked a key field: %r' % (doc,))
        checks += 1

        # 2. /models derives the list url from the chat url and forwards the key
        code, doc = srv.post('/models', {'url': stub.chat_url, 'key': 'sk-stub-123456'})
        need(code == 200 and doc.get('models') == ['m-a', 'm-b'],
             'POST /models did not list the stub models: %r' % (doc,))
        need(doc.get('url') == stub.models_url,
             'POST /models derived the wrong url: %r' % (doc,))
        need(stub.seen and stub.seen[0][1] == 'Bearer sk-stub-123456',
             'POST /models did not forward the key: %r' % (stub.seen,))
        checks += 1

        # 3. switching to the model tier keeps the secret, reports only its tail
        code, doc = srv.post('/llm', {'url': stub.chat_url, 'model': 'm-a',
                                      'key': 'sk-secret-abcd', 'timeout': 45})
        need(code == 200 and doc.get('kind') == 'llm' and doc.get('model') == 'm-a',
             'POST /llm did not switch to the model tier: %r' % (doc,))
        need(doc.get('key_set') is True and str(doc.get('key_hint', '')).endswith('abcd')
             and 'sk-secret' not in json.dumps(doc),
             'POST /llm echoed the key: %r' % (doc,))
        checks += 1

        # 4. the state survives a later GET (the panel re-reads it)
        code, doc = srv.get('/llm')
        need(doc.get('kind') == 'llm' and doc.get('url') == stub.chat_url
             and doc.get('timeout') == 45,
             'GET /llm lost the model tier state: %r' % (doc,))
        checks += 1

        # 5. a job now really goes through the model: the stub answers, the gate accepts
        code, doc = srv.post('/jobs', {'job': {'desc': 'llm tier probe %d' % stub.port,
                                               'links': [], 'ipt': 2}})
        need(code == 200 and doc.get('ok') is True, 'the job was refused: %r' % (doc,))
        got = wait_job(srv, doc['id'], want=('done',))
        need(str(got.get('code') or '').startswith('set n 0') and got.get('bytes_b64'),
             'the model tier job did not reach done with code: %r' % (got,))
        need(any(s[0] == '/v1/chat/completions' for s in stub.seen),
             'the queue never called the model endpoint: %r' % (stub.seen,))
        checks += 1

        # 6. junk urls and the way back are both explicit
        code, doc = srv.post('/llm', {'url': 'ftp://nope'})
        need(code == 400 and 'http' in str(doc.get('error', '')).lower(),
             'POST /llm accepted a non-http url: %r %r' % (code, doc))
        code, doc = srv.post('/llm', {'kind': 'template'})
        need(code == 200 and doc.get('kind') == 'none',
             'POST /llm kind=template did not go back to unconfigured: %r' % (doc,))
        checks += 1
        # 7. a url that stops at the API base is completed before the provider sees it
        base = 'http://127.0.0.1:%d/v1' % stub.port
        code, doc = srv.post('/llm', {'url': base, 'model': 'm-a'})
        need(code == 200 and doc.get('url') == stub.chat_url,
             'POST /llm did not complete a base url: %r' % (doc,))
        code, doc = srv.post('/llm', {'url': stub.chat_url, 'model': 'm-a'})
        need(code == 200 and doc.get('url') == stub.chat_url,
             'POST /llm rewrote a full chat url: %r' % (doc,))
        checks += 1

        # 8. /llm/probe answers with the raw shape of one tiny call
        code, doc = srv.post('/llm/probe', {'url': base, 'model': 'm-a'})
        need(code == 200 and doc.get('ok') is True and doc.get('status') == 200,
             'POST /llm/probe did not reach the stub: %r' % (doc,))
        need(doc.get('url') == stub.chat_url,
             'POST /llm/probe reported the wrong url: %r' % (doc,))
        need('set n 0' in str(doc.get('content') or ''),
             'POST /llm/probe lost the reply text: %r' % (doc,))
        checks += 1

        # 9. v0.12.3: a reasoning model spends the budget on reasoning_content before it
        #    writes anything, so a tight max_tokens came back as an empty message with
        #    finish_reason=length (measured on the real player job: 502 prompt tokens,
        #    1600 completion tokens, content empty).  The provider must widen the budget
        #    once and keep the job alive instead of failing it.
        code, doc = srv.post('/llm', {'url': stub.chat_url, 'model': 'm-a',
                                      'max_tokens': 800, 'timeout': 20})
        need(code == 200 and doc.get('max_tokens') == 800,
             'POST /llm did not take a max_tokens budget: %r' % (doc,))
        stub.posts.clear()
        stub.truncate = 1
        code, doc = srv.post('/jobs', {'job': {'desc': 'budget probe %d' % stub.port,
                                               'links': [], 'ipt': 2}})
        need(code == 200 and doc.get('ok') is True,
             'the budget job was refused: %r' % (doc,))
        got = wait_job(srv, doc['id'], want=('done',))
        need(str(got.get('code') or '').startswith('set n 0'),
             'a truncated first reply was not retried into a done job: %r' % (got,))
        budgets = [q.get('max_tokens') for q in stub.posts]
        need(budgets[:2] == [800, 3200],
             'the retry did not widen the budget: %r' % (budgets,))
        checks += 1

        # 10. when even the widened budget is not enough, the error names the knob
        stub.truncate = 99
        stub.posts.clear()
        never = MP.HttpProvider(stub.chat_url, model='m-a', timeout=20, max_tokens=500)
        out = never({'desc': 'never answers %d' % stub.port, 'links': [], 'ipt': 2})
        stub.truncate = 0
        msg = str(out.get('error') or '')
        need('finish_reason=length' in msg and '--llm-max-tokens' in msg,
             'a permanently truncated reply did not name the budget knob: %r' % (msg,))
        climbed = [q.get('max_tokens') for q in stub.posts]
        need(climbed[:2] == [500, 2000], 'the retry ladder did not climb: %r' % (climbed,))
        checks += 1

        # 12. v0.12.4: the panel shows what the model was thinking, so the
        #     thinking has to come back over HTTP and land on the job
        code, doc = srv.post('/jobs', {'job': {'desc': 'thinking probe %d' % stub.port,
                                               'links': [], 'ipt': 2}})
        need(code == 200 and doc.get('ok') is True,
             'the thinking job was refused: %r' % (doc,))
        got = wait_job(srv, doc['id'], want=('done',))
        need(STUB_THINK in str(got.get('reasoning') or ''),
             'the model thinking did not reach the job: %r' % (got.get('reasoning'),))
        need(len(str(got.get('reasoning') or '')) <= MP.REASONING_MAX,
             'the stored thinking is longer than the cap')
        checks += 1

        # 11. GET /llm reports the budget; an absurd one is refused
        code, doc = srv.get('/llm')
        need(doc.get('max_tokens') == 800,
             'GET /llm does not report max_tokens: %r' % (doc,))
        code, doc = srv.post('/llm', {'url': stub.chat_url, 'max_tokens': 10})
        need(code == 400, 'POST /llm took an absurd max_tokens: %r %r' % (code, doc))
        checks += 1

    finally:
        srv.close()
        stub.close()
    return checks


# ---------------------------------------------------------------- half A4: chats (v0.20.0)

class _ChatStub:
    """An OpenAI endpoint that asks for one mlog_check and then answers.

    Which reply it sends is decided by the conversation, not by a request counter:
    a prompt that already carries a tool result gets the final sentence. That also
    makes it usable for the continuation check, where the same endpoint must answer
    a second turn of the same chat.
    """

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.posts = []
        self.delay = 0.6          # long enough that the next POST sees state=running
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(n) if n else b'{}'
                try:
                    body = json.loads(raw.decode('utf-8'))
                except ValueError:
                    body = {}
                outer.posts.append(body)
                if outer.delay:
                    time.sleep(outer.delay)
                messages = body.get('messages') or []
                if any(m.get('role') == 'tool' for m in messages):
                    message = {'role': 'assistant', 'content': '检查完了：没有问题。'}
                else:
                    message = {'role': 'assistant', 'content': '',
                               'tool_calls': [{'id': 'call_abc', 'type': 'function',
                                               'function': {
                                                   'name': 'mlog_check',
                                                   'arguments': '{"code":"jump 0 always"}'}}]}
                payload = {'model': 'stub',
                           'choices': [{'message': message, 'finish_reason': 'stop'}]}
                data = json.dumps(payload).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def chat_url(self):
        return 'http://127.0.0.1:%d/v1/chat/completions' % self.port

    def close(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass


def test_chats():
    """v0.20.0: POST /chat answers at once, and a tool round trip shows up in the chat.

    The module polls GET /chat every 900 ms and renders the messages array, so this
    pins the OpenAI shape, the brief list (counts, no payload), the busy refusal, the
    continuation context and DELETE. The stub lives on 127.0.0.1 on purpose: the
    sidecar must bypass the machine proxy for loopback or none of this connects.
    """
    checks = 0
    stub = _ChatStub()
    tmp = tempfile.mkdtemp(prefix='logicd-chats-')
    srv = Server(['--jobs-dir', os.path.join(tmp, 'jobs'),
                  '--chats-dir', os.path.join(tmp, 'chats')])

    def wait_idle(cid, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            code, doc = srv.get('/chat?id=' + cid)
            need(code == 200 and doc.get('ok') is True,
                 'GET /chat failed: %r %r' % (code, doc))
            chat = doc['chat']
            if chat.get('state') != 'running':
                return chat
            time.sleep(0.15)
        raise Fail('chat %r never left running' % cid)

    try:
        # 1. h) before a model is configured the player gets a clear 400
        code, doc = srv.post('/chat', {'text': '你好'})
        need(code == 400, 'POST /chat without a model answered %r' % (code,))
        need('模型' in str(doc.get('error') or ''),
             'the refusal does not mention the model: %r' % (doc,))
        checks += 1

        # 2. b) point the sidecar at the stub
        code, doc = srv.post('/llm', {'kind': 'llm', 'url': stub.chat_url, 'model': 'stub'})
        need(code == 200 and doc.get('kind') == 'llm',
             'POST /llm refused the stub: %r %r' % (code, doc))
        checks += 1

        # 3. c) bad text is refused; a good line answers 'running' immediately
        code, doc = srv.post('/chat', {'text': '   '})
        need(code == 400, 'blank text answered %r' % (code,))
        code, doc = srv.post('/chat', {'text': 'x' * 8001})
        need(code == 400, 'oversized text answered %r' % (code,))
        code, doc = srv.post('/chat', {'id': 'nosuch1', 'text': '你好'})
        need(code == 400, 'an unknown id answered %r' % (code,))
        code, doc = srv.post('/chat', {'text': '帮我看看 jump 0 always 有没有问题'})
        need(code == 200 and doc.get('ok') is True,
             'POST /chat was refused: %r %r' % (code, doc))
        need(doc.get('state') == 'running',
             'POST /chat did not answer running: %r' % (doc,))
        cid = doc.get('id')
        need(isinstance(cid, str) and cid, 'POST /chat returned no id: %r' % (doc,))
        need(doc.get('title'), 'POST /chat returned no title: %r' % (doc,))
        checks += 1

        # 4. a second line while the first is still running is 'busy'
        code, doc = srv.post('/chat', {'id': cid, 'text': '再问一句'})
        need(code == 400 and doc.get('error') == 'busy',
             'a second line while running answered %r %r' % (code, doc))
        checks += 1

        chat = wait_idle(cid)
        need(chat.get('state') == 'idle' and not chat.get('error'),
             'the run ended badly: %r' % (chat.get('error'),))
        roles = [m.get('role') for m in chat.get('messages') or []]
        need(roles[:4] == ['user', 'assistant', 'tool', 'assistant'],
             'the transcript is not user/assistant/tool/assistant: %r' % (roles,))
        called = (chat['messages'][1].get('tool_calls') or [{}])[0]
        need(called.get('function', {}).get('name') == 'mlog_check',
             'the assistant turn did not keep its tool_calls: %r' % (called,))
        need(chat['messages'][2].get('tool_call_id') == called.get('id'),
             'the tool reply does not point at its call: %r' % (chat['messages'][2],))
        need('语句数' in str(chat['messages'][2].get('content') or ''),
             'the tool output does not look like mlog_check: %r' % (chat['messages'][2],))
        need(chat.get('tools_supported') is True,
             'tools_supported is %r' % (chat.get('tools_supported'),))
        need(chat.get('reply') == '检查完了：没有问题。', 'reply: %r' % (chat.get('reply'),))
        last = chat.get('last') or {}
        tools = last.get('tools') or []
        need(len(tools) == 1 and tools[0].get('name') == 'mlog_check',
             'last.tools did not record the call: %r' % (tools,))
        need(tools[0].get('ok') is True and isinstance(tools[0].get('ms'), int),
             'last.tools row is incomplete: %r' % (tools[0],))
        need('\n' not in str(tools[0].get('summary') or ''),
             'the tool summary is not one line: %r' % (tools[0],))
        checks += 1

        # 5. the second model request carried the tool result back
        need(len(stub.posts) >= 2, 'the model was called %d time(s)' % len(stub.posts))
        second = stub.posts[1].get('messages') or []
        need(any(m.get('role') == 'tool' for m in second),
             'the tool result was not sent back: %r' % ([m.get('role') for m in second],))
        checks += 1

        # 6. e) GET /chats is the list window: counts only, no payload
        code, doc = srv.get('/chats?limit=50')
        need(code == 200 and doc.get('ok') is True and doc.get('count') >= 1,
             'GET /chats: %r %r' % (code, doc))
        brief = [c for c in doc.get('chats') or [] if c.get('id') == cid]
        need(len(brief) == 1, 'the chat is missing from GET /chats: %r' % (doc,))
        b = brief[0]
        need(b.get('msgs') == len(chat['messages'])
             and b.get('messages') == len(chat['messages']),
             'brief message count: %r' % (b,))
        need('last' not in b, 'the brief carries the full last block: %r' % (b,))
        need(b.get('state') == 'idle' and b.get('tools') == 1,
             'brief state/tools: %r' % (b,))
        checks += 1

        # 7. f) continuing the chat keeps the earlier turn in the prompt
        before = len(stub.posts)
        code, doc = srv.post('/chat', {'id': cid, 'text': '那再看看 set n 0 呢'})
        need(code == 200 and doc.get('id') == cid,
             'the continuation was refused: %r %r' % (code, doc))
        chat = wait_idle(cid)
        need(chat.get('state') == 'idle', 'the continuation ended in %r' % (chat.get('state'),))
        need(len(chat['messages']) > 4, 'the continuation appended nothing')
        sent = [m for q in stub.posts[before:] for m in (q.get('messages') or [])]
        need(any(m.get('role') == 'assistant' and '检查完了' in str(m.get('content') or '')
                 for m in sent), 'the earlier reply was not in the continuation prompt')
        checks += 1

        # 8. g) delete is final; a missing id is 400, an unknown one 404
        code, doc = srv.delete('/chat?id=' + cid)
        need(code == 200 and doc.get('deleted') is True, 'DELETE /chat: %r %r' % (code, doc))
        code, doc = srv.get('/chat?id=' + cid)
        need(code == 404, 'a deleted chat still answers %r' % (code,))
        code, doc = srv.get('/chat')
        need(code == 400, 'GET /chat without ?id= answered %r' % (code,))
        code, doc = srv.delete('/chat?id=' + cid)
        need(code == 404, 'deleting a missing chat answered %r' % (code,))
        checks += 1
    finally:
        srv.close()
        stub.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return checks


def test_diag():
    """v0.15.0: every refusal carries a class, every job keeps the evidence.

    The old queue saw one string for every failure and retried all of them the same
    way, so a 401 burned three model calls and 'refused after 3 attempt(s)' could not
    be explained afterwards.  These checks pin the class, the retry policy that goes
    with it, the attempt log on the job, and the worker pool.
    """
    import jobqueue
    import mlogprovider as MP
    checks = 0
    stub = _StubEndpoint()
    tmp = tempfile.mkdtemp(prefix='logicd-diag-')
    try:
        # 1. a 401 is answered once: the class is http-status and it is fatal
        stub.posts.clear()
        stub.status = 401
        bad = MP.HttpProvider(stub.chat_url, model='m-a', timeout=20)
        out = bad({'desc': 'auth wall %d' % stub.port, 'links': [], 'ipt': 2})
        stub.status = 0
        need(out.get('kind') == 'http-status',
             'a 401 was not classified as http-status: %r' % (out,))
        need(out.get('fatal') is True, 'a 401 was not marked fatal: %r' % (out,))
        need(len(stub.posts) == 1,
             'a 401 was retried %d time(s) instead of failing once' % len(stub.posts))
        need('HTTP 401' in str(out.get('error') or ''),
             'a 401 lost its status line: %r' % (out.get('error'),))
        checks += 1

        # 2. an empty message is transient: it is retried, and each try is counted
        stub.posts.clear()
        stub.empty = True
        emp = MP.HttpProvider(stub.chat_url, model='m-a', timeout=20)
        out = emp({'desc': 'empty wall %d' % stub.port, 'links': [], 'ipt': 2})
        stub.empty = False
        need(out.get('kind') == 'empty',
             'an empty reply was not classified as empty: %r' % (out,))
        need(len(stub.posts) == MP.RETRY_TOTAL,
             'an empty reply was retried %d time(s), want %d'
             % (len(stub.posts), MP.RETRY_TOTAL))
        need((out.get('diag') or {}).get('attempt') == MP.RETRY_TOTAL,
             'the diagnostics did not count the attempts: %r' % (out.get('diag'),))
        checks += 1

        # 3. a good reply comes back with what the call cost
        ok = MP.HttpProvider(stub.chat_url, model='m-a', timeout=20)
        out = ok({'desc': 'evidence probe %d' % stub.port, 'links': [], 'ipt': 2})
        need(not out.get('error'), 'the evidence reply failed: %r' % (out,))
        diag = out.get('diag') or {}
        need(diag.get('prompt_tokens') == 11 and diag.get('completion_tokens') == 40,
             'the reply lost its token counts: %r' % (diag,))
        need(diag.get('finish_reason') == 'stop',
             'the reply lost its finish_reason: %r' % (diag,))
        need(diag.get('provider') == 'http' and diag.get('budget'),
             'the diagnostics did not identify the call: %r' % (diag,))
        checks += 1

        # 4. the queue keeps the log and stops asking an exhausted wall
        calls = []

        def empties(job, hint=None):
            calls.append(1)
            return {'error': 'model endpoint answered with an empty message',
                    'kind': 'empty',
                    'diag': {'provider': 'stub', 'model': 'm-a', 'attempt': len(calls)}}

        q = jobqueue.Queue(jobqueue.Store(os.path.join(tmp, 'q')), provider=empties,
                           workers=1)
        probe, _ = q.enqueue({'desc': 'queue empty probe %d' % stub.port, 'links': [],
                              'ipt': 2})
        jid = probe['id']
        q.drain(timeout=20)
        job = q.get(jid)
        need(job.get('state') == 'failed',
             'a job whose endpoint kept answering empty did not fail: %r' % (job,))
        need(len(calls) == 2,
             'the queue re-asked an exhausted wall %d time(s), want 2' % len(calls))
        need('empty message' in str(job.get('explanation') or ''),
             'the failure did not name the wall: %r' % (job.get('explanation'),))
        log = job.get('attempt_log') or []
        need(len(log) == 2 and log[-1].get('kind') == 'empty'
             and isinstance(log[-1].get('ms'), int),
             'the job did not keep a per-attempt log: %r' % (log,))
        need(job.get('model') == 'm-a' and job.get('provider') == 'stub',
             'the job did not keep which model/provider answered: %r' % (job,))
        checks += 1

        # 5. three jobs on three workers overlap instead of queueing up
        def slow(job, hint=None):
            time.sleep(0.4)
            return {'code': 'set n 0' + chr(10) + 'end'}

        q3 = jobqueue.Queue(jobqueue.Store(os.path.join(tmp, 'q3')), provider=slow,
                            workers=3)
        ids = []
        for i in range(3):
            j3, _ = q3.enqueue({'desc': 'parallel probe %d %d' % (stub.port, i),
                                'links': [], 'ipt': 2})
            ids.append(j3['id'])
        need(q3.workers == 3, 'the queue did not take the worker count: %r' % (q3.workers,))
        started = time.time()
        q3.start()
        try:
            deadline = started + 10
            while time.time() < deadline:
                states = [q3.get(i).get('state') for i in ids]
                if all(st in JTERMINAL for st in states):
                    break
                time.sleep(0.02)
        finally:
            q3.stop()
        elapsed = time.time() - started
        need(all(q3.get(i).get('state') == 'done' for i in ids),
             'the parallel jobs did not all finish: %r'
             % ([q3.get(i).get('state') for i in ids],))
        need(elapsed < 1.0,
             'three 0.4 s jobs took %.2f s with three workers (serial is 1.2 s)'
             % elapsed)
        checks += 1

        # ---- v0.15.1（A4）：一批里的第几台要进作业，也要进提示词。
        # 批量投递时每台拿到同一句话，模型不知道自己是第几台 —— 它照抄隔壁那台的
        # 链接名，预检就报 link-name-not-bound。
        one = jobqueue.normalize({'desc': 'batch one', 'snapshot': {'map': 'm'},
                                   'batch': {'i': 2, 'n': 3}})
        need(one.get('batch') == {'i': 2, 'n': 3},
             'the batch marker did not survive normalize: %r' % (one.get('batch'),))
        odd = jobqueue.normalize({'desc': 'batch odd', 'snapshot': {'map': 'm'},
                                  'batch': {'i': 9, 'n': 2}})
        need(odd.get('batch') == {'i': 2, 'n': 2},
             'batch.i must be clamped into 1..n, got %r' % (odd.get('batch'),))
        try:
            jobqueue.normalize({'desc': 'x', 'snapshot': {'map': 'm'}, 'batch': 3})
            need(False, 'a scalar batch was accepted')
        except ValueError:
            pass
        prompt = MP.build_prompt({'desc': 'batch one', 'mode': 'new', 'links': None,
                                   'ipt': 2, 'origin': {'x': 1, 'y': 2},
                                   'batch': {'i': 2, 'n': 3}})
        need('the same sentence was sent to 3 processors; this is #2' in prompt,
             'the prompt does not tell the model which processor of the batch it is')
        need('each processor has its OWN link table' in prompt,
             'the prompt does not warn that link tables differ inside a batch')
        checks += 1

        # ---- v0.15.2（bug 2）：样板库。原来 prompt 里只有一个 label-loop 骨架，
        # 模型没有别的形状可抄。现在给 8 个都过静态检查的样板，按需求关键词挑 3 个。
        import mlogcheck
        need(len(MP.EXAMPLES) >= 8, 'the sample library shrank to %d' % len(MP.EXAMPLES))
        for ex in MP.EXAMPLES:
            found = mlogcheck.analyse(ex['code'].split(chr(10)), None, 2)
            need(not found, 'sample %r does not pass the analyzer: %s'
                 % (ex['title'], [(f.sev, f.rid, f.line) for f in found]))
            need(len(ex['keys']) >= 4, 'sample %r has no keyword list' % (ex['title'],))
        picked = MP._examples_for('给炮塔加上自动射击敌人', 3)
        need(1 <= len(picked) <= 3, 'the picker returned %d samples' % len(picked))
        need('turret' in picked[0]['title'],
             'a turret task picked %r first' % (picked[0]['title'],))
        top = MP.build_prompt({'desc': '给炮塔加上自动射击敌人', 'links': None, 'ipt': 2})
        need('turret -- radar for the nearest enemy' in top,
             'the chosen sample never reached the prompt')
        need(top.count('  # ') <= 3, 'the prompt carries %d samples' % top.count('  # '))
        checks += 1
    finally:
        stub.close()
    return checks


def test_life():
    """v0.15.0: the sidecar can say who it is, take a heartbeat, and end itself.

    C1 was a real session: 8765 still held a 0.12.4 python from the previous run, so the
    module saw 200 from /health and believed its own spawn had failed.  C2 was that same
    process surviving the game.  These checks pin the answers /health now carries, the
    two new routes, and --idle-exit.
    """
    import re
    import shutil
    checks = 0
    tmp = tempfile.mkdtemp(prefix='logicd-life-')
    procs = []

    def spawn(*extra):
        p = subprocess.Popen([sys.executable, str(HERE.parent.parent / 'tools' / 'logicd.py'),
                              '--port', '0', '--jobs-provider', 'none',
                              '--jobs-dir', os.path.join(tmp, 'jobs')] + list(extra),
                             cwd=str(HERE.parent.parent),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding='utf-8', errors='replace')
        procs.append(p)
        line = p.stdout.readline()
        m = re.search(r'listening on http://[^:]+:(\d+)', line or '')
        need(m is not None, 'no ready line from the sidecar: %r' % (line,))
        return p, 'http://127.0.0.1:%s' % m.group(1)

    def get(url, path):
        with urllib.request.urlopen(url + path, timeout=15) as r:
            return json.loads(r.read().decode('utf-8'))

    def post(url, path, body=None):
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(url + path, data=data, method='POST')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode('utf-8'))

    def raw_post(url, path, raw):
        req = urllib.request.Request(url + path, data=raw, method='POST')
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode('utf-8', 'replace')

    def wait_exit(p, timeout=20):
        try:
            p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            need(False, 'the sidecar did not exit within %s s' % timeout)
        return p.returncode

    try:
        # 1. identity: the module needs a pid/exe to tell 'mine' from 'some old build'
        p1, u1 = spawn('--idle-exit', '30')
        h = get(u1, '/health')
        need(h.get('ok') is True and h.get('protocol') == 1,
             '/health lost its ok/protocol: %r' % (h,))
        need(h.get('version'), '/health does not name the build: %r' % (h,))
        need(h.get('pid') == p1.pid,
             '/health pid %r is not the process it came from (%r)' % (h.get('pid'), p1.pid))
        need(str(h.get('exe') or '').lower().endswith('logicd.py'),
             '/health does not say where it runs from: %r' % (h.get('exe'),))
        need(int(h.get('port') or 0) == int(u1.rsplit(':', 1)[1]),
             '/health port %r does not match %r' % (h.get('port'), u1))
        need(post(u1, '/ping').get('ok') is True, 'POST /ping did not answer')
        # v0.15.3 真机根因：模块把 JS 对象 {} 直接当请求体发，socket 拼出来是
        # "[object Object]" —— /ping 必须先解析 JSON，所以回 400，模块把这一次 400
        # 当成「后端没了」，于是停发心跳、45 秒后 sidecar 自退（表现为「已就绪却投不出去」）。
        code_bad, _ = raw_post(u1, '/ping', b'[object Object]')
        need(code_bad == 400,
             'a non-JSON heartbeat body answered %r, want 400 (the module must send "{}")'
             % (code_bad,))
        code_ok, _ = raw_post(u1, '/ping', b'{}')
        need(code_ok == 200, 'a JSON "{}" heartbeat answered %r, want 200' % (code_ok,))
        need(post(u1, '/shutdown').get('ok') is True, 'POST /shutdown did not answer')
        need(wait_exit(p1) == 0, 'a shutdown sidecar did not exit cleanly')
        checks += 1

        # 2. --idle-exit: nobody pings -> it goes away (this is what stops C2)
        t0 = time.time()
        p2, u2 = spawn('--idle-exit', '2')
        get(u2, '/health')          # one heartbeat, then silence
        rc = wait_exit(p2, 30)
        took = time.time() - t0
        need(rc == 0, 'the idle sidecar exited with %r' % (rc,))
        need(took < 25, 'the idle sidecar took %.1f s to notice' % took)
        checks += 1

        # 3. pings keep it alive: a game that is open must not lose its backend
        p3, u3 = spawn('--idle-exit', '2')
        for _ in range(4):
            time.sleep(0.8)
            need(post(u3, '/ping').get('ok') is True, 'POST /ping failed mid-session')
        need(p3.poll() is None, 'the sidecar exited while it was being pinged')
        post(u3, '/shutdown')
        wait_exit(p3)
        checks += 1
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    return checks


def main(argv=None):
    ap = argparse.ArgumentParser(description='logicd contract tests')
    ap.add_argument('--no-sim', action='store_true', help='protocol half only (no JRE needed)')
    a = ap.parse_args(argv)
    try:
        cap, n = test_protocol()
        print('protocol: %d check(s) passed' % n)
    except Fail as exc:
        print('FAIL protocol: %s' % exc)
        return 1
    except Exception as exc:
        print('FAIL protocol: %s: %s' % (type(exc).__name__, exc))
        return 1
    try:
        n = test_jobs()
        print('jobs: %d check(s) passed' % n)
    except Fail as exc:
        print('FAIL jobs: %s' % exc)
        return 1
    except Exception as exc:
        print('FAIL jobs: %s: %s' % (type(exc).__name__, exc))
        return 1
    try:
        n = test_provider()
        print('provider: %d check(s) passed' % n)
    except Fail as exc:
        print('FAIL provider: %s' % exc)
        return 1
    except Exception as exc:
        print('FAIL provider: %s: %s' % (type(exc).__name__, exc))
        return 1
    try:
        n = test_improve()
        print('improve: %d check(s) passed' % n)
    except Fail as exc:
        print('FAIL improve: %s' % exc)
        return 1
    except Exception as exc:
        print('FAIL improve: %s: %s' % (type(exc).__name__, exc))
        return 1
    try:
        n = test_llm()
        print('llm: %d check(s) passed' % n)
    except Fail as exc:
        print('FAIL llm: %s' % exc)
        return 1
    except Exception as exc:
        print('FAIL llm: %s: %s' % (type(exc).__name__, exc))
        return 1
    try:
        n = test_chats()
        print('chats: %d check(s) passed' % n)
    except Fail as exc:
        print('FAIL chats: %s' % exc)
        return 1
    except Exception as exc:
        print('FAIL chats: %s: %s' % (type(exc).__name__, exc))
        return 1
    try:
        n = test_diag()
        print('diag: %d check(s) passed' % n)
    except Fail as exc:
        print('FAIL diag: %s' % exc)
        return 1
    except Exception as exc:
        print('FAIL diag: %s: %s' % (type(exc).__name__, exc))
        return 1
    try:
        n = test_life()
        print('life: %d check(s) passed' % n)
    except Fail as exc:
        print('FAIL life: %s' % exc)
        return 1
    except Exception as exc:
        print('FAIL life: %s: %s' % (type(exc).__name__, exc))
        return 1
    if a.no_sim:
        print('logicd OK (protocol only)')
        return 0
    try:
        n, state = test_write_path(cap)
    except Fail as exc:
        print('FAIL write path: %s' % exc)
        return 1
    except Exception as exc:
        print('FAIL write path: %s: %s' % (type(exc).__name__, exc))
        return 1
    if state == 'skip':
        print('logicd OK (protocol only, write path skipped)')
        return 3
    print('write path: %d template(s) accepted by the game' % n)
    print('logicd OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
