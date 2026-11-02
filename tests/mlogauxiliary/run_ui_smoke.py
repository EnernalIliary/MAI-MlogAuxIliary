"""Smoke the whole main.js init path in the REAL game engine (v160.7 Rhino).

The settings UI cannot be built headless (Vars.ui is null there -- Vars.java:209/332),
so this test does what the game does, minus the pixels:

  * loads rules.generated.js and quickcheck.js into the script scope, then main.js
    with its two require("...") calls swapped for those globals;
  * asserts init reports "ready" and never prints "init failed"/"bootstrap failed";
  * asks the mod itself to build its settings table against a fake Table
    (maiSelfTest) -- this catches Table/Cell API drift and broken wiring;
  * takes the Table/Cell method names maiSelfTest reported using, and calls each
    one on a REAL arc.scene.ui.layout.Table in the engine: a shape that hits a
    Java overload ambiguity (t.table(function) does in v160.7) fails here, which
    a fake JS table can never catch;
  * reads the diagnostics block (maiDiag) and checks it names both directories;
  * confirms maiSettings() says "no ui" headless instead of throwing.
  * greps the source for the v0.4.0 wiring a headless engine cannot show: closeOnBack()
    plus a close button on the modal panel, the review bar hooked to Vars.ui.logic, F4 -> AI panel.

Gate treats exit 3 as SKIP (no JRE / no jar).

Why this exists: the first version of the console-function mount read scope.get(k, scope)
back, which throws "Cannot find function get" under the real engine -- main.js died
after loading its rule table and the mod looked half-installed.
"""
import http.server, io, json, os, re, shutil, socket, subprocess, sys, time
import threading
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

SCRIPTS = os.path.join(ROOT, 'MlogAuxiliary', 'scripts')
MAIN = os.path.join(SCRIPTS, 'main.js')


def preloaded(tmpdir):
    """main.js with require() swapped for globals of the same script scope."""
    src = io.open(MAIN, 'rb').read().decode('utf-8')
    src = src.replace('RULES = require("rules.generated");', 'RULES = MAI_RULES;')
    src = src.replace('QUICK = require("quickcheck");', 'QUICK = MAI_QUICKCHECK;')
    p = os.path.join(tmpdir, 'main_preloaded.js')
    io.open(p, 'w', encoding='utf-8', newline='\n').write(src)
    return p


def region_probe(srv):
    """P3: place a container and a vault next to the processor, then snapshot them.

    The snapshot is the mod's own code path (maiSnapshot -> regionSnapshot), so this
    pins the two engine facts that are easy to get wrong: tile.block() is a METHOD
    while tile.build is a FIELD, and empty tiles must be skipped (their block name
    is 'air').  A JS-side fake cannot catch either."""
    bx, by = srv.base
    return [
        "var W = Packages.mindustry.Vars.world;",
        "W.tile(%d, %d).setBlock(Packages.mindustry.Vars.content.block('container'));" % (bx + 2, by + 1),
        "W.tile(%d, %d).setBlock(Packages.mindustry.Vars.content.block('switch'));" % (bx + 4, by + 1),
        # v0.12.9: stock lives on the BUILDING, not the tile.  A 2x2 container is one
        # building on four tiles, so 100 copper must come back as 100, not 400.
        "var cb = W.tile(%d, %d).build;" % (bx + 2, by + 1),
        "cb.items.add(Packages.mindustry.content.Items.copper, 100);",
        # and a unit: units are not in the tile grid at all, they come from Groups.unit
        # and are filtered by tile position (world pixels / Vars.tilesize).
        "var u = Packages.mindustry.content.UnitTypes.dagger.create(Packages.mindustry.game.Team.sharded);",
        "u.x = (%d + 0.5) * 8; u.y = (%d + 0.5) * 8;" % (bx + 3, by + 2),
        "Packages.mindustry.gen.Groups.unit.add(u);",
        "print('MAISNAP:' + maiSnapshot(%d, %d, %d, %d));" % (bx - 1, by - 1, bx + 4, by + 4),
    ]


def select_probe(srv):
    """v0.5.0: F3 IS the left mouse button -- hold to drag, release to snapshot.

    The mouse cannot be driven here, so the state machine is pinned directly: selPress()
    is the button going down (there is no arming step any more) and selRelease() is it
    coming up. The highlight must use the vanilla rectangle recipe -- Lines.stroke(2) plus
    a Pal.accentBack/Pal.accent double outline anchored at the box's LOWER-LEFT corner
    (InputHandler.java:1758-1795) -- not a translucent fill, and not a centre anchor.
    """
    bx, by = srv.base
    return [
        "print('MAIRECT:' + maiSelRect(%d, %d, %d, %d));" % (bx + 4, by + 4, bx - 1, by - 1),
        "print('MAIRECT2:' + maiSelRect(%d, %d, %d, %d));" % (bx - 1, by - 1, bx + 4, by + 4),
        "print('MAIDUMP:' + JSON.stringify(maiDump(%d, %d, %d, %d)));"
        % (bx - 1, by - 1, bx + 4, by + 4),
        # the highlight is Lines.rect(x,y,w,h) + Lines.stroke(2) + Draw.color(Color); pin
        # those overloads by reflection (a headless engine has no GL context, so drawing
        # itself would throw and the bug would hide in the catch).
        "var DL = Packages.java.lang.Class.forName('arc.graphics.g2d.Lines');",
        "var lm = DL.getMethods(), hasLRect = 'NO', hasStroke = 'NO';",
        "for(var li = 0; li < lm.length; li++){",
        "  var ln = String(lm[li].getName()), lp = lm[li].getParameterTypes();",
        "  if(ln === 'rect' && lp.length === 4 && String(lp[0].getName()) === 'float'){ hasLRect = 'YES'; }",
        "  if(ln === 'stroke' && lp.length === 1 && String(lp[0].getName()) === 'float'){ hasStroke = 'YES'; }",
        "}",
        "var DD = Packages.java.lang.Class.forName('arc.graphics.g2d.Draw');",
        "var dm = DD.getMethods(), hasCol = 'NO';",
        "for(var di = 0; di < dm.length; di++){",
        "  var dn = String(dm[di].getName()), dp = dm[di].getParameterTypes();",
        "  if(dn === 'color' && dp.length === 1 && String(dp[0].getName()) === 'arc.graphics.Color'){ hasCol = 'YES'; }",
        "}",
        "print('MAIDRAWAPI:' + hasLRect + ':' + hasStroke + ':' + hasCol + ':' + (Packages.mindustry.graphics.Pal.accentBack === null ? 'noaccentback' : 'accentback'));",
        # the size label needs the generated Fonts holder (mindustry.gen.Fonts.outline)
        "var hasFont = 'NO';",
        "try{ var FC = Packages.java.lang.Class.forName('mindustry.ui.Fonts'); var ff = FC.getField('outline'); if(ff !== null){ hasFont = 'YES'; } }catch(e1){ hasFont = 'NO'; }",
        "print('MAIFONTAPI:' + hasFont);",
        # v0.5.0: F3 down = button down (no arming), F3 up = button up
        "print('MASELIDLE:' + maiSelState());",
        "print('MASELPRESS:' + maiSelPress(%d, %d));" % (bx + 5, by + 5),
        "print('MASELDOWN:' + maiSelState());",
        "print('MASELMOVE:' + maiSelMove(%d, %d));" % (bx + 9, by + 7),
        "print('MASELREL:' + maiSelRelease());",
        "print('MASELEND:' + maiSelState());",
        # the highlight args are the box's LOWER-LEFT corner in world units (x8)
        "print('MASELDRAW:' + maiSelDraw(%d, %d, %d, %d));" % (bx + 5, by + 5, bx + 9, by + 7),
        # the hint must be showInfoFade (a Touchable.disabled label, UI.java:373), never
        # showText -- a Dialog eats the mouse and the drag never starts.
        "var UF = Packages.java.lang.Class.forName('mindustry.core.UI');",
        "var um = UF.getMethods(), hasFade = 'NO';",
        "for(var ui = 0; ui < um.length; ui++){",
        "  var un = String(um[ui].getName()), up = um[ui].getParameterTypes();",
        "  if(un === 'showInfoFade' && up.length === 1 && String(up[0].getName()) === 'java.lang.String'){ hasFade = 'YES'; }",
        "}",
        "print('MAIHINTAPI:' + hasFade);",
        # v0.8.1：单格选中处理器 = 开 AI 面板（F4 并入 F3）
        "print('MAIPICK:' + maiPick(%d, %d));" % (bx, by),
        "print('MAIPICKEMPTY:' + maiPick(%d, %d));" % (bx + 1, by + 1),
    ]


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_sidecar(fake_path, jobs_dir):
    """A real tools/logicd.py with the offline fake provider.

    The job queue is the only path where the mod, the sidecar, the gate and the game's
    own processor writer meet, so it cannot be faked in JS: the probe below submits a
    job, waits for it, writes the payload into the engine's processor and reads the
    code back.
    """
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, '-X', 'utf8', os.path.join(ROOT, 'tools', 'logicd.py'),
         '--port', str(port), '--jobs-dir', jobs_dir,
         '--jobs-provider', 'fake', '--fake-provider', fake_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/health' % port, timeout=1) as r:
                if r.status == 200:
                    return proc, port
        except Exception:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError('logicd did not come up on port %d' % port)


def job_probe(port, bx, by):
    """v0.8.0: one clean job and one rejected job, driven through the real queue."""
    return [
        "print('MAIJOBSET:' + maiSetBase('http://127.0.0.1:%d'));" % port,
        "var jClean = maiJobSyncAt(%d, %d, 'ui clean code', 25000);" % (bx, by),
        "print('MAIJOBCLEAN:' + jClean);",
        "print('MAIJOBPROG:' + maiJobProgress(jClean));",
        "print('MAIJOBWRITE:' + maiJobWrite(jClean, %d, %d));" % (bx, by),
        # v0.10.1: the processor is edited AFTER the job is submitted -- the first write
        # must be refused (and must not touch the code), the second click overwrites.
        "var jConf = maiJobSyncAt(%d, %d, 'ui clean code conflict', 25000);" % (bx, by),
        "print('MAIJOBCONF:' + jConf);",
        "var cb = Packages.mindustry.Vars.world.build(%d, %d);" % (bx, by),
        "cb.updateCode('set n 7' + String.fromCharCode(10) + 'end');",
        "print('MAIJOBEDIT:' + maiJobWrite(jConf, %d, %d));" % (bx, by),
        "print('MAIJOBEDIT2:' + maiJobWrite(jConf, %d, %d));" % (bx, by),
        "var jRej = maiJobSyncAt(%d, %d, 'ui broken code', 25000);" % (bx, by),
        "print('MAIJOBREJ:' + jRej);",
        # v0.9.0: this engine processor has no links at all, and a program that
        # names a link anyway must still be refused (the v0.8.0 blind spot).
        "var jZero = maiJobSyncAt(%d, %d, 'ui hallucinated link', 25000);" % (bx, by),
        "print('MAIJOBZERO:' + jZero);",
        # v0.12.0: one batch over two processors -- written together, undone together,
        # and a second target whose configure throws must roll the first one back.
        # v0.12.4: the sidecar keeps the model's reasoning on the job and the panel
        # prints it under the status line.  The fake provider has no reasoning, so hand
        # the renderer a hand-made job that does (this is the UI half, not the transport).
        "print('MAIJOBTHINK:' + maiJobReasoning('{\"id\":\"t1\",\"state\":\"done\",\"reasoning\":\"先想一想：玩家要的是打印铜的数量\"}'));",
        # v0.18.4: the thinking window reads the whole text, not the 16x40 head.
        "print('MAIJOBTHINK2:' + maiJobThinkProbe(JSON.stringify({id:'t2',state:'failed',reasoning:'%s'})));" % ("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        "var wmA = maiJobSyncAt(%d, %d, 'ui clean code batch', 25000);" % (bx, by),
        "print('MAIWRITEMANY:' + maiWriteManyProbe(wmA, %d, %d));" % (bx, by),
    ]


def arrow_probe():
    """v0.4.7: findings -> one arrow per line, glued to that line's StatementElem.

    The mapping (finding.line -> statements child index -> pixel position) IS the feature,
    and it rests on two engine facts a JS fake cannot pin: children come back in statement
    order, and the header row of each StatementElem is 38 px tall (LCanvas.java:427-491).
    The icon is passed in because a headless engine has no atlas (Icon.right is null), and
    line 0 / line 9 must be skipped instead of throwing -- a stale finding after an edit
    can point past the end of the code.
    """
    return [
        "var arT = Packages.arc.scene.ui.layout.Table;",
        "var arRoot = new arT();",
        "var arSt = new arT();",
        "arRoot.add(arSt).padLeft(40).padTop(12).growX();",
        "var arKids = [];",
        "for(var arI = 0; arI < 4; arI++){",
        "  var arE = new arT();",
        # no Label here: add(CharSequence) needs a Label style from Core.scene, which is
        # null in a headless engine -- an Image child needs no style.
        "  arE.add(new Packages.arc.scene.ui.Image(new Packages.arc.graphics.g2d.TextureRegion())).growX().height(38);",
        "  arSt.add(arE).growX().height(60);",
        "  if(arI < 3){ arSt.row(); }",
        "  arKids.push(arE);",
        "}",
        "arRoot.pack();",
        "print('MAIARST:' + arSt.x + ',' + arSt.y);",
        "for(var arJ = 0; arJ < arKids.length; arJ++){",
        "  print('MAIARROWKID:' + arJ + ':' + arKids[arJ].x + ',' + arKids[arJ].y + ',' + arKids[arJ].getHeight());",
        "}",
        "var arF = JSON.stringify([",
        "{sev:'WARN', line:1, rid:'link-name-not-bound', msg:'one'},",
        "{sev:'ERR', line:3, rid:'sensor-not-a-property', msg:'three-a'},",
        "{sev:'INFO', line:3, rid:null, msg:'three-b'},",
        "{sev:'WARN', line:9, rid:null, msg:'past the end'},",
        "{sev:'WARN', line:0, rid:null, msg:'before the start'}",
        "]);",
        "var arDr = new Packages.arc.graphics.g2d.TextureRegion();",
        "print('MAIARROWS:' + maiArrows(arSt, arF, arDr));",
    ]

ECHO_MARK = 'MAISIDECAR-OK'


class _Echo(http.server.BaseHTTPRequestHandler):
    """Answers like tools/logicd.py: JSON, Content-Length, HTTP/1.0 style close."""

    def _reply(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply({'ok': True, 'mark': ECHO_MARK, 'method': 'GET', 'path': self.path})

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n).decode('utf-8', 'replace') if n else ''
        self._reply({'ok': True, 'mark': ECHO_MARK, 'method': 'POST', 'echo': raw})

    def log_message(self, *args):
        pass


def echo_server():
    """A real local HTTP server: the socket transport cannot be faked in JS."""
    hs = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Echo)
    threading.Thread(target=hs.serve_forever, daemon=True).start()
    return hs, hs.server_address[1]


def http_probe(port):
    # One line per probe, and never echo the body of the dead-port case: the simulator's
    # js() bridge raises SimError on any console line containing "Exception"/"Error:",
    # which would look like a transport failure instead of the expected code -1.
    return [
        "var p1 = maiHttpRaw('GET', 'http://127.0.0.1:%d/health', null);" % port,
        "print('MAIHTTP1:' + p1.split('\\n')[0] + ':' + (p1.indexOf('MAISIDECAR-OK') >= 0 ? 'MARK' : 'NOMARK'));",
        "var p2 = maiHttpRaw('POST', 'http://127.0.0.1:%d/plan', 'MAIPING');" % port,
        "print('MAIHTTP2:' + p2.split('\\n')[0] + ':' + (p2.indexOf('MAIPING') >= 0 ? 'ECHO' : 'NOECHO'));",
        "var p3 = maiHttpRaw('GET', 'http://127.0.0.1:1/health', null);",
        "print('MAIHTTP3:' + p3.split('\\n')[0]);",
    ]
def main():
    try:
        import mlogsim as M
    except Exception as e:
        print('SKIP no mlogsim: %s' % e)
        return 3
    jar = getattr(M, 'V160_JAR', None)
    if not jar or not os.path.exists(jar):
        print('SKIP no v160 jar')
        return 3
    java = M._default_java()
    if not java:
        print('SKIP no java')
        return 3

    d = os.path.join(ROOT, 'out', '_lw_ui')
    os.makedirs(d, exist_ok=True)
    pre = preloaded(d).replace(chr(92), '/')
    sc = SCRIPTS.replace(chr(92), '/')

    srv = M.Server(java, jar)
    srv.prepare()
    M.run_mlog(srv, 'p', 'noop', steps=1, update='none')
    js = ' '.join([
        "var s = Packages.mindustry.Vars.mods.getScripts();",
        "var F = Packages.java.nio.file.Files, P = Packages.java.nio.file.Paths, JS = Packages.java.lang.String;",
        "function rd(p){ return new JS(F.readAllBytes(P.get(p)), 'UTF-8'); }",
        "s.runConsole(rd('%s/rules.generated.js'));" % sc,
        "s.runConsole(rd('%s/quickcheck.js'));" % sc,
        "s.runConsole(rd('%s'));" % pre,
        "print('MAISELF:' + maiSelfTest());",
        "print('MAIDIAG:' + maiDiag().replace(/\\n/g, ' | '));",
        "print('MAISET:' + maiSettings());",
        "print('MAILIST:' + maiList().split('\\n')[0]);",
        "print('MAILISTROWS:' + maiListRows());",
        "print('MAIUNDO:' + maiUndoProbe());",
    "print('MAICONFLICT:' + maiConflictProbe());",
    # v0.12.8: the conflict baseline now covers the link table, and a multiplayer write
    # that the server drops has to say 无权限 instead of looking successful.
    "print('MAILINKCONFLICT:' + maiLinkConflictProbe());",
    "print('MAINET:' + maiNetProbe());",
    "print('MAIWRITECHECK:' + maiWriteCheckProbe());",
    # v0.13.0: the job status lines that go to the chat box (format only; no ui, no net)
    "print('MAICHAT:' + maiChatProbe());",
                    "print('MAISIDECAR:' + maiSidecarProbe());",
    # v0.13.2: the two sections the job view now opens up -- the model's assumptions
    # and the cost report (which carries the UNREACHABLE block).
    "print('MAIJOBPANEL:' + maiJobPanelProbe(JSON.stringify({state:'done',"
    " assumptions:['假定一','假定二'],"
    " cost:'UNREACHABLE : 1 statement(s) can never run:\\nL11: control shootp salvo1 null 0\\nloop heads: L5(?)'})));",
    # v0.15.0 (A2+A3): a failed job carries a class, a per-attempt log and the call
    # that produced it; the panel has to turn that into a sentence.
    "print('MAIJOBPANELDIAG:' + maiJobDiagProbe(JSON.stringify({state:'failed',"
    " attempts:2, provider:'http', model:'m-a',"
    " findings:[{sev:'ERR', line:0, msg:'model endpoint answered with an empty message',"
    " kind:'empty'}],"
    " attempt_log:[{n:1, ms:1200, provider:'http', model:'m-a', budget:4096,"
    " prompt_tokens:11, completion_tokens:1600, finish_reason:'length', kind:'empty',"
    " error:'empty reply'}]})));",
    # v0.15.1 (D2+D3): the last hint is kept, and the manager list filters.
    "print('MAIHINT:' + maiHintProbe('框里有 3 台处理器：把光标移到要续聊的那台上再点「续聊」'));",
    "print('MAIFILTER:' + maiFilterProbe(JSON.stringify([{id:'a1', state:'done', desc:'打印矿石'},"
    " {id:'a2', state:'failed', desc:'打印玩家'}, {id:'a3', state:'rejected', desc:'打印矿石'}]), '矿石', false));",
    "print('MAIFILTER2:' + maiFilterProbe(JSON.stringify([{id:'a1', state:'done', desc:'打印矿石'},"
    " {id:'a2', state:'failed', desc:'打印玩家'}, {id:'a3', state:'rejected', desc:'打印矿石'}]), '', true));",
    # v0.14.0: the workspace data layer (W1) -- reuse by selection centre, archive, release.
    # The probe swaps the three storage primitives for an in-memory table, so it also works
    # where Vars.dataDirectory is null (headless sim does not call Vars.loadSettings).
    "print('MAIWORKSPACE:' + maiWorkspaceProbe());",
    "print('MAIWS2:' + maiWorkspace2Probe());",
    # v0.19.0: the workspace UI (filter/sort/detail model/world ghost boxes/release confirm).
    "print('MAIWS3:' + maiWorkspace3Probe());",
    # v0.20.0 (user m02407/m02414/m02455): the free-chat dialog -- size budget, state wording,
    # list sub-line, tool trace line, whitespace helpers. Pure functions only (no Vars.ui).
    "print('MAICHATUI:' + maiChatUiProbe());",
    # v0.21.0 档1（用户 m04199「能直接把工作区快照并入MAI对话里面吗」）：两个窗口共用一个外壳，
    # 标题行右侧是「对话 / 工作区」分段开关，开一个就关掉另一个。纯状态探针，窗口本身要真机看。
    "print('MAIDUAL:' + maiDualProbe());",
    # v0.22.0 档3（用户 m04334「完成之后继续做档3」）：工作区快照随 POST /chat 一起下发，
    # 侧车只读地用 ws_list / ws_get / ws_search 看它。这里只验纯函数的裁剪与预算。
    "print('MAIWSDIGEST:' + maiWsDigestProbe());",
    # v0.16.0 B1: 存蓝图在无头环境（Vars.schematics 为 null）必须先守住，不能抛。
    "print('MAIBP:' + maiBlueprintSaveProbe(JSON.stringify({x1:10, y1:10, x2:20, y2:20})));",
    # v0.16.1 B2/B4: 签名 payload v2 的 meta 要能在游戏里解出来（Java Base64 + zlib）。
    "print(maiSigMetaProbe('aigenerated:MAI:5f1371d29749:enjaq1YqU7Iy0lFKVrKKjjbUMdRRUlZ4NnXD8_n7n8_frBQbq6OUq2RVrVRerGQFJAyVdJTyEnNTgZwXk-c-nb3v-ZRNQKGCovzcghKgIFzn-z09z3YvfLKr7emGjU8b5wDVZOUnARUASXOl2loA1_0zJw'));",
    # 原版「详细」页标题形如 "[[schematic] 名"（arc 用 [[ 转义一个 [）。
    "print(maiBpTitleProbe('[[schematic] 蓝图甲'));",
    "print(maiBpChatProbe('蓝图甲'));",
    # v0.18.0：首次配置向导的布线（无头没有 Vars.ui，只能验页面能建 + 标记读回）。
    "print('MAIWELCOME:' + maiWelcomeProbe());",
    "print('MAILLM:' + maiLlmProbe());",
    # v0.18.1（用户 m10546）：改键之后原版落盘的是只有 min/max 的 Axis，读数必须跟上。
    "print(maiKeybindProbe());",
    # v0.18.2（用户 m10938）：主题色/强调色成了设置项，写入必须立刻读回来。
    "print(maiThemeProbe());",
    # v0.12.5: the retained selection box + corner badge state machine (no sidecar, no ui)
    "print('MAIJOBBOX0:' + maiJobBox());",
    "print('MAIJOBBOXPIN:' + maiJobBoxPin(JSON.stringify(['a','b']), JSON.stringify(['queued','running'])));",
    "print('MAIBADGERECT:' + maiBadgeRect());",
    "print('MAIJOBBOXFIN:' + maiJobBoxPin(JSON.stringify(['a','b']), JSON.stringify(['done','rejected'])));",
    "print('MAIJOBBOXCLR:' + maiJobBoxClear());",
    "print('MAIJOBBOX3:' + maiJobBox());",
    # v0.12.7: the box must NOT time itself out -- only a write or an explicit close ends it
    "print('MAIJOBBOXPIN2:' + maiJobBoxPin(JSON.stringify(['a']), JSON.stringify(['done'])));",
    "print('MAIJOBBOXCLOSE:' + maiJobBoxClose());",
    "print('MAIJOBBOX4:' + maiJobBox());",
    "print('MAIJOBMANAGER:' + maiJobManager());",
    ] + region_probe(srv) + select_probe(srv) + arrow_probe() + [
        "void 0;",
    ])
    try:
        out = srv.js(js)
    except Exception as e:
        print('FAIL engine call: %s' % str(e)[-400:])
        return 1
    text = '\n'.join(str(x) for x in out)
    lines = [l for l in text.splitlines()
             if '[mai]' in l or 'mlog-auxiliary' in l
             or l.startswith(('MAISELF:', 'MAIDIAG:', 'MAISET:', 'MAILIST:', 'MAILISTROWS:',
                              'MAIUNDO:', 'MAICONFLICT:', 'MAILLM:',
                              'MAILINKCONFLICT:', 'MAINET:', 'MAIWRITECHECK:', 'MAICHAT:', 'MAISIDECAR:',
                              'MAIJOBPANEL:', 'MAIJOBPANELDIAG:', 'MAIHINT:', 'MAIFILTER:', 'MAIFILTER2:', 'MAIWORKSPACE:', 'MAIWS2:', 'MAIWS3:', 'MAICHATUI:', 'MAIDUAL:', 'MAIWSDIGEST:', 'MAIBP:',
                              'MAISIGMETA:', 'MAIBPTITLE:', 'MAIBPCHAT:', 'MAIWELCOME:', 'MAIKEYBIND:', 'MAITHEME:',
                              'MAIJOBBOX0:', 'MAIJOBBOXPIN:', 'MAIJOBBOXFIN:',
                              'MAIJOBBOXCLR:', 'MAIJOBBOX3:', 'MAIJOBMANAGER:',
                              'MAIJOBBOXPIN2:', 'MAIJOBBOXCLOSE:', 'MAIJOBBOX4:',
                              'MAIBADGERECT:', 'MAIDBG:', 'MAIJOBTHINK2:'))]
    joined = '\n'.join(lines)

    # ---- resolve every Table/Cell method the page actually uses, in the engine
    used = []
    for l in text.splitlines():
        if l.startswith('MAISELF:') and '\u7528\u5230\u7684\u65b9\u6cd5' in l:
            used = [x for x in l.split('\u7528\u5230\u7684\u65b9\u6cd5 ')[-1].strip().split(',') if x]
    if not used:
        used = []

    # arg shells: the method name is enough, because only Java overload RESOLUTION
    # is under test here -- any runtime failure caused by a null skin counts as OK.
    SHELL = {
        'defaults': '', 'row': '',
        'add': "t.add('x');",
        'label': "t.label('x');",
        'labelWrap': "t.labelWrap('x');",
        'field': "t.field('v', function(){});",
        'button': "t.button('b', function(){});",
        'check': "t.check('c', false, function(){});",
        'image': "t.image('info');",
        'pane': "t.pane(function(tt){});",
        'table': "t.table(function(tt){});",
        'scrollPane': "t.scrollPane(function(tt){});",
        'pad': "t.add('x').pad(4);",
        'left': "t.add('x').left();",
        'width': "t.add('x').width(120);",
        'colspan': "t.add('x').colspan(2);",
        'get': "t.add('x').get();",
        'padTop': "t.add('x').padTop(4);",
        'padBottom': "t.add('x').padBottom(4);",
        'padRight': "t.add('x').padRight(4);",
        'right': "t.add('x').right();",
        'clear': 't.clear();',
        'color': "t.add('x').color(Packages.arc.graphics.Color.white);",
        'margin': 't.margin(8);',
        'growX': "t.add('x').growX();",
        'height': "t.add('x').height(40);",
    }
    missing = [m for m in used if m not in SHELL]
    probe = [
        "var T = Packages.arc.scene.ui.layout.Table;",
        "function cls(e){ var s = '' + e;"
        " if (s.indexOf('ambiguous') >= 0) return 'AMBIGUOUS';"
        " if (s.indexOf('Cannot find function') >= 0) return 'NOFUNC';"
        " if (s.indexOf('TypeError') >= 0) return 'TYPE';"
        " return 'OK_SKIN'; }",
        "var out = [];",
    ]
    for m in used:
        if m in SHELL:
            probe.append("(function(){ var t = new T(); var r;"
                         " try { %s r = 'RESOLVED'; } catch(e){ r = cls(e); }"
                         " out.push('%s=' + r); })();" % (SHELL[m], m))
    # the console only echoes a trailing array literal without printing 'undefined',
    # and Server.js waits for that marker -- so print explicitly and end with void 0
    # v0.4.1: Table.pane() hands back Cell<ScrollPane>, and Group has no public add()
    # (Arc-Source/arc-core/src/arc/scene/Group.java -- addChild(Element) is the real API).
    # That is exactly why v0.4.0 crashed on out.add() while out.clear() worked.
    probe.append("(function(){ var sp;"
                 " try { sp = new Packages.arc.scene.ui.ScrollPane(new T()); }"
                 " catch(e){ sp = null; }"
                 " out.push('ScrollPaneHasAdd=' + ((sp !== null && typeof sp.add === 'function') ? 'YES' : 'NO')); })();")
    probe.append("print('MAISHAPES: ' + out.join(' '));")
    probe.append("void 0;")
    shapes = {}
    if used and not missing:
        try:
            res = srv.js(' '.join(probe))
            flat = ' '.join(str(x) for x in res)
            for k, v in re.findall('([A-Za-z]+)=([A-Z_]+)', flat):
                shapes[k] = v
        except Exception as e:
            shapes = {'<engine>': 'FAIL ' + str(e)[-200:]}

    problems = []
    if 'ready,' not in joined:
        problems.append('mod never reported ready')
    if 'init failed' in joined or 'bootstrap failed' in joined:
        problems.append('init threw')
    if 'MAISELF:\u8bbe\u7f6e\u8868\u683c\u53ef\u5efa' not in joined:
        problems.append('maiSelfTest did not build the settings table')
    if 'MAISELF:' in joined and '\u586b\u4e86 0 ' in joined:
        problems.append('maiSelfTest filled no rows')
    if '\u533a\u57df\u8868\u683c' not in joined:          # 区域表格
        problems.append('maiSelfTest did not build the region table')
    if '\u6e05\u5355\u8868\u683c' not in joined:          # 清单表格 (v0.7.0)
        problems.append('maiSelfTest did not build the processor list table')
    if '\u6570\u636e\u76ee\u5f55' not in joined or 'mods \u76ee\u5f55' not in joined:
        problems.append('maiDiag is missing a directory line')
    if 'MAISET:no ui' not in joined:
        problems.append('maiSettings should report "no ui" headless')
    if 'MAILIST:\u5168\u56fe' not in joined:
        problems.append('maiList did not report a whole-map summary: %r'
                        % [l for l in lines if l.startswith('MAILIST:')][:1])
    if 'MAILISTROWS:[' not in joined:
        problems.append('maiListRows is not returning a JSON array')
    if 'MAICONFLICT:ok' not in joined:
        problems.append('v0.10.1: the pre-write conflict gate is broken: %r'
                        % [l for l in lines if l.startswith('MAICONFLICT:')][:1])
    if 'MAILLM:ok ' not in joined:
        problems.append('v0.11.0: the model-tier settings probe failed: %r'
                        % [l for l in lines if l.startswith('MAILLM:')][:1])
    if 'MAIUNDO:ok ' not in joined:
        problems.append('v0.10.0: the undo snapshot did not round-trip through the real '
                        'filesystem: %r' % [l for l in lines if l.startswith('MAIUNDO:')][:1])
    # v0.12.8: the link table is part of the conflict baseline, and the multiplayer
    # write path has to be able to say 无权限 (headless: net=false, so no extra gates).
    if 'MAILINKCONFLICT:ok' not in joined:
        problems.append('v0.12.8: the link-table conflict gate is broken: %r'
                        % [l for l in lines if l.startswith('MAILINKCONFLICT:')][:1])
    if 'MAINET:net=false admin=false player=null perm=null' not in joined:
        problems.append('v0.12.8: the multiplayer probe is not headless-clean (single player '
                        'must take no extra gate): %r'
                        % [l for l in lines if l.startswith('MAINET:')][:1])
    if 'MAIWRITECHECK:offline queue=0' not in joined:
        problems.append('v0.12.8: the write-back check queue is not empty headless: %r'
                        % [l for l in lines if l.startswith('MAIWRITECHECK:')][:1])
    # v0.13.0: MAI + logic icon + status + (x,y) + ": info" -- the user asked for that shape
    # on both the start and the finish of a job.  Format only: headless has no chat box.
    # v0.15.2: 尖括号不能省，:logic: 用原文（发送前那一刻才过 formatIcons 变图标）。
    if 'MAICHAT:ok' not in joined:
        problems.append('v0.15.2: the chat status line lost its shape (want '
                        '<MAI:logic:><Status>(x,y):info): %r'
                        % [l for l in lines if l.startswith('MAICHAT:')][:1])
    elif '<MAI:logic:><Generating>(12,34):\u7ed9\u6838\u5fc3\u8865\u94dc' not in joined:
        problems.append('v0.15.2: the Generating line is not '
                        '"<MAI:logic:><Generating>(x,y):info": %r'
                        % [l for l in lines if l.startswith('MAICHAT:')][:1])
    elif '<MAI:logic:><Cancelled>(1,2)' not in joined:
        problems.append('v0.15.2: an empty detail must not leave a dangling colon: %r'
                        % [l for l in lines if l.startswith('MAICHAT:')][:1])

    # v0.13.1: the package ships a frozen logicd.exe and the mod spawns it itself, so
    # the player needs neither Python nor a console window.  Headless there is no exe
    # next to the mod, so the state machine must fall back quietly to noexe instead of
    # throwing (a throw here would also kill every later probe in the same run).
    if 'MAISIDECAR:ok' not in joined:
        problems.append('v0.13.1: the sidecar autostart state machine is broken headless '
                        '(want MAISIDECAR:ok ... state=idle->noexe): %r'
                        % [l for l in lines if l.startswith('MAISIDECAR:')][:1])
    elif ' exe=none' not in joined:
        problems.append('v0.13.1: a dev checkout has no sidecar/bin/logicd.exe, so the probe '
                        'must report exe=none: %r'
                        % [l for l in lines if l.startswith('MAISIDECAR:')][:1])

    # v0.13.2: the job view used to show findings and code only. The model's assumptions
    # and the cost report (with its UNREACHABLE block) existed but were invisible; the
    # probe feeds a job with two assumptions and a three-line cost report and checks both
    # sections come back, the cost one starting at UNREACHABLE.
    if 'MAIWORKSPACE:ok' not in joined:
        problems.append('v0.14.0: the workspace data layer is broken '
                        '(want MAIWORKSPACE:ok dir=no|yes mem=yes): %r'
                        % [l for l in lines if l.startswith('MAIWORKSPACE:')][:1])

    if 'MAIWS2:ok' not in joined:
        problems.append('v0.14.1: the workspace dialog helpers are broken '
                        '(want MAIWS2:ok): %r'
                        % [l for l in lines if l.startswith('MAIWS2:')][:1])
    # v0.19.0: the workspace UI is one screen with a filter/sort bar, a detail model that
    # caps its tables, a world layer that only boxes live+on-this-map workspaces, and a
    # release path that asks first.
    if 'MAIWS3:ok' not in joined:
        problems.append('v0.19.0: the workspace UI helpers are broken '
                        '(want MAIWS3:ok ...): %r'
                        % [l for l in lines if l.startswith('MAIWS3:')][:1])
    # v0.20.0 (user m02407/m02414/m02455): the chat dialog is one screen with a chat list, a
    # 2px separator and a transcript; the plan/state/summary helpers must stay deterministic.
    if 'MAICHATUI:ok' not in joined:
        problems.append('v0.20.0: the chat UI helpers are broken '
                        '(want MAICHATUI:ok ...): %r'
                        % [l for l in lines if l.startswith('MAICHATUI:')][:1])
    if 'MAIJOBPANELDIAG:ok why=1 ev=2' not in joined:
        problems.append('v0.15.0: the job panel does not explain a failed call '
                        '(want MAIJOBPANELDIAG:ok why=1 ev=2): %r'
                        % [l for l in lines if l.startswith('MAIJOBPANELDIAG:')][:1])
    # v0.21.0 档1: the two dialogs share one shell (title row segmented switch), so the mode
    # list and the default mode must stay deterministic.
    if 'MAIDUAL:ok' not in joined:
        problems.append('v0.21.0: the shared dialog shell lost its mode switch '
                        '(want MAIDUAL:ok modes=chat,ws ...): %r'
                        % [l for l in lines if l.startswith('MAIDUAL:')][:1])
    # v0.22.0 档3: the workspace snapshot that rides on POST /chat must be trimmed to a budget --
    # full code on every processor of every workspace would blow up the request.
    if 'MAIWSDIGEST:ok' not in joined:
        problems.append('v0.22.0: the workspace digest lost its budget/trim rules '
                        '(want MAIWSDIGEST:ok ws=2 ...): %r'
                        % [l for l in lines if l.startswith('MAIWSDIGEST:')][:1])
    if '为什么：端点回了空内容' not in joined:
        problems.append('v0.15.0: the failure explanation lost its wording: %r'
                        % [l for l in lines if l.startswith('MAIJOBPANELDIAG:')][:1])
    if 'finish_reason=length' not in joined:
        problems.append('v0.15.0: the diagnostics no longer show the finish_reason: %r'
                        % [l for l in lines if l.startswith('MAIJOBPANELDIAG:')][:1])
    if 'MAIHINT:ok last=yes' not in joined:
        problems.append('v0.15.1: hint() no longer keeps the last message '
                        '(want MAIHINT:ok last=yes): %r'
                        % [l for l in lines if l.startswith('MAIHINT:')][:1])
    if 'MAIFILTER:ok hit=2 of 3 btn=只看失败：关' not in joined:
        problems.append('v0.15.1: the manager filter does not narrow the list '
                        '(want MAIFILTER:ok hit=2 of 3): %r'
                        % [l for l in lines if l.startswith('MAIFILTER:')][:1])
    if 'MAIFILTER2:ok hit=2 of 3 btn=只看失败：开' not in joined:
        problems.append('v0.15.1: the only-failed toggle is broken '
                        '(want MAIFILTER2:ok hit=2 of 3 btn=只看失败：开): %r'
                        % [l for l in lines if l.startswith('MAIFILTER2:')][:1])
    if 'MAIBP:probeblueprint:蓝图接口不可用' not in joined:
        problems.append('v0.16.0: save-as-blueprint must guard a null Vars.schematics '
                        '(want MAIBP:probeblueprint:蓝图接口不可用): %r'
                        % [l for l in lines if l.startswith('MAIBP:')][:1])
    if 'MAISIGMETA:ws=ws1 name=蓝图甲 job=job7 prompt=数矿石' not in joined:
        problems.append('v0.16.1: the signed payload v2 meta does not decode in-game '
                        '(want MAISIGMETA:ws=ws1 name=蓝图甲 job=job7 prompt=数矿石): %r'
                        % [l for l in lines if l.startswith('MAISIGMETA:')][:1])
    if 'MAIBPTITLE:[蓝图甲]' not in joined:
        problems.append('v0.16.1: the schematic info title is not parsed '
                        '(want MAIBPTITLE:[蓝图甲]): %r'
                        % [l for l in lines if l.startswith('MAIBPTITLE:')][:1])
    if 'MAIBPCHAT:none' not in joined:
        problems.append('v0.16.1: reading a blueprint context without a schematic library '
                        'must return none (want MAIBPCHAT:none): %r'
                        % [l for l in lines if l.startswith('MAIBPCHAT:')][:1])
    if 'MAIJOBPANEL:ok' not in joined:
        problems.append('v0.13.2: the job view does not show assumptions + cost '
                        '(want MAIJOBPANEL:ok assumptions=2 cost=3): %r'
                        % [l for l in lines if l.startswith('MAIJOBPANEL:')][:1])

    # v0.12.5: the F3 box now outlives the drag, so its state machine has to be pinned
    # headless -- there is no camera, no mouse and no sidecar in this engine.
    def boxmark(prefix):
        got = [l[len(prefix):] for l in lines if l.startswith(prefix)]
        if not got:
            return None
        try:
            return json.loads(got[0])
        except ValueError:
            return None

    if boxmark('MAIJOBBOX0:') != {'box': False}:
        problems.append('v0.12.5: maiJobBox is not empty on a clean slate: %r'
                        % [l for l in lines if l.startswith('MAIJOBBOX0:')][:1])
    pinned = boxmark('MAIJOBBOXPIN:')
    if (not isinstance(pinned, dict) or pinned.get('box') is not True
            or pinned.get('total') != 2 or pinned.get('terminal') != 0
            or pinned.get('linger') is not False):
        problems.append('v0.12.5: an in-flight batch of two is not kept on screen: %r'
                        % (pinned,))
    elif '\u751f\u6210\u4e2d' not in str(pinned.get('badge')):
        problems.append('v0.12.5: the badge of a running batch does not say so: %r'
                        % (pinned.get('badge'),))
    fin = boxmark('MAIJOBBOXFIN:')
    if (not isinstance(fin, dict) or fin.get('terminal') != 2
            or fin.get('done') != 1 or fin.get('failed') != 1
            or fin.get('linger') is not True):
        problems.append('v0.12.5: a finished batch is not marked finished: %r' % (fin,))
    elif '1 \u6210 1 \u8d25' not in str(fin.get('badge')):
        problems.append('v0.12.5: the badge does not count the verdicts: %r'
                        % (fin.get('badge'),))
    if 'MAIJOBBOXCLR:cleared' not in joined:
        problems.append('v0.12.5: maiJobBoxClear did not clear the box')
    if boxmark('MAIJOBBOX3:') != {'box': False}:
        problems.append('v0.12.5: the box came back after being cleared: %r'
                        % [l for l in lines if l.startswith('MAIJOBBOX3:')][:1])
    pinned2 = boxmark('MAIJOBBOXPIN2:')
    if not isinstance(pinned2, dict) or pinned2.get('box') is not True \
            or pinned2.get('linger') is not True:
        problems.append('v0.12.7: a finished batch is not kept on screen until the player '
                        'writes or closes it: %r' % (pinned2,))
    if 'MAIJOBBOXCLOSE:closed' not in joined:
        problems.append('v0.12.7: maiJobBoxClose did not close the box: %r'
                        % [l for l in lines if l.startswith('MAIJOBBOXCLOSE:')][:1])
    if boxmark('MAIJOBBOX4:') != {'box': False}:
        problems.append('v0.12.7: the box survived an explicit close: %r'
                        % [l for l in lines if l.startswith('MAIJOBBOX4:')][:1])
    badge = boxmark('MAIBADGERECT:')
    if not isinstance(badge, dict) or badge.get('rect') is not True:
        problems.append('v0.12.6: the corner badge has no geometry: %r'
                        % [l for l in lines if l.startswith('MAIBADGERECT:')][:1])
    else:
        sc = float(badge.get('scale') or 1)
        box = badge.get('box') or {}
        right = float(box.get('x', 0)) + float(box.get('w', 0)) - 4 / sc
        top = float(box.get('y', 0)) - 4 / sc
        if abs(float(badge.get('right')) - right) > 1e-3:
            problems.append('v0.12.6: the badge is not right-aligned to the box corner: '
                            'right=%r wanted %r' % (badge.get('right'), right))
        if abs(float(badge.get('top')) - top) > 1e-3:
            problems.append('v0.12.6: the badge is not below the box bottom edge: '
                            'top=%r wanted %r' % (badge.get('top'), top))
        if abs((float(badge.get('y')) + float(badge.get('h'))) - float(badge.get('top'))) > 1e-3:
            problems.append('v0.12.6: the badge baseline does not match its own top: %r' % (badge,))
        if abs((float(badge.get('x')) + float(badge.get('w'))) - float(badge.get('right'))) > 1e-3:
            problems.append('v0.12.6: the badge width disagrees with its right edge: %r' % (badge,))
        if '\u751f\u6210\u4e2d' not in str(badge.get('text')):
            problems.append('v0.12.6: the badge measured the wrong text: %r' % (badge.get('text'),))
        # v0.14.2: 文案换成两个固定像素尺寸的按钮（取消在最右、查看在左），都在框外、互不重叠。
        btns = badge.get('buttons')
        # v0.15.1: 第三个按钮「续聊」加在左边，这里只要求 cancel/view 还在且顺序对。
        ids = [str(b.get('id')) for b in btns] if isinstance(btns, list) else []
        if ids[:2] != ['cancel', 'view']:
            problems.append('v0.14.2: the box has no 查看/取消 buttons: %r' % (btns,))
        else:
            cancel, view = btns[0], btns[1]
            if abs(float(cancel.get('h', 0)) * sc - 18) > 1e-3:
                problems.append('v0.14.2: the buttons do not keep a fixed 18px height: %r'
                                % (float(cancel.get('h', 0)) * sc,))
            if abs((float(cancel.get('x')) + float(cancel.get('w'))) - right) > 1e-3:
                problems.append('v0.14.2: the 取消 button is not flush with the box right edge: '
                                'right=%r wanted %r'
                                % (float(cancel.get('x')) + float(cancel.get('w')), right))
            gap = float(view.get('x')) + float(view.get('w')) - float(cancel.get('x'))
            if abs(gap + 6 / sc) > 1e-3:
                problems.append('v0.14.2: the two buttons do not keep a 6px gap: %r' % (gap,))
            for b in btns:
                if float(b.get('y')) + float(b.get('h')) > float(box.get('y', 0)) + 1e-9:
                    problems.append('v0.14.2: a box button overlaps the selection box: %r' % (b,))
                if float(b.get('w', 0)) <= 0 or float(b.get('h', 0)) <= 0:
                    problems.append('v0.14.2: a box button has no size: %r' % (b,))
        if '\u70b9\u5f00\u770b' in str(badge.get('text')) or '\u53f3\u952e\u5173' in str(badge.get('text')):
            problems.append('v0.14.2: the badge still carries the old prose: %r'
                            % (badge.get('text'),))
    if 'MAIJOBMANAGER:no ui' not in joined:
        problems.append('v0.12.5: the job manager window should report "no ui" headless: %r'
                        % [l for l in lines if l.startswith('MAIJOBMANAGER:')][:1])
    if not used:
        problems.append('maiSelfTest did not report which Table methods it used')
    if missing:
        problems.append('no engine probe shell for Table method(s): ' + ', '.join(missing))
    bad = [m + '=' + shapes.get(m, 'MISSING') for m in used
           if shapes.get(m) not in ('RESOLVED', 'OK_SKIN')]
    if bad:
        problems.append('Table method(s) do not resolve in the engine: ' + ', '.join(bad))
    # comments talk about the ambiguity, so cut them before scanning code
    code_only = '\n'.join(l.split('//')[0] for l in io.open(MAIN, 'rb').read().decode('utf-8').splitlines())
    if re.search('[.](table|scrollPane)[(][ ]*function', code_only):
        problems.append('main.js calls t.table(function) / t.scrollPane(function) -- '
                        'ambiguous in this engine, keep the settings rows flat')

    # v0.4.0 wiring: a headless engine has no scene, so these can only be pinned
    # at the source level.  Each one is a bug that already shipped once.
    if 'closeOnBack()' not in code_only:
        problems.append('the modal panel never calls closeOnBack() -- arc Dialog.addCloseButton() '
                        'is an empty default (Dialog.java:453-455), so it cannot be dismissed')
    if 'buttons.button(' not in code_only:
        problems.append('the panel has no visible close button in d.buttons')
    if 'Vars.ui.logic' not in code_only or 'canvas.save()' not in code_only:
        problems.append('the review bar is not wired to the vanilla logic editor '
                        '(Vars.ui.logic / canvas.save) -- m08924 asked for that review')
    # v0.4.1 crash: Table.pane(cons) gives you a ScrollPane, which has no add().
    # Hand pane() a real Table instead -- that is what host = new Table() is for.
    if re.search('[.]pane[(][ ]*function', code_only):
        problems.append('main.js passes a builder to pane() -- that returns a ScrollPane, '
                        'not a Table; build a Table and hand it to pane(host)')
    if re.search('[.]pane[(][^;\n]*[.]get[(][)]', code_only):
        problems.append('pane(...).get() is a ScrollPane, not a Table (v0.4.0 crashed on it)')
    if shapes.get('ScrollPaneHasAdd', 'NO') == 'YES':
        problems.append('this engine DOES give ScrollPane an add() -- the v0.4.1 workaround '
                        'may be unnecessary, re-check')
    # v0.9.0: the F4 hotkey is gone for good -- the only entry is a single F3 tap.
    if 'KEY_PANEL' in code_only or 'toggleAiPanel' in code_only:
        problems.append('the F4 hotkey is back (KEY_PANEL / toggleAiPanel) -- m12288 asked for it to be deleted')

    # v0.4.3: an HTTP callback cannot be waited for on the main thread.  httpJson
    # delivers it through Core.app.post, so a spin loop here always times out --
    # that is exactly why F7 showed sidecar unavailable with a literal pending.
    if re.search('while[ ]*[(][ ]*[A-Za-z_]+[ ]*===[ ]*"pending"', code_only):
        problems.append('main.js busy-waits for an HTTP callback: the callback is posted back to the main thread, so it never lands')
    if re.search('while[ ]*[(][ ]*out[ ]*===[ ]*"[?]"', code_only):
        problems.append('ping() busy-waits on /health -- same deadlock, use probeSidecar()')
    if 'function regionAroundBox(' not in code_only:
        problems.append('the region request is not async (regionAroundBox) -- F7 would hang')
    if 'regionCandidatesText(rtext)' in code_only:
        problems.append('F7 still treats maiRegion() as synchronous')

    # ---- v0.4.4: the sidecar transport is a hand-rolled socket now.
    # URL.openConnection() hands back sun.net.www.protocol.http.HttpURLConnection, which
    # java.base does not export; Rhino binds members against the RUNTIME class (MemberBox),
    # so setRequestMethod / getResponseCode / getInputStream all threw
    # IllegalAccessException.  A JS-side fake socket would never catch that, so talk to a
    # real local HTTP server -- and to a dead port, which must come back as code -1.
    hs, hport = echo_server()
    try:
        try:
            hout = srv.js(' '.join(http_probe(hport) + ['void 0;']))
            htext = '\n'.join(str(x) for x in hout)
        except Exception as exc:
            htext = ''
            problems.append('maiHttpRaw threw in the engine: %s' % str(exc)[-300:])
        hres = {}
        for k, code, flag in re.findall(r'MAIHTTP([123]):(-?\d+)(?::(MARK|NOMARK|ECHO|NOECHO))?', htext):
            hres[k] = (int(code), flag)
        for k, code, flag in (('1', 200, 'MARK'), ('2', 200, 'ECHO'), ('3', -1, '')):
            got = hres.get(k)
            if got is None:
                problems.append('MAIHTTP%s printed nothing' % k)
                continue
            if got[0] != code:
                problems.append('MAIHTTP%s code=%r wanted %r' % (k, got[0], code))
            elif flag and got[1] != flag:
                problems.append('MAIHTTP%s body flag=%r wanted %r' % (k, got[1], flag))
    finally:
        hs.shutdown()
        hs.server_close()

    # ---- v0.8.0: the job queue end to end.  The mod submits a job, polls it, and writes
    # the returned payload into the engine's own processor; a second job names a link that
    # does not exist and must be rejected.  A real tools/logicd.py runs with the offline
    # fake provider, so no model is involved and the run is deterministic.
    jtext = ''
    jsrv = None
    try:
        fdir = os.path.join(ROOT, 'out', '_lw_ui')
        if not os.path.isdir(fdir):
            os.makedirs(fdir)
        fake = os.path.join(fdir, 'job_fake.json')
        with io.open(fake, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps({'clean': {'code': 'set n 0\nop add n n 1\nprint n\nend'},
                                 'broken': {'code': 'jump 99 always x 0\nend'},
                                 'hallucinated link': {'code': 'sensor x container9 @copper\nend'}},
                                ensure_ascii=False, indent=1))
        jdir = os.path.join(fdir, 'jobs')
        if os.path.isdir(jdir):
            shutil.rmtree(jdir, ignore_errors=True)
        jsrv, jport = start_sidecar(fake, jdir)
        bx, by = srv.base
        try:
            jout = srv.js(' '.join(job_probe(jport, bx, by) + ['void 0;']))
            jtext = '\n'.join(str(x) for x in jout)
        except Exception as exc:
            problems.append('job probe threw in the engine: %s' % str(exc)[-300:])
    except Exception as exc:
        problems.append('job sidecar failed to start: %s' % str(exc)[-300:])
    finally:
        if jsrv is not None:
            try:
                jsrv.terminate()
                jsrv.wait(timeout=15)
            except Exception:
                pass

    # ---- v0.8.0 job queue, end to end: real logicd (fake provider), real engine
    # processor.  The clean job must land as done, its payload must be writable into the
    # processor, and a job whose code names a link that does not exist must be rejected
    # with link-name-not-bound raised to ERR (tools/jobqueue.py FATAL_RIDS) -- a machine
    # -written program is never allowed to carry a dangling link name.
    jres = {}
    for l in jtext.splitlines():
        for key in ('MAIJOBSET', 'MAIJOBCLEAN', 'MAIJOBPROG', 'MAIJOBWRITE', 'MAIJOBREJ',
                    'MAIJOBCONF', 'MAIJOBEDIT', 'MAIJOBEDIT2',
                    'MAIJOBZERO', 'MAIWRITEMANY', 'MAIJOBTHINK', 'MAIJOBTHINK2'):
            if l.startswith(key + ':'):
                jres[key] = l[len(key) + 1:]

    def job_of(key, label):
        raw = jres.get(key)
        if raw is None:
            problems.append('%s printed nothing' % label)
            return {}
        try:
            return json.loads(raw)
        except Exception as exc:
            problems.append('%s returned bad JSON: %s' % (label, exc))
            return {}

    if not jres.get('MAIJOBSET', '').startswith('http://127.0.0.1:'):
        problems.append('maiSetBase did not take the test sidecar: %r' % (jres.get('MAIJOBSET'),))
    clean = job_of('MAIJOBCLEAN', 'maiJobSyncAt (clean)')
    if clean:
        if clean.get('state') != 'done':
            problems.append('clean job state=%r wanted done (%r)'
                            % (clean.get('state'), clean.get('explanation')))
        if int(clean.get('attempts') or 0) != 1:
            problems.append('clean job attempts=%r wanted 1' % (clean.get('attempts'),))
        if 'set n 0' not in str(clean.get('code')):
            problems.append('clean job code: %r' % (str(clean.get('code'))[:120],))
        if not clean.get('bytes_b64'):
            problems.append('clean job has no payload: %r' % (clean.get('bytes_b64'),))
    if '完成' not in (jres.get('MAIJOBPROG') or ''):      # 完成
        problems.append('maiJobProgress did not report completion: %r' % (jres.get('MAIJOBPROG'),))
    wr = jres.get('MAIJOBWRITE') or ''
    if '已写入' not in wr or 'set n 0' not in wr:      # 已写入
        problems.append('maiJobWrite did not write the job payload into the processor: %r' % (wr[:160],))
    edit1 = jres.get('MAIJOBEDIT') or ''
    edit2 = jres.get('MAIJOBEDIT2') or ''
    if '已被改动' not in edit1 or '已写入' in edit1:    # 处理器在投递后已被改动
        problems.append('v0.10.1: a job written onto an edited processor was not refused: %r'
                        % (edit1[:200],))
    if '已写入' not in edit2:                          # 已写入
        problems.append('v0.10.1: the second click did not overwrite after the refusal: %r'
                        % (edit2[:200],))
    conf = job_of('MAIJOBCONF', 'maiJobSyncAt (conflict)')
    if conf and conf.get('state') != 'done':
        problems.append('conflict job state=%r wanted done' % (conf.get('state'),))
    rej = job_of('MAIJOBREJ', 'maiJobSyncAt (broken code)')
    if rej:
        if rej.get('state') != 'rejected':
            problems.append('broken-code job state=%r wanted rejected' % (rej.get('state'),))
        if int(rej.get('attempts') or 0) != 3:
            problems.append('broken-code job attempts=%r wanted 3 (1 + 2 repairs)'
                            % (rej.get('attempts'),))
        if str(rej.get('code') or '') != '':
            problems.append('a rejected job still carries code: %r' % (str(rej.get('code'))[:120],))
        fat = [f for f in (rej.get('findings') or []) if f.get('sev') == 'ERR']
        if not fat:
            problems.append('rejected job findings carry no ERR line: %r'
                            % (rej.get('findings'),))
    zero = job_of('MAIJOBZERO', 'maiJobSyncAt (hallucinated link, link-less processor)')
    if zero:
        if zero.get('state') != 'rejected':
            problems.append('a processor with no links accepted a program naming container9 '
                            '(state=%r) -- the v0.8.0 link blind spot is back'
                            % (zero.get('state'),))
        zrids = [f.get('rid') for f in (zero.get('findings') or [])]
        if 'link-name-not-bound' not in zrids:
            problems.append('the zero-link refusal lost its rule: %r' % (zrids,))
    think = jres.get('MAIJOBTHINK') or ''
    if '先想一想' not in think:
        problems.append('v0.12.4: maiJobReasoning lost the job thinking: %r'
                        % (think[:160],))
    think2 = jres.get('MAIJOBTHINK2') or ''
    if not think2.startswith('ok len=80 rows=2 w0=60'):
        problems.append('v0.18.4: the thinking window no longer reads the whole text '
                        '(want ok len=80 rows=2 w0=60): %r' % (think2[:160],))
    wm = jres.get('MAIWRITEMANY') or ''
    if not wm.startswith('ok '):
        problems.append('v0.12.0: the transactional batch write probe failed: %r' % (wm[:260],))
    for needle, why in (('function jobBody(', 'the job body builder is gone'),
                        ('function jobSubmit(', 'the async job submit is gone'),
                        ('function jobTick(', 'the 1s job poll is gone'),
                        ('jobTick();', 'the per-frame hook no longer polls jobs'),
                        ('function renderJob(', 'the job result renderer is gone'),
                        ('function jobReasoningLines(',
                         'the model-thinking renderer is gone'),
                        ('api.maiJobReasoning = function',
                         'the reasoning test hook is gone'),
                        ('function openThinkDialog(', 'the thinking window is gone'),
                        ('function jobThinkCopy(', 'copying the whole thinking is gone'),
                        ('function jobThinkFetchOpen(',
                         'the list-row thinking fetch is gone'),
                        ('var tr = openThinkDialog(job);',
                         'a finished job no longer opens the thinking window'),
                        ('api.maiJobSyncAt = function', 'the job test hook is gone'),
                        ('POST", "/jobs"', 'main.js no longer submits to /jobs')):
        if needle not in code_only:
            problems.append('v0.8.0: ' + why)
    for needle, why in (('function sidecarStaleHint(',
                         'the stale-sidecar hint helper is gone'),
                        ('no such endpoint',
                         'the 404 no such endpoint branch is gone')):
        if needle not in code_only:
            problems.append('v0.9.1: ' + why)
    for needle, why in (('function saveUndoTo(',
                         'the on-disk undo snapshot writer is gone'),
                        ('function undoFromDisk(',
                         'the disk undo path is gone'),
                        ('undoStack.push({b: b, code: String(b.code), snap: snapName})',
                         'writeBuild no longer records the snapshot name')):
        if needle not in code_only:
            problems.append('v0.10.0: ' + why)
    for needle, why in (('function conflictGate(',
                         'the pre-write conflict gate is gone'),
                        ('function rememberOrig(',
                         'jobSubmit no longer records the code it saw at submit time'),
                        ('JOB_ORIG[k] = String(b.code)',
                         'the conflict baseline is not the submit-time code')):
        if needle not in code_only:
            problems.append('v0.10.1: ' + why)
    for needle, why in (('function llmText(',
                         'the model-tier status text is gone'),
                        ('function showPickList(',
                         'the model picker is gone'),
                        ('setPasswordMode(true)',
                         'the api key field is not masked any more'),
                        ('llmSave(LLM_KEY_KEY',
                         'the api key is no longer persisted from the settings page'),
                        ('httpJson("POST", "/models"',
                         'the model list no longer comes from the sidecar (https needs it)')):
        if needle not in code_only:
            problems.append('v0.11.0: ' + why)
    for needle, why in (('function llmSync(',
                         'the model tier no longer re-syncs after a sidecar restart'),
                        ('function llmProbe(',
                         'the model endpoint self-test button is gone'),
                        ('"/llm/probe"',
                         'the endpoint probe no longer goes through the sidecar')):
        if needle not in code_only:
            problems.append('v0.12.1: ' + why)
    for needle, why in (('function writeMany(',
                         'the transactional batch write is gone'),
                        ('function undoBatchFromDisk(',
                         'a batch undo no longer restores the whole batch'),
                        ('batch: String(batch || "")',
                         'an undo snapshot no longer records its batch'),
                        ('api.maiWriteManyProbe = function',
                         'the batch write probe is gone')):
        if needle not in code_only:
            problems.append('v0.12.0: ' + why)
    for needle, why in (('function openQuickJob(',
                         'F3 no longer opens the single description box'),
                        ('return openQuickJob(r);',
                         'selFinish stopped going through the description box'),
                        ('function drawJobButtons(',
                         'the running-job buttons in the box corner are gone'),
                        ('jobBox !== null && jobBox.r',
                         'drawSelection stopped keeping the box after release'),
                        ('function jobBoxTick(',
                         'the retained box is no longer polled for job states'),
                        ('function jobButtonsClick(',
                         'the corner buttons are not clickable any more'),
                        ('function openJobManager(',
                         'the unified job manager window is gone'),
                        ('"/jobs?all=1&limit=',
                         'the manager window no longer reads the job history')):
        if needle not in code_only:
            problems.append('v0.12.5: ' + why)
    if 'return dumpRegion(r.x1, r.y1, r.x2, r.y2);' in code_only:
        problems.append('v0.12.5: F3 still opens the region snapshot panel on release')
    for needle, why in (('function jobButtonLayout(',
                         'the button geometry is inlined again -- text and hit box will drift apart'),
                        ('font.setUseIntegerPositions(false)',
                         'the badge draws with integer positions -- at 1/camerascale the text '
                         'snaps away from the box corner'),
                        ('api.maiBadgeRect = function',
                         'the badge geometry probe is gone')):
        if needle not in code_only:
            problems.append('v0.12.6: ' + why)
    for needle, why in (('function closeJobBox(',
                         'the box can no longer be closed on purpose'),
                        ('jobBoxWritten(b)',
                         'a successful write no longer tells the box it was written'),
                        ('api.maiJobBoxClose = function',
                         'the explicit-close probe is gone')):
        if needle not in code_only:
            problems.append('v0.12.7: ' + why)
    if 'JOB_BOX_LINGER_MS' in code_only:
        problems.append('v0.12.7: the box still times itself out -- it has to wait for a '
                        'write or for the player to close it')
    # v0.14.2: 作业框的操作区是两个固定像素尺寸的按钮；写入成功不再把框（和工作区）抹掉。
    for needle, why in (('var JOB_BTN_H = 18',
                         'the box buttons lost their fixed pixel height'),
                        ('F.rect(b.x, b.y, b.w, b.h)',
                         'the box buttons have no background any more'),
                        ('jobBox.written = (jobBox.written || 0) + 1',
                         'a successful write no longer keeps the box on screen'),
                        ('jobButtonsClick();',
                         'the update loop no longer listens for the box buttons')):
        if needle not in code_only:
            problems.append('v0.14.2: ' + why)
    if 'jobBox = null;\n            jobBadge = null;' in code_only.split('function jobBoxWritten')[1][:400]:
        problems.append('v0.14.2: a write still wipes the selection box and its workspace')

    # v0.12.8: 冲突基线必须连链接表一起比；联机写入被服务端丢掉时必须报「无权限」。
    for needle, why in (('function linkSig(',
                         'the conflict baseline no longer covers the link table'),
                        ('JOB_LINKS[k] = linkSig(b)',
                         'the link table is no longer recorded when a job is submitted'),
                        ('function permWhy(',
                         'the multiplayer permission gate is gone -- a rejected write will '
                         'look successful again'),
                        ('function writeCheckTick(',
                         'the write-back check is gone -- a dropped multiplayer write is silent'),
                        ('armWriteCheck(items[i].b',
                         'the batch write no longer arms the write-back check'),
                        ('无权限',
                         'the rejected-write message lost its 无权限 wording'),
                        ('api.maiLinkConflictProbe = function',
                         'the link-table conflict probe is gone'),
                        ('api.maiNetProbe = function',
                         'the multiplayer probe is gone')):
        if needle not in code_only:
            problems.append('v0.12.8: ' + why)
    # v0.12.9: a region snapshot has to say what else is in the area, and the stock has to
    # be counted per BUILDING (a 2x2 container is one building on four tiles).
    for needle, why in (('function stockOf(',
                         'the snapshot no longer reads stock off the building'),
                        ('function seqSize(',
                         'the Seq size trap is back -- Groups.unit.size is a METHOD in v160.7 '
                         '(EntityGroup re-declares size()), so i < seq.size never runs'),
                        ('seenIds[bid] !== true',
                         'stock is counted per tile again -- a 2x2 container would count 4x'),
                        ('Packages.mindustry.gen.Groups.unit',
                         'units are no longer read from Groups.unit (they are not in the grid)'),
                        ('typeof seq.rawSeq === "function"',
                         'the rawSeq() path is gone -- Groups.unit.get is undefined on the real '
                         'engine (EntityGroup hides Seq.get), so plain get(i) counts zero units'),
                        ('function unitStats(',
                         'the unit/enemy summary is gone'),
                        ('blocks: blocks, items: items, liquids: liquids, stores: stores',
                         'the snapshot dropped the new blocks/items/liquids/stores sections')):
        if needle not in code_only:
            problems.append('v0.12.9: ' + why)
    # v0.13.0: 作业启用/完成必须播报到聊天框，格式 MAI<逻辑徽标> Status (x,y): info。
    # 图标只能走 UI.formatIcons(":logic:")（聊天框用 Fonts.def 画真 glyph，不认 [logic] 标签）。
    for needle, why in (('if(r === null){ jobBadge = null; jobWidgetHide(); return; }',
                         'v0.15.2: closing the box must also drop the HUD widget, or the '
                         'other two buttons stay on screen'),
                        ('function chatLine(',
                         'the chat status line builder is gone'),
                        ('function chatSay(',
                         'nothing posts job status to the chat box any more'),
                        ('Packages.mindustry.gen.Call.sendChatMessage(s)',
                         'v0.15.2: the status must go out as a real chat message, otherwise '
                         'only the host sees it'),
                        ('function chatPretty(',
                         'v0.15.2: :logic: must be resolved through UI.formatIcons before it '
                         'is sent'),
                        ('Packages.mindustry.core.UI.formatIcons(":logic:")',
                         'the logic icon is no longer resolved to a real glyph -- the chat box '
                         'draws with Fonts.def and does not understand [logic] tags'),
                        ('jobChatStart(b, String(res.id), desc)',
                         'submitting a single job no longer announces Generating'),
                        ('jobChatStart(t.b, jid, desc)',
                         'a batch submit no longer announces Generating per processor'),
                        ('jobChatDone(jobBox.jobs[want])',
                         'a finished batch job is no longer announced'),
                        ('if(isJobTerminal(job.state)){',
                         'a finished single job is no longer announced'),
                        # v0.13.1: the autostart has to be more than a helper nobody calls
    ('function ensureSidecar(',
     'the mod must be able to start the packaged sidecar itself'),
    ('function sidecarTick(',
     'the spawn needs a poll loop -- ProcessBuilder output goes to sidecar.log'),
    ('new Packages.java.lang.ProcessBuilder(arr)',
     'Runtime.exec splits paths on spaces; "Program Files (x86)" would break it'),
    ('pb.redirectOutput(new Packages.java.io.File(',
     'an unread pipe blocks the child once it has written 64 KB'),
    ('ensureSidecar(true); }catch(e){ log("sidecar autostart skipped at load',
     'v0.14.4: the sidecar must start with the game, not when a panel opens'),
    ('ensureSidecar(true); }catch(e2){ log("sidecar autostart skipped',
     'v0.14.4: ClientLoadEvent must try again once the player is in a game'),
    ('sidecarTick();',
     'the update hook must drive the poll loop'),
    ('api.maiSidecarProbe = function',
     'headless probe for the autostart state machine'),
    ('api.maiChatProbe = function',
                         'the chat format probe is gone')):
        if needle not in code_only:
            problems.append('v0.13.0: ' + why)
    for needle, why in (
        ('var managerFilter = {text: "", onlyBad: false, limit: 100};',
         'v0.15.1: the job manager lost its filter state'),
        ('function jobHit(job){',
         'v0.15.1: the manager list no longer filters'),
        ('function jobFollowAsk(){',
         'v0.15.1: the box lost its follow-up entry'),
        ('{id: "follow", label: "续聊"}',
         'v0.15.1: the follow button is missing from the box corner'),
        ('var LAST_HINT = {text: "", at: 0};',
         'v0.15.1: the last hint is not kept any more'),
        ('LAST_HINT.text = String(text);',
         'v0.15.1: hint() no longer records the last hint'),
        ('function hintCopy(){',
         'v0.15.1: the last-hint copy button is gone'),
    ):
        if needle not in code_only:
            problems.append(why + ' (missing: ' + needle + ')')

    for needle, why in (('var JOB_WHY = {',
                         'the failure-class wording table is gone'),
                        ('function jobWhyLines(',
                         'the human explanation of a failure is gone'),
                        ('function jobEvidenceLines(',
                         'the call evidence block is gone'),
                        ('function jobLastAttempt(',
                         'the attempts log is not read any more'),
                        ('function jobAssumptionLines(',
                         'the job view stopped showing what the model assumed'),
                        ('function jobCostLines(',
                         'the job view stopped showing the cost report'),
                        ('api.maiJobPanelProbe = function',
                         'headless probe for assumptions + cost'),
                        ('UNREACHABLE = 永远执行不到的语句',
                         'the cost block no longer says what UNREACHABLE means')):
        if needle not in code_only:
            problems.append('v0.13.2: ' + why)
    for needle, why in (('function openWorkspaceDialog(',
                         'the workspace snapshot dialog is gone'),
                        ('function renderWorkspaces(',
                         'the workspace list renderer is gone'),
                        ('function wsRestoreBox(',
                         'restoring a workspace selection box is gone'),
                        ('function wsExportText(',
                         'exporting a workspace to JSON is gone'),
                        ('function wsImportText(',
                         'importing a workspace from JSON is gone'),
                        ('function wsImprovePayload(',
                         'the improve payload builder is gone'),
                        ('job.mode = "improve";',
                         'improve jobs no longer switch the job mode'),
                        ('function blueprintList(',
                         'the blueprint list (null-safe) is gone'),
                        ('api.maiWorkspace2Probe = function',
                         'headless probe for the workspace dialog helpers'),
                        ('d.buttons.button("工作区快照"',
                         'no dialog offers a way into the workspace list')):
        if needle not in code_only:
            problems.append('v0.14.1: ' + why)
    # v0.19.0: the workspace screen is a list + detail pair with a world layer and a
    # release that asks first; losing any of these brings back the old one-column box.
    for needle, why in (('function wsRowParts(',
                         'the workspace card row parts are gone'),
                        ('function wsDetailModel(',
                         'the workspace detail model is gone'),
                        ('function wsFilterList(',
                         'the workspace filter is gone'),
                        ('function wsSortList(',
                         'the workspace sort is gone'),
                        ('function wsGhostRects(',
                         'the world-layer boxes lost their geometry'),
                        ('function drawWorkspaceGhosts(',
                         'the workspace boxes are no longer drawn in the world'),
                        ('function wsAskRelease(',
                         'releasing a workspace no longer asks first'),
                        ('showConfirm("释放工作区"',
                         'the release confirmation text is gone'),
                        ('wsUI.open = false',
                         'closing the workspace dialog no longer hides the world boxes'),
                        ('api.maiWorkspace3Probe = function',
                         'headless probe for the workspace UI')):
        if needle not in code_only:
            problems.append('v0.19.0: ' + why)
    # v0.19.3（用户 m01237「按钮太宽把标题挤到右边」）：像素和 scene 单位必须换算，
    # 对话框宽度必须按 scene 单位算；这两条掉了就会再退回「对话框比窗口宽 Scl 倍」。
    for needle, why in (('function wsSclFactor(',
                         'pixels are no longer converted to scene units'),
                        ('function wsScreenSize(',
                         'the dialog size is back to raw pixel width'),
                        ('function wsSceneUnits(',
                         'the pixel-to-scene conversion helper is gone'),
                        ('function wsDialogPlan(',
                         'the dialog size planner is gone'),
                        ('wsUI.busyList',
                         're-entrant list refreshes can stack two sets of widgets')):
        if needle not in code_only:
            problems.append('v0.19.3: ' + why)
    # v0.19.4（用户 m01690「标题居中导致按钮被拉伸」）：标题必须左对齐，按钮宽度必须有上限
    # （chips 与主按钮以前是「四等分 / 半栏」，宽屏下就是被拉伸的样子）。
    for needle, why in (('d.title.setAlignment(Packages.arc.util.Align.left)',
                         'the dialog title is centered again and widens the whole row'),
                        ('function wsChipWidth(',
                         'filter chips are back to equal-width stretching'),
                        ('function wsPrimaryWidth(',
                         'the primary action buttons are back to half-pane width'),
                        ('d.buttons.left()',
                         'the bottom button bar is centered again')):
        if needle not in code_only:
            problems.append('v0.19.4: ' + why)
    # v0.19.5（用户 m01884「MAI 的左栏仍然没变…按钮/输入框还是被拉得很宽，占满一整行」）：
    # 左栏控件按文字宽度走，不再「栏宽 - 固定宽」铺满一行；另外留一个几何自检 dump，
    # 真机排版再出问题时直接读 last_log.txt 的 [mai] ws geom: 数字，不用靠截图猜。
    for needle, why in (('function wsGeomDump(',
                         'the on-screen geometry dump is gone'),
                        ('wsUI.needGeom = true',
                         'the geometry dump is never armed when the dialog opens'),
                        ('bar.add(tf).width(150)',
                         'the search field is stretched across the whole row again')):
        if needle not in code_only:
            problems.append('v0.19.5: ' + why)
    # v0.19.6（用户 m02101「标题还是居中的」）：Arc 的 cell.align 默认 0 就落到 Table.java:1249 的居中分支，
    # 不继承表的 align —— 卡片色条没写 .left() 会被副行撑宽的第 0 列居中，名字被挤到最右；
    # 标题行则是 add(title).expandX().fillX()（Dialog.java:78）撑满整行，文字飘到中间。
    for needle, why in (('card.image(Tex.whiteui).width(4).height(26).color(col).left()',
                         'the card status strip is centred inside its column again'),
                        ('.left().padLeft(12).colspan(2)',
                         'the card sub-line sits in column 0 again and widens the strip column'),
                        ('d.titleTable.clearChildren()',
                         'the dialog title cell is filled again (the title drifts to the middle)'),
                        ('d.cont.left().top()',
                         'the dialog content table is centred again'),
                        ('try{ x = Math.round(Number(e.x)); }catch(e2){ }',
                         'the geometry dump reads getX() again (every x is -1)'),
                        ('["mismatch", "不符", "地图不符（工作区记录的地图跟当前地图不一样）"]',
                         'the filter chips are long enough to get truncated again')):
        if needle not in code_only:
            problems.append('v0.19.6: ' + why)
    # v0.20.0 (user m02407 / m02414 / m02455): the manager grows a "new chat" entry, the chat
    # dialog is list | 2px separator | transcript, and a running turn is polled from the frame hook.
    for needle, why in (('function openChatDialog(',
                         'the free-chat dialog is gone'),
                        ('body.image(Tex.whiteui).width(2)',
                         'the separator between the chat list and the transcript is gone'),
                        ('function chatTick(',
                         'nothing polls a running chat turn any more'),
                        ('httpJson("POST", "/chat"',
                         'the dialog no longer posts a turn to the sidecar'),
                        ('＋ 新建对话',
                         'the manager lost its new-chat entry'),
                        ('CHAT_UI.polling',
                         'the chat polling flag is gone')):
        if needle not in code_only:
            problems.append('v0.20.0: ' + why)
    # v0.20.1 (user m02850 "没有看到哪里更新了"): the chat dialog used to be reachable only from the
    # job manager, which itself needs a submitted job -- add a direct entry on the blueprint page and in
    # the workspace snapshot dialog, so the new feature is actually visible.
    # v0.21.1（用户 m04334「合并控制面板和MAI对话吧」）：这两个入口已合成一个 —— 只查合并后的那个，
    # 原来的「MAI 对话」按钮与工作区底栏「＋ 对话」必须消失（改由标题行的模式分段开关承担）。
    if 'btns.button("MAI " + LOGIC_NAME' not in code_only:
        problems.append('v0.21.1: the blueprint page lost the merged logic-assistant entry')
    for needle, why in (('btns.button("MAI 对话"',
                         'the old separate "MAI 对话" button is back next to the merged entry'),
                         ('d.buttons.button("＋ 对话"',
                         'the workspace footer got its own chat button back (the mode switch replaces it)')):
        if needle in code_only:
            problems.append('v0.21.1: ' + why)
    # v0.20.2（用户 m03006「左边的新对话窗口太大了已经超出左边了」/ m03017「新对话应该在右边」/
    # m03682「现在的「＋ 新对话」按钮占满了整个左边的屏幕」）：
    # ① 宽度预算必须自洽（listW+gapW+detailW === availW，上一版漏了外壳 pad 3+3 就外溢）；
    # ② 「新对话」入口并到「会话列表」标题行、宽度固定，不再随栏宽拉伸；
    # ③ 底栏右对齐，新对话在最右；④ 对话窗也要有和 ws 一样的真机排版 dump。
    for needle, why in (('var gapW = 22;',
                         'the chat width budget no longer reserves the separator + shell pads'),
                         ('p.listW + p.gapW + p.detailW',
                         'the chat width budget identity is gone (the panes overflow again)'),
                         ('head.button("＋ 新对话"',
                         'the new-chat button is back to spanning the whole list column'),
                         ('＋ 在这里开一段新对话',
                         'the transcript pane lost its right-hand new-chat entry'),
                         ('d.buttons.right();',
                         'the chat dialog footer is no longer right-aligned'),
                         ('function chatGeomDump(',
                         'the chat dialog lost its real-layout dump (chatGeomDump)'),
                         ('CHAT_UI.needGeom = true;',
                         'nothing asks for the one-shot chat geometry dump any more')):
        if needle not in code_only:
            problems.append('v0.20.2: ' + why)
    for needle, why in (('width(Math.max(100, iw - 4))',
                         'the new-chat button is stretched across the list column again'),
                         ('d.cont.add(chatBar).growX()',
                         'the manager chat bar is stretched to the dialog edge again')):
        if needle in code_only:
            problems.append('v0.20.2: ' + why)
    # v0.20.3（用户 m03832「还是不对」）：Arc 的 Table 列宽是**跨行共享**的 —— 两栏行 + 输入行落在
    # 同一个 cont 里时，输入行（全宽 974）会把第 0 列顶成 980，三列相加 1754 ⇒ 窗口 1768、左栏贴左、
    # 底栏出屏。两栏必须包进只有一条列的 wrapper（body），输入行和 body 同属 cont 的唯一一列。
    for needle, why in (('var body = wsT();',
                         'the chat panes are no longer wrapped in a single-column table'),
                        ('body.pane(listT).width(plan.listW).height(availH).left()',
                         'the chat list pane lost its fixed width / left alignment'),
                        ('d.cont.add(body).width(Math.max(200, Number(plan.availW) - 6))',
                         'the chat pane row is no longer pinned to the dialog width budget'),
                        ('cols=',
                         'the chat geometry dump no longer reports the cont column count'),
                        ('budget=',
                         'the chat geometry dump no longer reports the width budget')):
        if needle not in code_only:
            problems.append('v0.20.3: ' + why)
    for needle, why in (('d.cont.pane(listT).width(plan.listW).height(availH);',
                         'the two panes are direct cont cells again (three columns, a 1768-wide window)'),
                        ('var cellR = d.cont.pane(detailT)',
                         'the detail pane is added straight to cont again (second full-width row wins the column)')):
        if needle in code_only:
            problems.append('v0.20.3: ' + why)
    # v0.21.0 档1（用户 m04199「能直接把工作区快照并入MAI对话里面吗」/ m04211「先做档1」）：
    # 两个窗口必须共用一套外壳 —— 标题左对齐 + 标题行右侧「对话 / 工作区」分段开关 + 开一个关另一个；
    # 各自的 new Dialog(...) / titleTable 重建不许再抄一份（v0.19.4 / v0.19.6 / v0.20.2 三次事故都是抄出两份）。
    for needle, why in (('function dualDialog(',
                         'the two dialogs no longer share one shell constructor'),
                         ('function dualChrome(',
                         'the shared title bar (left title + mode switch) is gone'),
                         ('function dualSwitch(',
                         'the mode switch no longer closes the other dialog'),
                         ('function dualHideOthers(',
                         'nothing hides the sibling dialog before opening one'),
                         ('var d = dualDialog("chat");',
                         'the chat dialog builds its own frame again'),
                         ('var d = dualDialog("ws");',
                         'the workspace dialog builds its own frame again'),
                         ('seg.button(dualModeCn("chat")',
                         'the title row lost its chat segment button'),
                         ('seg.button(dualModeCn("ws")',
                         'the title row lost the workspace segment button'),
                         ('dualChrome(d, mode);',
                         'the shared frame is built but never decorated')):
        if needle not in code_only:
            problems.append('v0.21.0: ' + why)
    for needle, why in (('new Packages.arc.scene.ui.Dialog("Mlog辅助 · 逻辑助手"',
                         'a dialog is hand-rolled again instead of going through dualDialog()'),
                         ('new Packages.arc.scene.ui.Dialog("Mlog辅助 · 自由对话 v"',
                         'the chat dialog is back to a hand-rolled Dialog (a second copy of the title fix)'),
                         ('new Packages.arc.scene.ui.Dialog("Mlog辅助 · 工作区快照 v"',
                         'the workspace dialog is back to a hand-rolled Dialog')):
        if needle in code_only:
            problems.append('v0.21.0: ' + why)
    # v0.21.1（用户 m04334：①「工作区的按钮宽度不够，被换行了」②「合并控制面板和MAI对话」③「合并之后要加个逻辑标志并染上主题色」）
    # ① 分段按钮的宽度必须按字数给（64 装不下「工作区」+ 内边距 ⇒ Label 折行），并配 ellipsis 兜底；
    # ② 只剩一个入口 openLogicAssistant()，按上次用的模式打开；③ 标题行与入口按钮前各有一个主题色方块当逻辑标志。
    for needle, why in (('function dualSegWidth(',
                         'the mode segments go back to one fixed width (the 3-char label wraps again)'),
                         ('width(dualSegWidth("ws"))',
                         'the workspace segment no longer sizes itself by its label'),
                         ('LOGIC_NAME = "逻辑助手"',
                         'the merged window lost its single name'),
                         ('d.titleTable.image(Tex.whiteui, themeColor())',
                         'the logic mark in the title row is gone (or no longer theme-coloured)'),
                         ('btns.image(Tex.whiteui, themeColor())',
                         'the entry button lost the theme-coloured logic mark'),
                         ('function openLogicAssistant(',
                         'the single entry point (open last mode) is gone'),
                         ('api.maiLogicAssistant',
                         'the console probe for the merged entry is gone')):
        if needle not in code_only:
            problems.append('v0.21.1: ' + why)
    for needle, why in (('width(64).padLeft(6)',
                         'the segments are back to a 64px fixed width, so 「工作区」wraps onto two lines'),):
        if needle in code_only:
            problems.append('v0.21.1: ' + why)
    # v0.22.0 档3（用户 m04334「完成之后继续做档3」）：工作区快照随每轮对话下行，侧车只读地看它。
    # ① 快照必须裁剪（每台代码 6000、总预算 120000、作业只留最近 12 条），否则一次请求能到几十 MB；
    # ② 每轮 POST /chat 都要带上 workspaces + map；③ 对话面板要说明助手能看到几个工作区。
    for needle, why in (('function wsDigestOf(',
                         'the workspace digest (budget + trim) is gone: the request would carry every byte'),
                         ('req.workspaces = wsDigest()',
                         'a chat turn no longer carries the workspace snapshot'),
                         ('req.map = wsMapName()',
                         'a chat turn no longer tells the sidecar which map the workspaces belong to'),
                         ('api.maiWsDigestProbe',
                         'the digest probe is gone (the budget rules are untested)'),
                         ('个工作区（ws_list / ws_get / ws_search，只读）',
                         'the transcript no longer says how many workspaces the assistant can see')):
        if needle not in code_only:
            problems.append('v0.22.0: ' + why)
    for needle, why in (('function jobRetryHead(',                          'a retrying job no longer labels its findings as the last attempt'),
                        ('function jobFindingLines(',
                         'the truncation gloss helper is gone'),
                        ('上一次尝试的问题',
                         'the retry header lost its wording')):
        if needle not in code_only:
            problems.append('v0.12.6: ' + why)
    # v0.14.4（用户 2026-11-01）：模板档删掉，面板删掉，产品路径不许再碰 /plan 与 /compose。
    for needle, why in (('openPanel', 'v0.14.4: the template panel should be gone'),
                        ('llmUseTemplate', 'v0.14.4: the template tier switch should be gone'),
                        ('"直接生成"', 'v0.14.4: the /plan fallback button should be gone'),
                        ('"/plan"', 'v0.14.4: the mod must not call /plan any more'),
                        ('"/compose"', 'v0.14.4: the mod must not call /compose any more'),
                        ('ui.chatfrag.addMessage',
                         'v0.15.2: addMessage only writes to the local chat box -- the status '
                         'has to be a real sent message')):
        if needle in code_only:
            problems.append(why)
    for needle, why in (('var CHAT_SEND_KEY = "mlogauxiliary-chat-send"',
                         'v0.14.4: the chat broadcast setting key is gone'),
                        ('function chatSendOn(', 'v0.14.4: the chat broadcast switch is gone'),
                        ('if(chatSendOn() && netActive()){',
                         'v0.14.4: chatSay must honour the broadcast switch (default off)'),
                        ('function jobWidgetBuild(', 'v0.14.4: the native job widget is gone'),
                        ('Packages.mindustry.ui.Styles.defaultt',
                         'v0.14.4: the job widget must use the vanilla button style'),
                        ('ui.hudGroup', 'v0.14.4: the widget must live on the HUD stage'),
                        ('function llmAutoSync(', 'v0.14.4: the model profile must auto-attach'),
                        ('未配置模型：先在', 'v0.14.4: an unconfigured model must be said up front'),
                        ('setPasswordCharacter(', 'v0.14.4: the key must be masked with a real glyph'),
                        ('显示密钥', 'v0.14.4: there must be a way to reveal the key on demand'),
                        # v0.18.3（用户 m11124）
                        ('var SETTINGS_LIVE = false;',
                         'v0.18.3: the settings page must know it is live'),
                        ('section(t, "诊断与最近提示")',
                         'v0.18.3: diagnostics belong on the hint-log page'),
                        ('lastHintLbl = hbtn.button("复制"',
                         'v0.18.3: the hint page keeps exactly one copy button'),
                        ('d.buttons.button("撤销上次写入"',
                         'v0.18.3: undo must live in the MAI control panel')):
        if needle not in code_only:
            problems.append(why)
    # 设置页顺序：运行环境必须排在最后（用户 2026-11-01「运行环境应该放在最后」）
    # v0.18.3（用户 m11124）：「快捷动作页，说明页毫无意义，诊断全部移动到最近提示页」
    # ⇒ 段序变成 大模型 / 聊天播报 / 框选控件 / 外观 / 写入 / 热键 / 诊断与最近提示 / 运行环境。
    # 热键那两条是 kv 行（没有自己的 section），所以不在这里。
    order = ['section(t, "大模型', 'section(t, "聊天播报', 'section(t, "框选控件',
             'section(t, "外观', 'section(t, "写入',
             'section(t, "诊断与最近提示', 'section(t, "运行环境']
    for gone, why in (('section(t, "快捷动作', 'v0.18.3: 快捷动作段应该已经删掉'),
                      ('section(t, "说明', 'v0.18.3: 说明段应该已经删掉'),
                      ('section(t, "本地快检', 'v0.18.3: 本地快检段应该并进诊断页'),
                      ('section(t, "最近提示', 'v0.18.3: 最近提示段应该已并入诊断页'),
                      ('button("清空"', 'v0.18.3: 提示页只留一个复制按钮')):
        if gone in code_only:
            problems.append(why)
    pos = -1
    for key in order:
        at = code_only.find(key)
        if at < 0:
            problems.append('v0.14.4: settings section missing: ' + key)
            break
        if at < pos:
            problems.append('v0.14.4: settings sections out of order at ' + key)
            break
        pos = at
    # v0.14.5（用户 2026-11-01）：「做一个hud倍率自己调就好」——框角控件的大小由玩家自己
    # 在设置页加减（HUD_SCALE_KEY，0.5×–2.0×），按钮 / 字号 / 边距一起缩放，改完要重建控件。
    for needle, why in (('var HUD_SCALE_KEY  = "mlogauxiliary-hud-scale"',
                         'v0.14.5: the HUD scale setting key is gone'),
                        ('function hudScale(', 'v0.14.5: the HUD scale reader is gone'),
                        ('function hudScaleStep(', 'v0.14.5: the +/- stepper is gone'),
                        ('function hudScaleRefresh(', 'v0.14.5: the settings label must refresh'),
                        ('function jobWidgetDrop(',
                         'v0.14.5: changing the scale must rebuild the widget'),
                        ('S.scl(84) * hk, S.scl(40) * hk',
                         'v0.14.5: the widget buttons must scale with the setting'),
                        ('lbl.setFontScale(hk)', 'v0.14.5: the status label must scale too'),
                        ('var edge = JOB_BTN_EDGE * hk2',
                         'v0.14.5: the widget offset must scale too')):
        if needle not in code_only:
            problems.append(why)
    # v0.14.6（用户 m06631）：游戏内导入的模组是 zip，`m.root` 是 ZipFi，随包的
    # logicd.exe 只是一个 zip 条目 —— 必须解到数据目录再从磁盘起进程。
    for needle, why in (
                        ('var SIDE_IDLE_EXIT = 45;',
                         'the idle-exit constant is gone'),
                        ('function sidecarLogPort(',
                         "the module no longer reads the sidecar's chosen port"),
                        ('function sidecarUrl(',
                         'the module no longer routes to the chosen port'),
                        ('function sidecarPing(',
                         'the heartbeat is gone'),
                        ('function sidecarVersionMismatch(',
                         'the build check is gone'),
                        ('function sidecarOurs(',
                         'the own-build check is gone'),
                        ('function sidecarRetire(',
                         'the retire path is gone'),
                        ('arr[2] = "0";',
                         'the sidecar is spawned on a fixed port again'),
                        ('arr[4] = String(SIDE_IDLE_EXIT);',
                         'the sidecar is spawned without --idle-exit'),
                        ('httpRaw(method, sidecarUrl() + path, body)',
                         'requests still go to the hand-filled address'),
                        ('sidecarPing();',
                         'the per-frame hook no longer pings'),
                        ('httpJson("POST", "/ping", "{}"',
                         'v0.15.3: the heartbeat body must be JSON text, not an object')
                       ):
        if needle not in code_only:
            problems.append('v0.15.0: ' + why)
    # v0.15.3 真机回归：{} 会被 httpRaw 拼成 "[object Object]"，sidecar 回 400，
    # 心跳被判失败、45 秒后进程自退（「已就绪但投不出去」）。这一行必须不再出现。
    pinned_red = (('"/ping", {}', 'v0.15.3: the heartbeat is back to a non-JSON body'),
                  ('"/shutdown", {}', 'v0.15.3: the retire request is back to a non-JSON body'))
    for needle, why in pinned_red:
        if needle in code_only:
            problems.append(why)
    # v0.16.0 B1：框选存蓝图必须走原版接口（用户口径：尽量调用原版保存蓝图的接口）：
    # create() 复制方块 -> tags 里写 name（否则存成 unknown.msch）-> add() -> 回读 all() 校验。
    # add() 出异常只 ui.showException 不抛，所以少了回读就等于没验过。
    for needle, why in (('function blueprintSaveBox(r, name, cb){',
                         'v0.16.0: the save-as-blueprint path is gone'),
                        ('s = Vars.schematics.create(Number(r.x1), Number(r.y1), '
                         'Number(r.x2), Number(r.y2));',
                         'v0.16.0: the vanilla Schematics.create() call is gone'),
                        ('s.tags.put("name", nm);',
                         'v0.16.0: the blueprint name tag is not written (saves as unknown.msch)'),
                        ('Vars.schematics.add(s);',
                         'v0.16.0: the vanilla Schematics.add() call is gone'),
                        ('if(after <= before && !blueprintHasName(nm)){',
                         'v0.16.0: the read-back check after add() is gone')):
        if needle not in code_only:
            problems.append(why)
    # v0.16.1 B2/B4: 蓝图页入口、「详细」页续聊、签名上下文解码 —— 每一环掉了都等于功能没了。
    for needle, why in (
            ('function sigMetaOf(code){',
             'v0.16.1: the signed-payload meta decoder is gone'),
            ('Packages.java.util.Base64.getUrlDecoder()',
             'v0.16.1: the Java base64url decoder is gone (Rhino has no atob)'),
            ('new Packages.java.util.zip.InflaterInputStream(',
             'v0.16.1: the zlib branch of the payload decoder is gone'),
            ('function blueprintCtxOf(name){',
             'v0.16.1: reading a blueprint context out of its tiles is gone'),
            ('btns.button("MAI " + LOGIC_NAME, function(){',
             'v0.18.1: the blueprint page entry button is gone'),
            ('info.buttons.button("MAI", function(){ openBlueprintChat(nm); }).width(110);',
             'v0.17.1: the schematic info page MAI button is gone'),
            ('mode: "improve", desc: text, links: null, ipt: 2, origin: null,',
             'v0.16.1: blueprint chat must post an improve job'),
            ('try{ Vars.schematics.saveChanges(sch); }',
             'v0.16.1: writing the new code back into the blueprint file is gone'),
            ('if(after === null || String(after.code) !== String(code)){',
             'v0.16.1: the read-back check after saveChanges is gone')):
        if needle not in code_only:
            problems.append(why)
    # v0.16.1 B2/B4: 蓝图页入口、「详细」页续聊、签名上下文解码 —— 每一环掉了都等于功能没了。
    for needle, why in (
            ('function sigMetaOf(code){',
             'v0.16.1: the signed-payload meta decoder is gone'),
            ('Packages.java.util.Base64.getUrlDecoder()',
             'v0.16.1: the Java base64url decoder is gone (Rhino has no atob)'),
            ('new Packages.java.util.zip.InflaterInputStream(',
             'v0.16.1: the zlib branch of the payload decoder is gone'),
            ('function blueprintCtxOf(name){',
             'v0.16.1: reading a blueprint context out of its tiles is gone'),
            ('btns.button("MAI " + LOGIC_NAME, function(){',
             'v0.18.1: the blueprint page entry button is gone'),
            ('info.buttons.button("MAI", function(){ openBlueprintChat(nm); }).width(110);',
             'v0.17.1: the schematic info page MAI button is gone'),
            ('mode: "improve", desc: text, links: null, ipt: 2, origin: null,',
             'v0.16.1: blueprint chat must post an improve job'),
            ('try{ Vars.schematics.saveChanges(sch); }',
             'v0.16.1: writing the new code back into the blueprint file is gone'),
            ('if(after === null || String(after.code) !== String(code)){',
             'v0.16.1: the read-back check after saveChanges is gone')):
        if needle not in code_only:
            problems.append(why)
    for needle, why in (('function sidecarIsZip(', 'v0.14.6: the zip-mod check is gone'),
                        ('function sidecarCopyTree(',
                         'v0.14.6: the zip-mod extraction is gone'),
                        ('function sidecarBinDir(',
                         'v0.14.6: the sidecar bin dir resolver is gone'),
                        ('Vars.dataDirectory.child("mlogauxiliary").child("sidecar")',
                         'v0.14.6: a zip mod must extract the sidecar into the data dir'),
                        ('if(!sidecarIsZip()){',
                         'v0.14.6: a disk install must not be extracted every launch')):
        if needle not in code_only:
            problems.append(why)
    # v0.4.4 red lines: the transport must stay a raw socket, and the settings category
    # must show the vanilla logic badge (Icon.logic).
    if 'openConnection(' in code_only:
        problems.append('main.js calls URL.openConnection() again -- Rhino cannot call methods '
                        'on sun.net.www.protocol.http.HttpURLConnection (IllegalAccessException)')
    if 'new Packages.java.net.Socket()' not in code_only:
        problems.append('the sidecar transport is no longer a raw java.net.Socket')
    if 'Packages.mindustry.gen.Icon.logic' not in code_only:
        problems.append('the settings category lost the vanilla logic badge (Icon.logic)')
    # v0.4.4: try/finally with a nested try/catch inside the finally body makes the v160.7
    # Rhino codegen die (ArrayIndexOutOfBoundsException "arraycopy ..." / "invalid bytecode"),
    # and Scripts.runConsole hands that error back as a String, so the whole mod loads
    # silently dead.  Keep the transport on try/catch + an explicit close.
    if '}finally{' in code_only:
        problems.append('main.js introduced try/finally again -- the v160.7 Rhino codegen '
                        'throws on finally bodies that contain try/catch')
    if 'try{ if(sock !== null){ sock.close(); } }catch(e1){}' not in code_only:
        problems.append('the socket transport lost its explicit socket close')
    # v0.4.5: a refused socket is the everyday "sidecar isn't running" case, so the
    # panel must translate it instead of pasting the Java stack, and a link name the
    # code reads but nothing answers to must be named in the panel (p.missing).
    if 'Connection refused' not in code_only:
        problems.append('main.js stopped explaining a refused sidecar connection')
    if 'p.missing' not in code_only:
        problems.append('the region panel stopped reporting unbound link names (p.missing)')
    # v0.4.6: the payload stores link coordinates relative to the processor
    # (LogicBlock.java:304-307), so every request that carries links has to say where
    # the processor is.  Without the origin the game keeps the link entries but none of
    # them resolves (valid=false), which is silent at run time.
    if 'function originOf(' not in code_only:
        problems.append('main.js lost originOf() -- link coordinates would be written '
                        'as absolute numbers into a relative payload')
    if 'origin: originOf(b)' not in code_only:
        problems.append('/plan or /compose stopped sending the processor origin')
    # v0.4.8: F3 drag-select.  F8 is the vanilla console (Binding.java:107
    # console = KeyCode.f8) and F9/F10 are taken too, so the box lives on F3; the
    # highlight is drawn in world space instead of becoming a scene element (v0.4.1
    # crashed exactly by inventing a result area in the scene tree).
    for needle, why in (('var KEY_SELECT  = ["f3"];', 'F3 is no longer the select key'),
                        ('function selRect(', 'selRect() is gone -- drag normalisation lost'),
                        ('function mouseTile(', 'mouseTile() is gone'),
                        ('function drawSelection(', 'the drag highlight is gone'),
                        ('Events.run(ET.Trigger.draw',
                         'the drag highlight is no longer drawn in world space'),
                        ('function dumpRegion(', 'dumpRegion() is gone'),
                        ('api.maiSelRect = function', 'the selRect test hook is gone')):
        if needle not in code_only:
            problems.append('v0.4.8: ' + why)
    # v0.4.9: F3 must not open a dialog (it ate the mouse: user reported 2026-10-30 that
    # the popup blocked dragging), and the drag must be anchored at the mouse-down tile.
    for needle, why in (('function hint(', 'the non-blocking hint() is gone'),
                        ('function selPress(', 'selPress() is gone -- the drag has no press anchor'),
                        ('function selDrawArgs(', 'selDrawArgs() is gone'),
                        ('api.maiSelPress = function', 'the selPress test hook is gone')):
        if needle not in code_only:
            problems.append('v0.4.9: ' + why)
    if 'popup("Mlog辅助 · 框选"' in code_only:
        problems.append('v0.4.9: F3 still pops a Dialog -- a Dialog takes the mouse and the drag never starts')
    # v0.5.0: F3 is the left mouse button (user 2026-10-30: "f3就是鼠标左键"), and the
    # highlight copies the vanilla rectangle recipe (InputHandler.java:1758-1795):
    # Lines.stroke(2) + accentBack/accent double outline, anchored at the lower-left
    # corner. No arming step, no translucent fill, no keyTap (that only fires on release).
    for needle, why in (('var selHolding = false', 'the hold state is gone'),
                        ('function drawSizeText(', 'the vanilla size label is gone'),
                        ('L.stroke(2);', 'the 2px outline stroke is gone'),
                        ('Pal.accentBack', 'the accentBack shadow line is gone'),
                        # v0.18.2（用户 m10938 / m10943）：选框用主题色、框内高亮用强调色。
                        # v0.18.3（用户 m11320 / m11340）：高亮按建筑画整块外框（随处理器尺寸缩放）、同台只画一次。
                        ('D.color(themeColor());', 'the selection frame colour is not the theme colour'),
                        ('D.color(emphColor());', 'the logic highlight colour is not the emphasis colour'),
                        ('L.rect(bx - bsize * 4, by - bsize * 4, bsize * 8, bsize * 8);',
                         'the logic highlight is not a whole-building rect'),
                        ('if(seenH[hid] === true){ continue; }',
                         'the logic highlight does not dedupe by building'),
                        ('if(seenProcs[pid] !== true){',
                         'the snapshot does not dedupe processors by building'),
                        ('var selDown = selKeyDown();', 'F3 is no longer read as a held button')):
        if needle not in code_only:
            problems.append('v0.5.0: ' + why)
    # v0.18.2（用户 m10938）：「允许在设置中调节主题色和强调色」——两个设置项与存取层都要在。
    for needle, why in (('var THEME_KEY = "mlogauxiliary-theme-color";',
                         'the theme colour setting key is gone'),
                        ('var EMPH_KEY = "mlogauxiliary-emph-color";',
                         'the emphasis colour setting key is gone'),
                        ('section(t, "外观");', 'the appearance section is gone'),
                        ('function colorRow(t, label, key, defHex, mk, expl){',
                         'the colour row builder is gone')):
        if needle not in code_only:
            problems.append('v0.18.2: ' + why)
    for needle, why in (('selArmed', 'the arming step is back -- F3 must start the drag itself'),
                        ('D.alpha(0.35)', 'the translucent fill is back -- vanilla is outline only'),
                        ('keyTap(Packages.arc.input.KeyCode[KEY_SELECT',
                         'F3 is read as a tap again -- it must be a held button')):
        if needle in code_only:
            problems.append('v0.5.0: ' + why)
    # v0.17.1（用户 m09822）：①框选键注册进原版「按键绑定」，②设置页标题文字居中，
    # ③蓝图「详细」页一个「MAI」按钮管上下文，④框选先读签名挂载的上下文。
    # 少了任何一条都等于回到用户报的问题，所以逐条钉住。
    for needle, why in (
            ('function selKeyBind(){',
             'the vanilla keybind registration is gone'),
            ('Packages.arc.input.KeyBind.add("mai-select"',
             'the mai-select keybind is no longer registered with KeyBind.add'),
            ('function selKeyCode(){',
             'reading the currently bound select key back out of KeyBind is gone'),
            ('var selDown = selKeyDown();',
             'the drag no longer reads the bound key as a held button'),
            ('function selKeyAxis(){',
             'v0.18.1: reading the KeyBind.Axis itself is gone'),
            ('if(ax.min !== null && ax.min !== undefined){ return Core.input.keyDown(ax.min) === true; }',
             'v0.18.1: an axis-form bind (key == null, min/max set) is ignored again '
             '-- that is exactly the "rebind does not take effect" bug'),
            ('kv(t, "框选键", selKeyName(), mk,',
             'v0.18.1: the settings page no longer shows which key is currently bound'),
            ('function listKeyTap(){',
             'v0.18.1: the list key (F2) is no longer read through the registered bind'),
            ('Packages.arc.input.KeyBind.add("mai-list", dflt, "mlogauxiliary")',
             'v0.18.1: the list key is no longer registered as a MAI keybind'),
            ('if(listKeyTap()){',
             'v0.18.1: the update hook no longer uses the rebindable list key'),
            ('kv(t, "清单键", listKeyName(), mk,',
             'v0.18.1: the settings page no longer shows the list key'),
            ('function alignText(el, align){',
             'the label alignment helper is gone (Cell.center() does not move the text)'),
            ('alignText(lbl, Packages.arc.util.Align.center);',
             'the settings title text is no longer centered'),
            ('alignText(sc.get(), Packages.arc.util.Align.center);',
             'section headings are no longer centered'),
            ('function sigCtxInRegion(targets){',
             'reading a workspace context out of the signature is gone'),
            ('function wsFromSig(r, sig){',
             'rebuilding a workspace from signature meta is gone'),
            ('function wsAttachCtx(ws, r, targets, desc){',
             'attaching a selection to an already-known workspace is gone'),
            ('var sig = sigCtxInRegion(targets);',
             'the F3 dialog no longer tries the signature before anything else')):
        if needle not in code_only:
            problems.append('v0.17.1: ' + why)
    for needle, why in (('hotBegin(', 'the self-made rebind capture is back -- use the vanilla keybind page'),
                        ('HOT_KEY', 'the self-made hotkey store is back -- KeyBind owns the value now')):
        if needle in code_only:
            problems.append('v0.17.1: ' + why)
    # v0.18.0（用户 m10072）：第一次启动弹「欢迎 + 快速配置」。
    # 三步都要在：验后端可用、填模型并启用、重启。少一步向导就没有意义，
    # 而多一次自动弹窗则是回归（标记没写/读了没判），所以正反都钉。
    if 'MAIWELCOME:ok' not in joined:
        problems.append('v0.18.0: the welcome page no longer builds: %r'
                        % [l for l in lines if l.startswith('MAIWELCOME:')][:1])
    elif 'seen=no' not in joined:
        problems.append('v0.18.0: the first-run flag reads back as already seen on a fresh config')
    for needle, why in (
            ('function welcomeBuild(t, mk){',
             'the welcome page layout is gone'),
            ('function welcomeTick(){',
             'nothing pops the welcome page on first launch any more'),
            ('var WELCOME_KEY = "mlogauxiliary-welcome-done";',
             'the first-run flag key is gone (the wizard would pop on every launch)'),
            ('welcomeMarkSeen();',
             'the wizard never records that it was shown'),
            ('"GET", "/health"',
             'step 1 no longer checks the sidecar health endpoint'),
            ('kind: "llm"',
             'step 2 no longer enables the model tier'),
            ('Core.app.exit()',
             'step 3 can no longer restart the game'),
            ('buttonRow(t, ["\u9996\u6b21\u914d\u7f6e\u5411\u5bfc"]',
             'the settings page lost its way back into the wizard')):
        if needle not in code_only:
            problems.append('v0.18.0: ' + why)
    # ---- P3 region snapshot, read back from the engine
    snap = None
    for l in text.splitlines():
        if l.startswith('MAISNAP:'):
            try:
                snap = json.loads(l[len('MAISNAP:'):])
            except Exception as exc:
                problems.append('maiSnapshot returned bad JSON: %s' % exc)
    if snap is None:
        problems.append('maiSnapshot printed nothing')
    else:
        bx, by = srv.base
        names = sorted(str(c.get('block')) for c in snap.get('grid') or [])
        # a 2x2 container is 4 tiles, the 1x1 switch is 1, the processor is 1:
        # every other tile in the box is empty and its block name is 'air'.
        want_names = ['container'] * 4 + ['micro-processor', 'switch']
        if names != want_names:
            problems.append('region grid is %r, wanted %r (empty tiles must be skipped)'
                            % (names, want_names))
        if snap.get('cells') != 6:
            problems.append('region cells: %r' % (snap.get('cells'),))
        procs = snap.get('processors') or []
        if len(procs) != 1 or str(procs[0].get('code')) != 'noop':
            problems.append('region processors: %r' % (procs,))
        elif procs[0].get('x') != bx or procs[0].get('y') != by:
            problems.append('region processor at %r, expected %r' % (procs[0], (bx, by)))
        # the box is clamped to the map: a processor at 0,0 asks for -1,-1
        want = {'x1': max(0, bx - 1), 'y1': max(0, by - 1),
                'x2': bx + 4, 'y2': by + 4}
        if snap.get('bounds') != want:
            problems.append('region bounds: %r' % (snap.get('bounds'),))
        if snap.get('truncated') is not False:
            problems.append('region truncated flag: %r' % (snap.get('truncated'),))
        # ---- v0.12.9: the snapshot now says what ELSE is in the area -- block counts,
        # stock, units, enemies -- and the stock must be per BUILDING, not per tile.
        blocks = snap.get('blocks') or {}
        if blocks.get('container') != 4 or blocks.get('micro-processor') != 1:
            problems.append('v0.12.9: snapshot block counts: %r' % (blocks,))
        if (snap.get('items') or {}).get('copper') != 100:
            problems.append('v0.12.9: a 2x2 container must count its 100 copper ONCE '
                            '(stock is per building, not per tile), got %r'
                            % (snap.get('items'),))
        stores = snap.get('stores') or []
        if len(stores) != 1:
            problems.append('v0.12.9: stores must hold one entry per building, got %r'
                            % (stores,))
        elif str(stores[0].get('block')) != 'container' or stores[0].get('x') != bx + 2:
            problems.append('v0.12.9: the store entry is at the wrong building: %r'
                            % (stores[0],))
        units = snap.get('units') or {}
        if units.get('total') != 1 or (units.get('types') or {}).get('dagger') != 1:
            problems.append('v0.12.9: snapshot units: %r' % (units,))
        if (units.get('teams') or {}).get('sharded') != 1:
            problems.append('v0.12.9: the unit team is not recorded: %r' % (units,))
        enemies = snap.get('enemies') or {}
        if enemies.get('units') != 0 or enemies.get('blocks') != 0:
            problems.append('v0.12.9: a headless engine has no local player, so "enemy" has '
                            'no meaning and both counts must be 0: %r' % (enemies,))

    # ---- v0.4.8 F3 drag-select: a backwards drag is the same box, and the summary is
    # produced without the sidecar (a headless engine has no Vars.ui, so the post is
    # skipped rather than throwing inside a daemon thread).
    bx, by = srv.base
    rect = rect2 = dump = None
    for l in text.splitlines():
        if l.startswith('MAIRECT:'):
            try:
                rect = json.loads(l[len('MAIRECT:'):])
            except Exception as exc:
                problems.append('maiSelRect returned bad JSON: %s' % exc)
        elif l.startswith('MAIRECT2:'):
            try:
                rect2 = json.loads(l[len('MAIRECT2:'):])
            except Exception as exc:
                problems.append('maiSelRect returned bad JSON: %s' % exc)
        elif l.startswith('MAIDUMP:'):
            try:
                dump = json.loads(l[len('MAIDUMP:'):])
            except Exception as exc:
                problems.append('maiDump returned bad JSON: %s' % exc)
    if rect is None or rect2 is None:
        problems.append('maiSelRect printed nothing')
    else:
        if rect != rect2:
            problems.append('a backwards drag gave a different box: %r vs %r' % (rect, rect2))
        # selRect() only normalises the drag; clamping to the map is regionSnapshot()'s
        # job (the probe drags to -1,-1 on purpose: a processor at 0,0 is the normal case).
        want_box = {'x1': bx - 1, 'y1': by - 1, 'x2': bx + 4, 'y2': by + 4}
        want_box['w'] = want_box['x2'] - want_box['x1'] + 1
        want_box['h'] = want_box['y2'] - want_box['y1'] + 1
        if rect != want_box:
            problems.append('drag rect %r wanted %r' % (rect, want_box))
    if dump is None:
        problems.append('maiDump printed nothing')
    else:
        for piece in ('区域 ', '处理器 1', 'micro-processor'):
            if piece not in dump:
                problems.append('dump summary is missing %r: %r' % (piece, dump[:160]))
    # ---- v0.5.0: F3 IS the left button. Press starts the drag with no arming step,
    # release ends it; the highlight is the vanilla outline anchored at the box corner.
    sel = {}
    for l in text.splitlines():
        for key in ('MASELIDLE', 'MASELPRESS', 'MASELDOWN', 'MASELMOVE', 'MASELREL',
                    'MASELEND', 'MASELDRAW', 'MAIDRAWAPI', 'MAIHINTAPI', 'MAIFONTAPI',
                    'MAIPICK', 'MAIPICKEMPTY'):
            if l.startswith(key + ':'):
                sel[key] = l[len(key) + 1:]

    def sel_state(key, label):
        raw = sel.get(key)
        if raw is None:
            problems.append('%s printed nothing' % label)
            return {}
        try:
            return json.loads(raw)
        except Exception as exc:
            problems.append('%s returned bad JSON: %s' % (label, exc))
            return {}

    idle = sel_state('MASELIDLE', 'maiSelState (before press)')
    if idle.get('holding') is not False or idle.get('dragging') is not False:
        problems.append('the select must start idle (holding/dragging false): %r' % (idle,))
    if sel.get('MASELPRESS') != 'true' or sel.get('MASELMOVE') != 'true':
        problems.append('selPress/selMove refused a plain press: %r %r'
                        % (sel.get('MASELPRESS'), sel.get('MASELMOVE')))
    down = sel_state('MASELDOWN', 'maiSelState (while held)')
    if down.get('holding') is not True or down.get('dragging') is not True:
        problems.append('pressing F3 must hold AND drag immediately: %r' % (down,))
    want_box = {'x1': bx + 5, 'y1': by + 5, 'x2': bx + 9, 'y2': by + 7, 'w': 5, 'h': 3}
    try:
        got_box = json.loads(sel.get('MASELREL') or '{}')
    except Exception as exc:
        problems.append('maiSelRelease returned bad JSON: %s' % exc)
        got_box = {}
    if got_box != want_box:
        problems.append('the drag box is not anchored at the mouse-down tile: %r wanted %r'
                        % (got_box, want_box))
    end = sel_state('MASELEND', 'maiSelState (after release)')
    if end.get('holding') is not False or end.get('dragging') is not False:
        problems.append('releasing did not clear the select state: %r' % (end,))
    # vanilla anchors the outline at the box's lower-left corner in world units (x8):
    # Lines.rect(x, y, w, h) draws from (x, y) up-right, unlike Draw.rect's centre anchor.
    # Half a tile back: Tile.worldx() is x*tilesize, i.e. the tile's CENTRE (Tile.java:166-172),
    # so the lower-left corner of tile x1 is (x1 - 0.5) * 8 -- exactly what Placement.java:380-390 does.
    want_draw = {'x': (bx + 5) * 8 - 4, 'y': (by + 5) * 8 - 4, 'w': 40, 'h': 24}
    try:
        got_draw = json.loads(sel.get('MASELDRAW') or '{}')
    except Exception as exc:
        problems.append('maiSelDraw returned bad JSON: %s' % exc)
        got_draw = {}
    if got_draw != want_draw:
        problems.append('the highlight is not corner-anchored: %r wanted %r'
                        % (got_draw, want_draw))
    draw_api = (sel.get('MAIDRAWAPI') or '').split(':')
    if draw_api[:3] != ['YES', 'YES', 'YES'] or len(draw_api) < 4 or draw_api[3] != 'accentback':
        problems.append('Lines.rect(float,float,float,float)/Lines.stroke(float)/'
                        'Draw.color(Color)/Pal.accentBack missing: %r' % (sel.get('MAIDRAWAPI'),))
    if sel.get('MAIFONTAPI') != 'YES':
        problems.append('mindustry.gen.Fonts.outline is missing -- the size label has no font: %r'
                        % (sel.get('MAIFONTAPI'),))
    if sel.get('MAIHINTAPI') != 'YES':
        problems.append('UI.showInfoFade(String) is missing -- the non-blocking hint has no API: %r'
                        % (sel.get('MAIHINTAPI'),))

    # ---- v0.8.1: one tile on a processor is still a *pick* (maiPick / processorAt stay).
    # v0.12.5 moved the F3 release path to the description box, so the old
    # "single tile opens showAiPanel directly" wiring is gone on purpose: it is pinned
    # in the v0.12.5 block below instead.
    want_pick = "%d,%d" % (bx, by)
    if sel.get("MAIPICK") != want_pick:
        problems.append("a single tile on the processor must pick it: %r wanted %r"
                        % (sel.get("MAIPICK"), want_pick))
    if sel.get("MAIPICKEMPTY") != "":
        problems.append("a non-processor building must not be picked: %r"
                        % (sel.get("MAIPICKEMPTY"),))
    for needle, why in (("function processorAt(", "the single-tile picker is gone"),
                        ("api.maiPick = function", "the pick test hook is gone")):
        if needle not in code_only:
            problems.append("v0.8.1: " + why)
    if "没有拖动" in code_only:
        problems.append("v0.8.1: the old no-drag hint is back -- a single tile must try the AI panel first")
    if re.search(r'\["f4"\]', code_only):
        problems.append("v0.9.0: f4 is bound again -- the hotkey must stay deleted")

    # ---- v0.4.7 review arrows: finding.line -> statements child -> pixel position
    arrows = None
    ast = None
    kids = {}
    for l in text.splitlines():
        if l.startswith('MAIARROWS:'):
            arrows = l[len('MAIARROWS:'):].split()
        elif l.startswith('MAIARST:'):
            try:
                ast = [float(x) for x in l[len('MAIARST:'):].split(',')]
            except Exception:
                ast = None
        elif l.startswith('MAIARROWKID:'):
            m = re.match(r'MAIARROWKID:(\d+):([-\d.]+),([-\d.]+),([-\d.]+)$', l)
            if m:
                kids[int(m.group(1))] = tuple(float(m.group(i)) for i in (2, 3, 4))
    if arrows is None:
        problems.append('maiArrows printed nothing')
    else:
        # 2 arrows (line 1 WARN, line 3 ERR+INFO -> ERR wins), and 0 / 9 are out of range
        # "arrows N" is one token, so arrows[0] == 'arrows' and arrows[1] == str(N)
        if arrows[:2] != ['arrows', '2'] or len(arrows) != 4:
            problems.append('arrows: %r (wanted 2: line 1 and line 3; 0 and 9 must be skipped)' % (arrows,))
        got = {}
        for tok in arrows[2:]:
            parts = tok.split(':')
            if len(parts) != 5:   # line:x,y:SEV:items:v
                problems.append('arrow token %r' % tok)
                continue
            xy = parts[1].split(',')
            got[int(parts[0])] = (float(xy[0]), float(xy[1]), parts[2], int(parts[3]), parts[4])
        if set(got) != {1, 3}:
            problems.append('arrow lines %r, wanted {1, 3}' % (sorted(got),))
        if 1 in got and got[1][2:4] != ('WARN', 1):
            problems.append('line 1 arrow: %r, wanted WARN with 1 finding' % (got[1],))
        if 3 in got and got[3][2:4] != ('ERR', 2):
            problems.append('line 3 arrow: %r, wanted ERR (worst of ERR+INFO) with 2 findings' % (got[3],))
        for line, v in got.items():
            if v[4] != 'v':
                problems.append('arrow on line %d is not visible' % line)
            if ast is None or (line - 1) not in kids:
                continue
            kx, ky, kh = kids[line - 1]
            want_x = ast[0] + kx - 16 - 2          # left gutter when there is room
            if want_x < 2:
                want_x = ast[0] + kx + 2
            want_y = ast[1] + ky + kh - 19 - 8     # header row is 38 px tall
            if abs(v[0] - round(want_x)) > 1 or abs(v[1] - round(want_y)) > 1:
                problems.append('arrow on line %d at %r, wanted (%r, %r)' % (line, v[:2], round(want_x), round(want_y)))
        if 1 in got and 3 in got and not got[1][1] > got[3][1]:
            problems.append('arrow on line 1 is not above the one on line 3 -- children order is off')

    # ---- v0.4.7: the review arrows, and the fake-WARN fix they came with.
    # LogicDialog.executor is package-private (real v160.7 reflection: MAIFIELDS showed
    # 'executor:pkg'), and Rhino only exposes public members -- so L.executor was
    # undefined, linkNames() came back empty, and every link slot looked unbound.
    # Read it through the reflective accessor instead of touching the field directly.
    if 'function logicExecutor(' not in code_only:
        problems.append('main.js lost logicExecutor() -- LogicDialog.executor is package-private; '
                        'a direct read is undefined and every link slot looks unbound')
    if re.search(r'(?<![A-Za-z_.])L[.]executor', code_only):
        problems.append('main.js reads L.executor directly again -- Rhino cannot see a package-private field')
    if 'setAccessible(true)' not in code_only:
        problems.append('logicExecutor() no longer calls setAccessible(true)')
    # linkNames() must distinguish "no links" ([]) from "cannot read them" (null):
    # an empty array makes quickcheck report a WARN on every link slot.
    if 'linkKnown' not in code_only:
        problems.append('the review stopped telling "no links" apart from "cannot read the link table"')
    if 'function syncArrows(' not in code_only or 'Touchable.childrenOnly' not in code_only:
        problems.append('the in-line review arrows are gone (syncArrows / Touchable.childrenOnly)')
    if 'function tapLine(' not in code_only:
        problems.append('arrow taps are not wired through tapLine()')
    if 'function layoutArrows(' not in code_only:
        problems.append('arrows are no longer re-positioned every frame (layoutArrows)')
    # ---- v0.6.0 the region panel: F3 -> dump + /region -> dialog with a per-processor button
    if 'function buildRegionTable(' not in code_only or 'function showRegionPanel(' not in code_only:
        problems.append('the region panel is gone (buildRegionTable / showRegionPanel)')
    if 'function buildAt(' not in code_only or 'showAiPanel(b);' not in code_only:
        problems.append('the region panel no longer hands a processor to the AI panel')
    if 'labelWrap(String(candText))' not in code_only:
        problems.append('the region panel lost its scrollable candidate text (labelWrap)')
    if 'function showAiPanel(target)' not in code_only:
        problems.append('showAiPanel() no longer accepts an explicit target build')
    if '(r.x1 - 0.5) * 8' not in code_only or '(r.y1 - 0.5) * 8' not in code_only:
        problems.append('the highlight lost its half-tile offset -- Tile.worldx() is the tile centre')
    # ---- v0.7.0 F2: whole-map processor list panel
    if 'var KEY_LIST    = ["f2"];' not in code_only:
        problems.append('F2 is no longer the processor list hotkey')
    if 'function scanProcessors(' not in code_only or 'function buildListTable(' not in code_only:
        problems.append('the processor list is gone (scanProcessors / buildListTable)')
    if 'function showListPanel(' not in code_only or 'api.maiListPanel = function' not in code_only:
        problems.append('the processor list panel is gone (showListPanel / maiListPanel)')
    if 'Core.camera.position.set(x * 8 + 4, y * 8 + 4)' not in code_only:
        problems.append('clicking a list row no longer jumps the camera to the processor')
    if 'isProcessor(g)' not in code_only:
        problems.append('the list scan stopped filtering by duck type (isProcessor)')

    if problems:
        print('FAIL ' + '; '.join(problems))
        print(joined[-1500:])
        return 1
    print('ui smoke: main.js initialised in the real engine')
    if snap is not None:
        print('  region snapshot: %d cell(s), %d processor(s)' % (len(snap.get('grid') or []), len(snap.get('processors') or [])))
    if arrows is not None:
        print('  review arrows: ' + ' '.join(arrows[2:]))
    if shapes:
        print('  table methods resolved: ' + ' '.join(
            '%s=%s' % (m, shapes.get(m, '?')) for m in used))
    if clean:
        print('  job queue: %s attempts=%s, %d byte payload written into the processor'
              % (clean.get('state'), clean.get('attempts'), len(str(clean.get('bytes_b64') or ''))))
    if rej:
        print('  job gate: %s after %s attempt(s), %d finding(s)'
              % (rej.get('state'), rej.get('attempts'), len(rej.get('findings') or [])))
    if zero:
        print('  job zero-link gate: %s, rule=%s (the processor links to nothing)'
              % (zero.get('state'), ','.join(sorted({str(f.get('rid')) for f in
                                                     (zero.get('findings') or [])}))))
    print('  job conflict gate: first write refused, second click overwrote (%s / %s)'
          % (edit1.split('|')[0].strip()[:40], edit2.split('|')[0].strip()[:40]))
    if wm:
        print('  batch write: ' + wm[:170])
    for l in lines:
        if l.startswith(('MAISELF:', 'MAIDIAG:', 'MAIUNDO:', 'MAICONFLICT:', 'MAILLM:',
                        'MAICHAT:', 'MAISIDECAR:', 'MAIJOBPANEL:', 'MAIJOBPANELDIAG:', 'MAIHINT:', 'MAIFILTER:', 'MAIFILTER2:', 'MAIWORKSPACE:', 'MAIWS2:', 'MAIWS3:', 'MAICHATUI:', 'MAIDUAL:', 'MAIWSDIGEST:',
                        'MAIJOBBOX', 'MAIBADGERECT:', 'MAIJOBMANAGER:', 'MAIBP:',
                        'MAISIGMETA:', 'MAIBPTITLE:', 'MAIBPCHAT:', 'MAIKEYBIND:', 'MAITHEME:',
                        'MAIJOBTHINK2:')):
            print('  ' + l[:300])
    return 0


if __name__ == '__main__':
    # this test used to leak one simulator JVM per run: it never called
    # srv.close().  The close below is explicit rather than left to atexit so the
    # child is gone before the process reports its exit code.
    try:
        rc = main()
    finally:
        try:
            import mlogsim as M
            M._close_live_servers()
        except Exception:
            pass
    sys.exit(rc)
