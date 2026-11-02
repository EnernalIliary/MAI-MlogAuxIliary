// ============================================================================
//  Mlog辅助 (MlogAuxiliary) —— scripts/main.js   v0.1  (P1: API 接入 + 写入通道)
//
//  玩家不写 mlog。选中处理器 → 选一个意图模板 → 本地 sidecar (tools/logicd.py)
//  把意图变成代码并跑仓库那套静态检查 → 面板列出 findings 与成本 → 确认写入。
//
//  写入不是"改 b.code 字段"，而是走原版 config 通道：
//      LogicBlock.config(byte[].class, (build, data) -> build.readCompressed(data, true))
//      （Mindustry-Source/core/src/mindustry/world/blocks/logic/LogicBlock.java:65-69）
//  载荷 = compress(code, links)：版本字节 1 + 代码 + 链接表 deflate（同文件 :137-166），
//  由 sidecar 用 tools/msch.py 的 build_logic_data() 生成 —— 和游戏自己的编码器同一格式。
//  于是单机与联机同一条路径，联机时服务端执行，不需要模组自己造同步。
//
//  ── 三条 Rhino 铁律（沿用 DP-CameraUnbind/METHOD.md 的实测结论）────────────
//   1) 按键常量写全限定名 Packages.arc.input.KeyCode.v
//   2) 控制台里只挂"顶层全局函数"（对象副本在控制台里是 undefined），所以末尾用
//      ScriptableObject.putProperty 把函数逐个挂到脚本作用域，并读回自检
//   3) 覆写/回调一律包 try/catch —— 顶层抛异常会让整个脚本静默中止
//
//  ── 本版边界（别假装它做了）─────────────────────────────────────────────
//   · UI 面板这一层**没有机器验证过**（无头模拟器里没有界面），协议层与写入通道
//     由 tests/logicd/run_logicd_tests.py 逐条验（含真 v160.7 处理器回读）。
//   · 游戏内快检已接（P2：scripts/quickcheck.js + rules.generated.js，五条单行判据），
//     但它只能“少报”：未赋值变量、成本 CFG、值域都在 sidecar 全量检查里。
//   · 撤销只在内存（P3）、没有 diff 预览（P3）、不接 LLM（P4）。
// ============================================================================

function __logicWriterMain(){
    "use strict";

    var MOD_VERSION = "0.19.6";
    var URL_KEY     = "mlogauxiliary-url";
    var DEFAULT_URL = "http://127.0.0.1:8765";
    // v0.11.0：设置页里填的模型档三件套。密钥只留在本机 Core.settings 与 sidecar 进程内存。
    var LLM_URL_KEY   = "mlogauxiliary-llm-url";
    var LLM_KEY_KEY   = "mlogauxiliary-llm-key";
    // v0.14.4（用户 2026-11-01）：「是否将AI工作提示发送到服务器聊天框，默认关闭」。
    var CHAT_SEND_KEY = "mlogauxiliary-chat-send";
    // v0.14.5（用户 2026-11-01）：「做一个hud倍率自己调就好」。框角控件在 HUD 上的大小
    // 因人而异，我给 84×40 只是起点；倍率存在 Core.settings 里，设置页加减，下一帧生效。
    var HUD_SCALE_KEY  = "mlogauxiliary-hud-scale";
    var HUD_SCALE_MIN  = 0.5;
    var HUD_SCALE_MAX  = 2;
    var HUD_SCALE_STEP = 0.1;
    var hudScaleBtn    = null;     // 设置页中间那格（显示当前倍率，点一下回 1.0×）
    var hudScaleLbl    = null;     // 设置页缩放行右侧的「1.0×」
    var hudSlider      = null;     // 设置页缩放滑条（无 UI 环境为 null，退化成按钮）
    var hudSliderLast  = -1;       // 上一帧滑条读数：用来判断「用户拖过了」
    var LLM_MODEL_KEY = "mlogauxiliary-llm-model";
    var KEY_REGION  = ["f7"];      // F7：扫鼠标周围 10 格，列出链接候选
    var KEY_SELECT  = ["f3"];      // F3：按住 = 鼠标左键拉框（v0.5.0；F8 是游戏控制台，F9/F10 也被占）
var KEY_LIST    = ["f2"];      // F2：全图处理器清单（v0.7.0；剩下的空键只有 F2，F1/F5/F6/F8/F11/F12 是游戏自己的）
    var TIMEOUT_MS  = 4000;        // 连接超时：sidecar 是本机的，慢就是没开

    function log(msg){ print("[mai] " + msg); }

    // ---------------------------------------------------------- 设置界面
    // 设置 -> 「Mlog辅助」一栏。把调试信息全摊开（版本 / 构建 / 快检模块是否加载 /
    // 规则表条数 / sidecar 地址与可达性 / 数据目录 / mods 目录），省得再往控制台敲命令。
    // 所有游戏对象访问都套 try：无头环境里只是返回 false，不会炸。

    var sidecarOk = null;
    var settingsDone = false;
    // v0.18.3（用户 m11124）：「运行环境的设置项显示未注册」——设置页正在渲染这件事本身
    // 就证明分类注册成功了；selfTest 那条假表路径里 settingsDone 还是 false，会假阴性。
    var SETTINGS_LIVE = false;
    var settingsErr = null;
    var settingsTries = 0;    // 见热键钩子：UI 晚于脚本建好时每 60 帧重试一次   // 注册失败时的原文，摊在诊断里，省得翻 last_log.txt

    function countKeys(o){
        var n = 0;
        if(o === null || o === undefined){ return 0; }
        for(var k in o){ if(Object.prototype.hasOwnProperty.call(o, k)){ n++; } }
        return n;
    }

    function dirText(fi){
        try{ return String(fi.absolutePath()); }
        catch(e){ return "?"; }
    }

    // 诊断数据只算一次，两个出口共用：设置页（键值两列对齐）与控制台/剪贴板（纯文本）。
    function diagParts(){
        var parts = [];
        parts.push({k: "版本", v: MOD_VERSION + "（构建 " + gameBuild() + "）"});
        if(RULES === null){
            parts.push({k: "快检", v: "未加载（缺 rules.generated.js 或 quickcheck.js）"});
        }else{
            parts.push({k: "快检", v: "已加载 · 指令 " + countKeys(RULES.instr) + " 条 / 版本门 "
                       + countKeys(RULES.ops) + " 条 / 链接槽 " + countKeys(RULES.link) + " 条"});
        }
        parts.push({k: "sidecar", v: base + "（"
                    + (sidecarOk === null ? "未探测" : (sidecarOk ? "可达" : "不可达")) + "）"});
        parts.push({k: "大模型", v: llmText()});
        parts.push({k: "数据目录", v: dirText(Vars.dataDirectory)});
        parts.push({k: "mods 目录", v: dirText(Vars.modDirectory)});
        parts.push({k: "设置项", v: (settingsDone || SETTINGS_LIVE) ? "已注册（设置 → Mlog辅助）"
                    : (settingsErr === null ? "未注册（Vars.ui 未就绪）" : "注册失败：" + settingsErr)});
        return parts;
    }

    function diagLines(){
        var parts = diagParts(), out = [];
        for(var i = 0; i < parts.length; i++){ out.push(parts[i].k + "：" + parts[i].v); }
        return out;
    }

    function diagText(){ return diagLines().join("\n"); }

    function probeSidecar(){
        httpJson("GET", "/health", null, function(code, text){
            sidecarOk = (code === 200);
            var body = String(text).substring(0, 200);
            log("health " + code + " " + body.substring(0, 100));
            var msg = sidecarOk ? ("可达：" + base + "\n" + body)
                                : ("不可达（" + code + "）：" + base + "\n先启动 sidecar：python tools/logicd.py");
            if(Vars.ui !== null){ Vars.ui.showText("Mlog辅助 · 连接", msg); }
        });
    }

    // ------------------------------------------------- 自动拉起 sidecar（v0.13.1）
    // 装了包之后玩家不该再开命令行窗口。F3 一按（或打开面板），模组先探一次 /health：
    // 探不到就把包里的 sidecar/bin/logicd.exe 拉起来 —— 那是 PyInstaller 打的 onedir
    // 构建，不需要玩家装 Python —— 然后每 400 ms 轮询到它开口。
    // 找不到 exe 时（在源码树里跑、或者玩家删了 bin）退回原样：只提示「先开 sidecar」。
    // 为什么要轮询而不是等进程输出：ProcessBuilder 的输出被重定向到 sidecar.log 了，
    // 模组读不到管道；而 sidecar 的 ready 行写在它自己的 stdout 上。
    // v0.15.0（C1+C2）：url 是自己的 sidecar 真正在的地址（--port 0 由它挑，端口写在
    // sidecar.log 的 ready 行上）；spawned 只对「我拉起来的」为真 —— 心跳与 /shutdown
    // 都只发给自己的进程，别人手动起的 python tools/logicd.py 不碰。
    var SIDE = {state: "idle", tries: 0, at: 0, why: "", exe: "", url: "",
                pingAt: 0, spawned: false, pingFail: 0};
    // sidecar 每 45 s 收不到 /ping 就自己退出（逻辑后端不该活过游戏）。
    var SIDE_IDLE_EXIT = 45;

    // v0.14.6（用户 m06631：游戏内导入的模组是 zip）：`m.root` 是 ZipFi，随包的
    // logicd.exe 只是 zip 里的一个条目 —— ProcessBuilder 执行不了它，`pb.directory()`
    // 也拿不到真实目录。所以第一次用到时把 sidecar 整棵解到数据目录（目录名带版本号，
    // 升级自动换新），此后一律从那里跑；`安装.bat` 装到磁盘的走原路径，不解压。
    var SIDE_BIN = null;

    function sidecarIsZip(){
        try{
            var m = Vars.mods.getMod("mlog-auxiliary");
            if(m === null || m === undefined){ return false; }
            return String(m.root.getClass().getName()).indexOf("ZipFi") >= 0;
        }catch(e){ log("sidecarIsZip: " + e); return false; }
    }

    // 不用 try/finally：v160.7 的 Rhino 在 finally 体里带 try/catch 时 codegen 会炸
    // （见 run_ui_smoke.py 的红线）。这里逐层 try/catch 兜底即可。
    function sidecarCopyTree(src, dst){
        dst.mkdirs();
        var kids = src.list();
        for(var i = 0; i < kids.length; i++){
            var k = kids[i];
            var t = dst.child(String(k.name()));
            if(k.isDirectory()){
                sidecarCopyTree(k, t);
            }else if(!(t.exists() && t.length() === k.length())){
                var inp = k.read();
                t.write(inp, false);
                inp.close();
            }
        }
    }

    function sidecarBinDir(){
        if(SIDE_BIN !== null && SIDE_BIN.exists()){ return SIDE_BIN; }
        try{
            var m = Vars.mods.getMod("mlog-auxiliary");
            if(m === null || m === undefined){ return null; }
            var src = m.root.child("sidecar");
            if(src === null || !src.exists()){ return null; }
            if(!sidecarIsZip()){
                SIDE_BIN = src.child("bin");
                return SIDE_BIN;
            }
            var out = Vars.dataDirectory.child("mlogauxiliary").child("sidecar")
                .child(String(MOD_VERSION));
            var bin = out.child("bin");
            if(!bin.child("logicd.exe").exists()){
                log("sidecar: mod is a zip, extracting to " + out.absolutePath());
                hint("从模组包解出逻辑后端（首次约 40 MB）…");
                sidecarCopyTree(src, out);
            }
            SIDE_BIN = bin;
            return SIDE_BIN;
        }catch(e){ log("sidecarBinDir: " + e); return null; }
    }

    function sidecarExe(){
        try{
            var d = sidecarBinDir();
            if(d === null || d === undefined){ return null; }
            var f = d.child("logicd.exe");
            return (f !== null && f.exists()) ? f : null;
        }catch(e){ log("sidecarExe: " + e); return null; }
    }

    function sidecarDir(){
        try{ return sidecarBinDir(); }
        catch(e){ return null; }
    }

    function sidecarPort(){
        try{ return endpoint(base).port; }
        catch(e){ return 8765; }
    }

    // v0.15.0（C1）：sidecar 用 --port 0 自己挑端口，真实端口只出现在它 stdout 的
    // ready 行上（重定向到了 sidecar.log）。固定 8765 被上一局遗留的旧 sidecar 占着时，
    // 模组会以为「自己拉起来的进程没起来」—— 真实踩过一次（8765 上跑着 0.12.4 的 python）。
    function sidecarLogPort(dir){
        try{
            if(dir === null || dir === undefined){ return 0; }
            var f = dir.child("sidecar.log");
            if(f === null || !f.exists()){ return 0; }
            var s = String(f.readString());
            var key = "listening on http://";
            var i = s.lastIndexOf(key);
            if(i < 0){ return 0; }
            var rest = s.substring(i + key.length);
            var j = rest.indexOf(":");
            if(j < 0){ return 0; }
            var tail = rest.substring(j + 1);
            var k = 0;
            while(k < tail.length && tail.charAt(k) >= "0" && tail.charAt(k) <= "9"){ k++; }
            if(k === 0){ return 0; }
            var n = parseInt(tail.substring(0, k), 10);
            return (n > 0 && n < 65536) ? n : 0;
        }catch(e){ log("sidecarLogPort: " + e); return 0; }
    }

    // 这次会话真正要用的地址：自己的 sidecar 用日志里的端口，否则用设置里手填的地址。
    function sidecarUrl(){
        if(SIDE.url !== "" && SIDE.url !== null){ return SIDE.url; }
        return base;
    }

    // v0.15.0（C2）：心跳。sidecar 侧 --idle-exit 45 秒收不到就退出，游戏关掉后
    // 不会再留一个 logicd 进程占内存和端口。
    // v0.15.3 真机根因：这里原来传的是 {} 对象，httpRaw 用 String(body) 拼请求体，
    // 发出去的是 "[object Object]" ⇒ sidecar 回 400 bad json ⇒ 心跳被当成失败,
    // 之后不再发心跳，45 秒后进程自退，玩家看到「已就绪，但投不出去」。body 必须是 "{}"。
    function sidecarPing(){
        if(SIDE.state !== "up" || SIDE.spawned !== true){ return; }
        if(SIDE.url === "" || SIDE.url === null){ return; }
        var now = Date.now();
        if(SIDE.pingAt > 0 && (now - SIDE.pingAt) < 4000){ return; }
        SIDE.pingAt = now;
        httpJson("POST", "/ping", "{}", function(code, text){
            if(code === 200){ SIDE.pingFail = 0; return; }
            SIDE.pingFail = (SIDE.pingFail || 0) + 1;
            log("sidecar ping failed: " + code + " (" + SIDE.pingFail + ")");
            // -1 是连不上：进程真的没了，立刻退役重拉；
            // 4xx/5xx 可能只是一次抖动，连续 3 次才算死，免得一次误判就丢后端。
            if(code === -1 || SIDE.pingFail >= 3){
                sidecarOk = false;
                sidecarRetire(sidecarUrl(), "心跳失败（" + code + "）");
            }
        });
    }

    // 版本对不上就不该悄悄用：0.12.4 的 sidecar 少一半接口，表现出来是「生成失败」。
    function sidecarVersionMismatch(h){
        var v = "";
        try{ v = String((h === null || h === undefined) ? "" : (h.version || "")); }
        catch(e){ v = ""; }
        return (v !== "" && v !== String(MOD_VERSION));
    }

    // 这个 sidecar 是不是模组自己装出来的（数据目录/装机目录里的 exe），还是别人手起的。
    function sidecarOurs(h){
        var e = "";
        try{ e = String((h === null || h === undefined) ? "" : (h.exe || "")); }
        catch(e2){ e = ""; }
        return e.toLowerCase().indexOf("mlogauxiliary") >= 0;
    }

    // 退役我们自己拉起来的那一个：请它退出，等它真的走了再重新拉一个。
    function sidecarRetire(url, why){
        log("sidecar retire (" + why + "): " + url);
        SIDE.spawned = false;
        SIDE.state = "idle";
        SIDE.url = "";
        sidecarOk = false;
        try{
            httpJson("POST", "/shutdown", "{}", function(code, text){
                log("sidecar shutdown -> " + code);
                Threads.daemon(function(){
                    try{ Packages.java.lang.Thread.sleep(700); }catch(e){ /* ignore */ }
                    Core.app.post(function(){
                        try{ if(SIDE.state === "idle"){ ensureSidecar(true); } }
                        catch(e2){ log("sidecar respawn failed: " + e2); }
                    });
                });
            });
        }catch(e){ log("sidecar shutdown failed: " + e); }
    }

    // 用 ProcessBuilder 而不是 Runtime.exec：exec 只能给一个命令行字符串，路径里的空格
    // （saves/mods 常常在 "Program Files (x86)" 下面）会被它按空格切开；数组形式没这个问题。
    // 输出必须重定向到文件 —— 子进程的 stdout 管道没人读，攒满 64 KB 后它会卡死。
    function sidecarSpawn(exe, dir){
        try{
            // v0.15.0：--port 0（自己挑，避开被占住的 8765）+ --idle-exit（心跳断了就退出）
            var arr = Packages.java.lang.reflect.Array.newInstance(Packages.java.lang.String, 5);
            arr[0] = String(exe.absolutePath());
            arr[1] = "--port";
            arr[2] = "0";
            arr[3] = "--idle-exit";
            arr[4] = String(SIDE_IDLE_EXIT);
            var pb = new Packages.java.lang.ProcessBuilder(arr);
            pb.directory(new Packages.java.io.File(String(dir.absolutePath())));
            pb.redirectErrorStream(true);
            pb.redirectOutput(new Packages.java.io.File(String(dir.absolutePath()), "sidecar.log"));
            var p = pb.start();
            SIDE.exe = String(exe.absolutePath());
            SIDE.spawned = true;
            SIDE.url = "";
            SIDE.pingFail = 0;
            SIDE.pingAt = 0;
            log("sidecar spawned: " + SIDE.exe);
            return p;
        }catch(e){
            log("sidecarSpawn failed: " + e);
            SIDE.why = String(e);
            return null;
        }
    }

    // 每帧便宜地看一眼：只在 starting 状态才真的发请求。
    function sidecarTick(){
        if(SIDE.state !== "starting"){ return; }
        var now = Date.now();
        if(SIDE.at > 0 && (now - SIDE.at) < 400){ return; }
        SIDE.at = now;
        SIDE.tries++;
        var tries = SIDE.tries;
        // 自己拉起来的那一个：端口写在 sidecar.log 里，先读出来再探测
        if(SIDE.url === "" && SIDE.spawned === true){
            var lp = sidecarLogPort(sidecarDir());
            if(lp > 0){ SIDE.url = "http://127.0.0.1:" + lp; SIDE.pingAt = 0; }
        }
        httpJson("GET", "/health", null, function(code, text){
            if(code === 200){
                var h = parse(text) || {};
                if(sidecarVersionMismatch(h)){
                    if(sidecarOurs(h)){
                        sidecarRetire(sidecarUrl(), "版本 " + String(h.version || "?")
                                      + " 不是 " + MOD_VERSION);
                    }else{
                        SIDE.state = "stale";
                        hint("8765 上是另一份 sidecar（版本 " + String(h.version || "?")
                             + "，本模组是 " + MOD_VERSION + "）：先把它停掉，否则会缺接口");
                    }
                    return;
                }
                sidecarOk = true;
                SIDE.state = "up";
                SIDE.pingFail = 0;
                SIDE.pingAt = 0;
                log("sidecar is up after " + tries + " probe(s) at " + sidecarUrl());
                flushPendingSubmits();      // v0.17.0：排队中的投递现在发出去
                llmAutoSync();          // v0.14.4：模型档随 sidecar 就绪自动挂载
            }else if(tries >= 25){
                SIDE.state = "failed";
                hint("sidecar 启动失败（25 次探测无响应），日志见 sidecar.log");
            }
        });
    }

    // v0.14.4（用户 2026-11-01）：「大模型也应该随游戏自动挂载」。
    // sidecar 每次重启都会把内存里的模型档忘掉（配置只在进程里），所以只要它一就绪，
    // 就把 Core.settings 里的 地址/密钥/模型 推一遍。60 s 内不重复推（每帧的 health 回调会反复命中）。
    var llmAutoAt = 0;
    function llmAutoSync(){
        var now = 0;
        try{ now = Packages.java.lang.System.currentTimeMillis(); }catch(e){ }
        if(llmAutoAt > 0 && now > 0 && now - llmAutoAt < 60000){ return; }
        llmAutoAt = (now > 0) ? now : 1;
        try{ llmSync(); }catch(e){ log("llm auto-sync skipped: " + e); }
    }

    // 幂等：已经在 starting 就不重复拉；已经在跑就不管。
    function ensureSidecar(quiet){
        if(SIDE.state === "starting"){ return; }
        if(SIDE.state === "up" && sidecarOk === true){ return; }
        httpJson("GET", "/health", null, function(code, text){
            if(code == 200){
                var h = parse(text) || {};
                if(!sidecarVersionMismatch(h)){
                    sidecarOk = true; SIDE.state = "up";
                    llmAutoSync();  // v0.14.4：已经在跑的 sidecar 也补一次模型档
                    return;
                }
                // v0.15.0（C1）：地址上有人应，但版本不是这一份。
                if(sidecarOurs(h)){
                    sidecarRetire(sidecarUrl(), "旧版 " + String(h.version || "?"));
                    return;
                }
                SIDE.state = "stale";
                if(quiet !== true){
                    hint("8765 上是另一份 sidecar（版本 " + String(h.version || "?")
                         + "，本模组是 " + MOD_VERSION + "）：先把它停掉再试");
                }
                return;
            }
            var exe = sidecarExe();
            var dir = sidecarDir();
            if(exe === null || dir === null){
                SIDE.state = "noexe";
                if(quiet !== true){
                    hint("连不上 sidecar（" + base + "），安装里也没有 sidecar/bin/logicd.exe。源码树先跑 "
                         + "python tools/logicd.py，装包版重新双击 安装.bat。");
                }
                return;
            }
            var p = sidecarSpawn(exe, dir);
            if(p === null){
                SIDE.state = "failed";
                if(quiet !== true){ hint("sidecar 启动失败：" + SIDE.why); }
                return;
            }
            SIDE.state = "starting"; SIDE.tries = 0; SIDE.at = 0;
            if(quiet !== true){ hint("正在启动 sidecar…"); }
        });
    }

    function sidecarStateText(){
        if(SIDE.state === "up"){ return "已就绪" + (SIDE.spawned === true ? "（本模组启动）" : ""); }
        if(SIDE.state === "stale"){ return "地址上是旧版 sidecar"; }
        if(SIDE.state === "starting"){ return "启动中（探测 " + SIDE.tries + " 次）"; }
        if(SIDE.state === "noexe"){ return "缺少 logicd.exe"; }
        if(SIDE.state === "failed"){ return "启动失败"; }
        return "未尝试";
    }

    function isLogicBuild(b){
        try{ return b instanceof Packages.mindustry.world.blocks.logic.LogicBlock.LogicBuild; }
        catch(e){ return b !== null && b !== undefined && typeof b.code === "string"; }
    }

    function aimedBuild(){
        try{
            var t = Vars.world.tileWorld(Core.input.mouseWorldX(), Core.input.mouseWorldY());
            if(t === null || t.build === null){ return null; }
            return isLogicBuild(t.build) ? t.build : null;
        }catch(e){ return null; }
    }

    function quickAimed(){
        var b = aimedBuild();
        if(b === null || Vars.ui === null){
            if(Vars.ui !== null){
                Vars.ui.showText("Mlog辅助 · 快检", "先把光标指向处理器");
            }
            return;
        }
        var res = quickRun(String(b.code), b);
        var out = [quickSummary(res)];
        var ls = quickLines(res);
        for(var i = 0; i < ls.length && i < 12; i++){ out.push(ls[i]); }
        Vars.ui.showText("Mlog辅助 · 快检", out.join("\n"));
    }

    // 只碰 Table / Cell 的少数几个方法，方便在没有 UI 的环境里用假表 smoke。
    // ------------------------------------------------------------ 设置页
    // 只用真引擎复核过的 Table / Cell 形状（tests/mlogauxiliary/run_ui_smoke.py）：
    // 一律平铺成「键 值 + 按钮」，绝不 t.table(fn)（v160.7 Rhino 里重载歧义），
    // 也不 t.scrollPane(fn) / t.textarea(fn)（Table 上根本没有这两个方法）。
    // 原版风格的排版素材。分段标题 + 3px 强调线：BaseDialog 的标题就是这么画的
    //   titleTable.image(Tex.whiteui, Pal.accent).growX().height(3f)   BaseDialog.java:25
    // 行用灰底 Styles.grayPanel：原版 CheckSetting 用的同一个 Drawable（SettingsMenuDialog.java:825-827），
    // 行宽/内边距也照它（minWidth 500、padTop 7 -> 这里 540 / 5，给中文留点余量）。
    var LABEL_W = 128, VALUE_W = 390, ROW_W = 540;
    // v0.18.1（用户 m10289）：infoCircle 固定贴住每行最右侧，值区给它留出位置。
    var INFO_W = 26;

    // ------------------------------------------------ v0.17.0（用户 m09440）
    // 1.「完成后自动写入」：作业一 done 就写进那台处理器，不用再点进详情。
    var AUTO_WRITE_KEY = "mlogauxiliary-auto-write";
    var autoWriteLbl = null;
    var hotSelLbl = null, hotListLbl = null, hotRegionLbl = null;

    function autoWriteOn(){
        try{ return Core.settings.getBool(AUTO_WRITE_KEY, false) === true; }
        catch(e){ return false; }
    }

    function autoWriteSet(v){
        try{ Core.settings.put(AUTO_WRITE_KEY, (v === true)); }
        catch(e){ log("auto write save failed: " + e); }
    }

    function autoWriteText(){
        return autoWriteOn() ? "自动写入已开启" : "完成后自动写入：关";
    }

    function autoWriteRefresh(){
        try{
            if(autoWriteLbl === null || autoWriteLbl === undefined){ return; }
            if(typeof autoWriteLbl.get !== "function"){ return; }
            var b = autoWriteLbl.get();
            if(b === null || b === undefined || typeof b.setText !== "function"){ return; }
            b.setText(autoWriteText());
        }catch(e){ log("auto write label not updated"); }
    }

    // 3. 解释信息收进 infoCircle：悬浮才显示，常驻的只有短句。
    // arc 的 Tooltips.create(String) 是 Java 侧造好的 provider，Rhino 里不用实现 Cons 接口。
    function tipOn(el, text){
        try{
            if(el === null || el === undefined || typeof el.addListener !== "function"){ return false; }
            // 无头环境没有 Scene（Tooltip 建 label 要 skin）—— 那里不建提示，也不报错。
            if(Core.scene === null || Core.scene === undefined){ return false; }
            var tp = Packages.arc.scene.ui.Tooltip.Tooltips.getInstance().create(String(text));
            try{ tp.setInstant(true); }catch(e1){ }
            el.addListener(tp);
            return true;
        }catch(e){ log("tooltip failed: " + e); return false; }
    }

    // v0.18.1（用户 m10289）：「设置主题色改为 [#8abcd1]，强调色为 [highlight]」。
    // [highlight] 在游戏里不是独立字段，UI.java:107 定义成 Pal.accent.cpy().lerp(Color.white, 0.3f)，
    // 这里取同一个算法，颜色与游戏文本标记一致；取不到就退回 Pal.accent。
    // v0.18.2（用户 m10938）：「允许在设置中调节主题色和强调色」。两个颜色都存 Core.settings，
    // 值是 6 位十六进制字符串。**不能存数字**：arc 的 Settings.getFloat 是 (float)get(...)，
    // JS 数字会变成 java.lang.Double，强转抛 ClassCastException（v0.18.1 倍率 bug 同一个坑）。
    var THEME_KEY = "mlogauxiliary-theme-color";
    var EMPH_KEY = "mlogauxiliary-emph-color";
    var THEME_DEFAULT = "8abcd1";          // v0.18.1 用户指定的主题色
    var COLOR_CACHE = {};                  // hex -> Color，别每帧 valueOf

    // 只认 6 位十六进制：'#'、空格容错，其它一律当非法（返回 null）。手写扫描，不用字符类正则。
    function hex6(v){
        if(v === null || v === undefined){ return null; }
        var raw = String(v), s = "", i, c, lc;
        for(i = 0; i < raw.length; i++){
            c = raw.charAt(i);
            if(c === "#" || c === " "){ continue; }
            s = s + c;
        }
        if(s.length !== 6){ return null; }
        for(i = 0; i < 6; i++){
            lc = s.charAt(i).toLowerCase();
            if(!((lc >= "0" && lc <= "9") || (lc >= "a" && lc <= "f"))){ return null; }
        }
        return s.toLowerCase();
    }

    // Color.toString() 是 rrggbbaa，取前 6 位
    function colorHexOf(col){
        try{ return hex6(String(col).substring(0, 6)); }
        catch(e){ return null; }
    }

    function colorOfHex(h, fallback){
        if(h === null || h === undefined){ return fallback; }
        var hit = COLOR_CACHE[h];
        if(hit !== undefined && hit !== null){ return hit; }
        try{
            var c = Packages.arc.graphics.Color.valueOf(h);
            COLOR_CACHE[h] = c;
            return c;
        }catch(e){
            log("color parse failed: " + h + " / " + e);
            return fallback;
        }
    }

    // 读设置里的颜色；没设或非法就用默认值（defHex 为 null 时交给调用方兜底）
    function colorSetting(key, defHex){
        var raw = null;
        try{ raw = Core.settings.get(key, null); }catch(e0){ raw = null; }
        var h = hex6(raw);
        if(h === null){ h = hex6(defHex); }
        return h;
    }

    function themeHex(){ return colorSetting(THEME_KEY, THEME_DEFAULT); }

    // 强调色默认取 [highlight] 的算法：UI.java:107 Pal.accent.cpy().lerp(Color.white, 0.3f)
    function emphDefaultHex(){
        try{ return colorHexOf(Pal.accent.cpy().lerp(Packages.arc.graphics.Color.white, 0.3)); }
        catch(e){ return "ffd37f"; }
    }

    function emphHex(){ return colorSetting(EMPH_KEY, emphDefaultHex()); }

    function themeColor(){ return colorOfHex(themeHex(), Pal.accent); }
    function emphColor(){ return colorOfHex(emphHex(), Pal.accent); }

    // 外观段的两种颜色：一行 = 标签 + 十六进制输入框 + 色块 + 「默认」+ 行尾 infoIcon。
    // 非法输入退回当前值并说明格式；改动即时落盘（选框/高亮下一帧变，
    // 已经建好的标题这类控件要重开设置页才换色）。
    function colorRow(t, label, key, defHex, mk, expl){
        var row = mk();
        row.margin(8).left();
        row.add(label).width(LABEL_W).left().color(Pal.gray);
        var field = null;
        var fcell = row.field(colorSetting(key, defHex), function(v){
            var h = hex6(v);
            if(h === null){
                log("odd color input: " + v);
                try{ if(field !== null){ field.setText(colorSetting(key, defHex)); } }catch(e1){ }
                try{
                    if(Vars.ui !== null && Vars.ui !== undefined){
                        Vars.ui.showText("Mlog辅助 · 颜色", "颜色要写成 6 位十六进制，例如 " + defHex + "。");
                    }
                }catch(e2){ }
                return;
            }
            try{ Core.settings.put(key, h); }catch(e3){ log("color save failed: " + e3); }
            log("color " + key + " = " + h);
            try{ if(swatch !== null){ swatch.setColor(colorOfHex(h, Pal.accent)); } }catch(e4){ }
        }).width(120).left();
        try{ field = fcell.get(); }catch(e5){ field = null; }
        var swatch = null;
        try{ swatch = row.image(Packages.mindustry.gen.Tex.whiteui).width(22).height(22).padLeft(8).get(); }
        catch(e6){ swatch = null; }
        try{ if(swatch !== null){ swatch.setColor(colorOfHex(colorSetting(key, defHex), Pal.accent)); } }
        catch(e7){ }
        row.button("默认", function(){
            var h = hex6(defHex);
            try{ Core.settings.put(key, h); }catch(e8){ log("color reset failed: " + e8); }
            try{ if(field !== null){ field.setText(h); } }catch(e9){ }
            try{ if(swatch !== null){ swatch.setColor(colorOfHex(h, Pal.accent)); } }catch(e10){ }
        }).height(34).padLeft(8);
        infoIcon(row, expl);
        t.add(row).width(ROW_W).left().padTop(5).row();
    }

    // 提示要换行：arc 默认的 Tooltips.create(String) 是 t.add(text)（Label 不 wrap），
    // 长句会拉成一条很宽的提示。Label 本身认 '\n'，所以先手工软换行：
    // 中文按字数切，ASCII 单词（Core.settings / sidecar 这类）整块搬，标点后优先断。
    function isAsciiWordChar(c){
        return (c >= "a" && c <= "z") || (c >= "A" && c <= "Z") || (c >= "0" && c <= "9")
            || c === "_" || c === "." || c === ":" || c === "/" || c === "-" || c === "@";
    }

    function softWrap(text, max){
        var s1 = String(text), out = [], line = "", i = 0, c, j, w;
        while(i < s1.length){
            c = s1.charAt(i);
            if(c === "\n"){ out.push(line); line = ""; i++; continue; }
            if(isAsciiWordChar(c)){
                j = i;
                while(j < s1.length && isAsciiWordChar(s1.charAt(j))){ j++; }
                w = s1.substring(i, j);
                if(line.length > 0 && (line.length + w.length) > max){ out.push(line); line = ""; }
                line = line + w;
                i = j;
                continue;
            }
            if(line.length >= max){ out.push(line); line = ""; }
            line = line + c;
            i++;
            if((c === "\uff1b" || c === "\u3002" || c === "\uff1a" || c === "\uff0c")
               && line.length >= max * 0.75){
                out.push(line); line = "";
            }
        }
        if(line.length > 0){ out.push(line); }
        return out.join("\n");
    }

    function infoIcon(row, text){
        try{
            var cell = row.image(Packages.mindustry.gen.Icon.infoCircle);
            try{ cell.size(18); }catch(e1){ }
            try{ cell.padLeft(6); }catch(e2){ }
            // expandX + right：行宽是固定的，让这一格吃掉剩下的空间，图标就贴住行尾。
            try{ cell.expandX(); cell.right(); }catch(e3){ }
            try{ cell.color(emphColor()); }catch(e4){ }
            var el = null;
            try{ el = cell.get(); }catch(e5){ el = null; }
            if(el !== null && el !== undefined){ tipOn(el, softWrap(text, 34)); }
        }catch(e){ log("info icon failed: " + e); }
    }

    // Label 的文字对齐是自己的字段（labelAlign 默认 left），只给 Cell 设 center()
    // 只挪控件、不挪文字 —— v0.17.1 用户报「标题都没居中」就是这里。
    function alignText(el, align){
        if(el === null || el === undefined){ return; }
        try{
            if(typeof el.setAlignment === "function"){ el.setAlignment(align); }
        }catch(e){ log("align failed: " + e); }
    }

    // 2. 设置页主标题：两行、居中、放大。
    function titleLine(t, text, scale, color){
        var cell = t.add(String(text));
        try{ cell.width(ROW_W); }catch(e1){ }
        try{ cell.center(); }catch(e2){ }
        try{ cell.padTop(scale > 1.5 ? 8 : 0); }catch(e3){ }
        try{
            var lbl = cell.get();
            try{ lbl.setFontScale(scale); }catch(e4){ }
            try{ lbl.setColor(color); }catch(e5){ }
            alignText(lbl, Packages.arc.util.Align.center);
        }catch(e6){ }
        try{ cell.row(); }catch(e7){ }
    }

    // 5. 热键：v0.17.1（用户 m09822 / 1）起框选键注册进原版「按键绑定」界面
    // （设置 → 控制 → 按键绑定，键名来自 bundles/bundle*.properties 的 keybind.mai-select.name），
    // 值由 KeyBind 自己存 Core.settings（keybind-default-keyboard-mai-select-key），不用我们再管。
    // 清单（F2）与区域候选（F7）不再允许改键，固定用这两个默认键。
    var KB_SELECT = null, KB_TRIED = false;
    var KB_LIST = null, KB_LIST_TRIED = false;

    function selKeyBind(){
        if(KB_SELECT === null && !KB_TRIED){
            KB_TRIED = true;
            try{
                var dflt = new Packages.arc.input.KeyBind.Axis(Packages.arc.input.KeyCode.f3);
                KB_SELECT = Packages.arc.input.KeyBind.add("mai-select", dflt, "mlogauxiliary");
                log("keybind registered: mai-select");
            }catch(e){
                KB_SELECT = null;
                log("keybind add failed: " + e);
            }
        }
        return KB_SELECT;
    }

    // 绑定的「轴」本身（arc 的 KeyBind.Axis：key 是单键，min/max 是区间）。
    function selKeyAxis(){
        var kb = selKeyBind();
        if(kb === null || kb === undefined){ return null; }
        try{
            var ax = kb.value;
            if(ax === null || ax === undefined){ return null; }
            return ax;
        }catch(e){ return null; }
    }

    // 当前绑定的框选键；没注册上就退回 F3。
    // v0.18.1（用户 m10546）：「设置控制更改的热键不生效。」—— 根因在 KeybindDialog.rebind：
    // 默认值是 Axis 的绑定走两步（先按 min 再按 max），落盘的 Axis 只有 min/max、key 是 null
    // （KeybindDialog.java:133-140 → KeyBind.java:74-77 load 回来同样没有 key）。
    // 这里原来只认 ax.key ⇒ 改键之后永远读不到 ⇒ 一直退回硬编码的 f3。
    // 现在两种形状都认：单键用 key，区间用 min（两次按同一颗键时 min == max，就是单键）。
    function selKeyCode(){
        var ax = selKeyAxis();
        if(ax !== null){
            try{
                if(ax.key !== null && ax.key !== undefined){ return ax.key; }
            }catch(e1){ }
            try{
                if(ax.min !== null && ax.min !== undefined){ return ax.min; }
            }catch(e2){ }
        }
        try{ return Packages.arc.input.KeyCode.f3; }catch(e3){ return null; }
    }

    // 按住判定：不是区间就只看那一颗键；区间取第一个键（第二颗键留给玩家自定义 widen，
    // 不做「两键同按才算」—— 框选是拖鼠标的动作，要求两个键同时按住只会更难用）。
    function selKeyDown(){
        var ax = selKeyAxis();
        if(ax === null || ax === undefined){
            var fk = keyByName("f3");
            if(fk === null || fk === undefined){ return false; }
            try{ return Core.input.keyDown(fk) === true; }catch(e0){ return false; }
        }
        try{
            if(ax.key !== null && ax.key !== undefined){ return Core.input.keyDown(ax.key) === true; }
            if(ax.min !== null && ax.min !== undefined){ return Core.input.keyDown(ax.min) === true; }
        }catch(e){ log("selKeyDown failed: " + e); }
        return false;
    }

    // 给设置页显示：现在这颗键到底绑成了什么。
    function selKeyName(){
        var ax = selKeyAxis();
        if(ax !== null && ax !== undefined){
            try{
                if(ax.key !== null && ax.key !== undefined){ return ax.key.getName(); }
            }catch(e1){ }
            try{
                if(ax.min !== null && ax.min !== undefined){
                    var a = ax.min.getName();
                    if(ax.max !== null && ax.max !== undefined && ax.max !== ax.min){
                        a = a + " / " + ax.max.getName();
                    }
                    return a;
                }
            }catch(e2){ }
        }
        return "f3";
    }

    // v0.18.1（用户 m10546）：「设置控制更改的热键不生效。」修好 sel* 之后，
    // F2「处理器清单」也照同一套注册进原版按键绑定（分类 MAI、条目「处理器清单（MAI）」），
    // 这样 f3 与 f2 都能在 设置 → 控制 里改。F7（区域候选）仍固定。
    function listKeyBind(){
        if(KB_LIST === null && !KB_LIST_TRIED){
            KB_LIST_TRIED = true;
            try{
                var dflt = new Packages.arc.input.KeyBind.Axis(Packages.arc.input.KeyCode.f2);
                KB_LIST = Packages.arc.input.KeyBind.add("mai-list", dflt, "mlogauxiliary");
                log("keybind registered: mai-list");
            }catch(e){
                KB_LIST = null;
                log("keybind add failed (list): " + e);
            }
        }
        return KB_LIST;
    }

    function listKeyAxis(){
        var kb = listKeyBind();
        if(kb === null || kb === undefined){ return null; }
        try{
            var ax = kb.value;
            if(ax === null || ax === undefined){ return null; }
            return ax;
        }catch(e){ return null; }
    }

    // 单击判定（清单是「按一下就开」）：单键用 key，区间用 min。
    function listKeyTap(){
        var ax = listKeyAxis();
        if(ax === null || ax === undefined){ return hotTap("f2"); }
        try{
            if(ax.key !== null && ax.key !== undefined){ return Core.input.keyTap(ax.key) === true; }
            if(ax.min !== null && ax.min !== undefined){ return Core.input.keyTap(ax.min) === true; }
        }catch(e){ log("listKeyTap failed: " + e); }
        return false;
    }

    function listKeyName(){
        var ax = listKeyAxis();
        if(ax !== null && ax !== undefined){
            try{
                if(ax.key !== null && ax.key !== undefined){ return ax.key.getName(); }
            }catch(e1){ }
            try{
                if(ax.min !== null && ax.min !== undefined){
                    var a = ax.min.getName();
                    if(ax.max !== null && ax.max !== undefined && ax.max !== ax.min){
                        a = a + " / " + ax.max.getName();
                    }
                    return a;
                }
            }catch(e2){ }
        }
        return "f2";
    }

    function keyByName(name){
        try{ return Packages.arc.input.KeyCode[String(name)]; }catch(e){ return null; }
    }

    function hotTap(name){
        var k = keyByName(name);
        if(k === null || k === undefined){ return false; }
        try{ return Core.input.keyTap(k) === true; }catch(e){ return false; }
    }

    // 6. 尽量让 sidecar 无感：后端还在启动时，投递先排队，就绪那一刻自动发出去，
    // 不用玩家自己再点一遍。
    var PENDING_SUBMITS = [];
    var pendingHinted = false;

    function flushPendingSubmits(){
        if(PENDING_SUBMITS.length === 0){ return; }
        var now = Packages.java.lang.System.currentTimeMillis();
        var keep = [], i;
        for(i = 0; i < PENDING_SUBMITS.length; i++){
            if(now - PENDING_SUBMITS[i].at <= 60000){ keep.push(PENDING_SUBMITS[i]); }
        }
        PENDING_SUBMITS = [];
        pendingHinted = false;
        if(keep.length === 0){ return; }
        hint("后端就绪，自动投递 " + keep.length + " 个作业");
        for(i = 0; i < keep.length; i++){
            try{ jobSubmit(keep[i].b, keep[i].desc, keep[i].cb, keep[i].wsInfo, keep[i].batch); }
            catch(e){ log("pending submit failed: " + e); }
        }
    }

    // 1. 自动写入：只写 done 且留了载荷的，写完照旧记一笔框角状态。
    // v0.18.1（用户 m10374）：「开启自动写入也不生效」。
    // 补三件事：①拿不到处理器时再用作业自己的 origin 找一次（框角批量那条路的 b 就是
    // lookupBuild 的结果，单台面板那条路给的是 mine.b —— 两条都兜住）；
    // ②每次跳过都 log 一行原因，不再静默；③写上之后照旧记一笔框角状态。
    function autoWriteApply(b, job){
        if(!autoWriteOn()){ return null; }
        if(job === null || job === undefined){ return null; }
        if(b === null || b === undefined){ b = lookupBuild(job); }
        if(b === null || b === undefined){
            log("auto write skipped: no processor for job " + String(job.id));
            return null;
        }
        if(String(job.state) !== "done"){ return null; }
        if(String(job.bytes_b64 || "").length === 0){
            log("auto write skipped: job " + String(job.id) + " has no payload");
            return null;
        }
        var r = "";
        try{ r = String(writeBuild(b, jobAsPlan(job))); }
        catch(e){ r = "自动写入失败：" + e; }
        try{ jobBoxWritten(b); }catch(e2){ }
        hint(r);
        log("auto write job " + String(job.id) + " -> " + r);
        return r;
    }

    // 造一行灰底容器。Styles/Tex 在无头环境里可能还没初始化（静态字段要 atlas），
    // 所以一律 try：拿不到就用无背景的 Table，功能一样，只是不好看。
    function realRow(){
        try{ return new Packages.arc.scene.ui.layout.Table(Packages.mindustry.ui.Styles.grayPanel); }
        catch(e){ return new Packages.arc.scene.ui.layout.Table(); }
    }

    function section(t, title){
        var sc = t.add(title);
        try{ sc.color(themeColor()); }catch(e0){ }
        try{ sc.width(ROW_W); }catch(e1){ }
        try{ sc.center(); }catch(e2){ }
        try{ sc.padTop(12); sc.padBottom(2); }catch(e3){ }
        try{ alignText(sc.get(), Packages.arc.util.Align.center); }catch(e4){ }
        try{ sc.row(); }catch(e5){ }
        try{
            t.image(Packages.mindustry.gen.Tex.whiteui, themeColor()).growX().height(3).padTop(2).padBottom(4).row();
        }catch(e){
            t.add("").height(4).row();
        }
    }

    // mk 是行工厂：真机用 realRow()，selfTest 喂假表（无 UI 时真 Table 的 labelWrap 会 NPE）。
    // v0.17.0：多一个 expl 参数 —— 长解释挂到行尾的 infoCircle 上，只在悬浮时显示。
    function kv(t, k, v, mk, expl){
        mk = mk || realRow;
        var hasExpl = (expl !== null && expl !== undefined && String(expl).length > 0);
        var row = mk();
        row.margin(8).left();
        if(k.length > 0){ row.add(k).width(LABEL_W).left().color(Pal.gray); }
        row.labelWrap(String(v)).width(hasExpl ? (VALUE_W - INFO_W) : VALUE_W).left();
        if(hasExpl){ infoIcon(row, expl); }
        t.add(row).width(ROW_W).left().padTop(5).row();
    }

    function buttonRow(t, labels, handlers, mk){
        mk = mk || realRow;
        var row = mk();
        row.margin(6).left();
        for(var i = 0; i < labels.length; i++){
            row.button(labels[i], handlers[i]).height(38).padRight(6);
        }
        t.add(row).width(ROW_W).left().padTop(5).row();
    }

    function copyDiag(){
        try{
            Core.app.setClipboardText(diagText());
            if(Vars.ui !== null){ Vars.ui.showText("Mlog辅助 · 诊断", "诊断已复制。"); }
        }catch(e){ log("clipboard failed: " + e); }
    }

    // v0.14.4（用户 2026-11-01）：「运行环境应该放在最后」「删除sidecar」「默认当隐藏API密钥」
    // 「增加一个设置选项，是否将AI工作提示发送到服务器聊天框，默认关闭」。
    // 顺序固定：大模型 → 聊天播报 → 快捷动作 → 本地快检 → 说明 → 运行环境（永远最后）。
    // sidecar 不再单独成段：地址固定本机 8765、随游戏自启，出问题时看运行环境那一行。
    // v0.18.1（用户 m10327）：「隐藏密钥按钮使用游戏原有的 :eye: 图标放在密钥栏最右侧」。
    // UI.formatIcons 把 ":eye:" / ":eyeOff:" 换成图标字体里的字形；两个字形都在
    // （扫 out/_server160.jar 的 mindustry/gen/Icon.class 常量池确认有 eye / eyeOff / eyeSmall）。
    // 字号跟随按钮样式，所以按钮里写通行标记即可，不必自己贴图。
    function eyeMark(hidden){
        var mark = hidden ? ":eye:" : ":eyeOff:";
        try{ return String(Packages.mindustry.core.UI.formatIcons(mark)); }
        catch(e){ log("eye mark failed: " + e); return hidden ? "显示" : "隐藏"; }
    }

    function buildSettingsTable(t, mk){
        mk = mk || realRow;
        SETTINGS_LIVE = true;
        t.defaults().pad(3).left();
        // v0.17.0（用户 m09440 / 2）：主标题两行居中、加大。
        titleLine(t, "MAI", 1.9, themeColor());
        titleLine(t, "MlogAuxIliary", 1.2, themeColor());
        titleLine(t, "v" + MOD_VERSION + " · 说一句要什么 → 生成 → 预检 → 写入", 0.9, Pal.gray);
        var parts = diagParts(), i;

        section(t, "大模型");
        kv(t, "档位", llmText(), mk, "启用后由模型直接写逻辑：描述交给模型，模型写 mlog，本模组只做预检与写入。");
        var lrow = mk();
        lrow.margin(8).left();
        lrow.add("地址").width(LABEL_W).left().color(Pal.gray);
        lrow.field(llmUrl(), function(v){
            llmSave(LLM_URL_KEY, String(v));
            log("llm url = " + v);
        }).width(300).left();
        t.add(lrow).width(ROW_W).left().padTop(5).row();
        // 密钥默认遮成 *：setPasswordMode 只换显示，明文仍在 Core.settings 里。
        // v0.12 只调了 setPasswordMode(true)，但 Fonts.def 里没有 bullet 字形，等于没遮；
        // v0.14.4 补 setPasswordCharacter('*')，并给一个「显示密钥」按钮按需看明文。
        var keyField = null;
        var krow = mk();
        krow.margin(8).left();
        krow.add("密钥").width(LABEL_W).left().color(Pal.gray);
        var kcell = krow.field(llmKey(), function(v){
            llmSave(LLM_KEY_KEY, String(v));
            log("llm key saved (" + String(v).length + " chars)");
        }).width(300).left();
        try{
            keyField = kcell.get();
            keyField.setPasswordMode(true);
            try{ keyField.setPasswordCharacter('*'); }catch(e1){ log("password char skipped: " + e1); }
        }catch(e){ log("password mode skipped (this table has no cell.get)"); }
        // v0.18.1（用户 m10327）：显隐不再是另起一行的文字按钮，而是这一行最右侧的眼睛图标。
        // expandX + right 把按钮推到行尾（行宽固定 ROW_W，值区 300，剩下的空间全给它）。
        var keyShown = false;
        var ktoggle = krow.button(eyeMark(true), function(){
            try{
                keyShown = !keyShown;
                keyField.setPasswordMode(!keyShown);
                ktoggle.get().setText(eyeMark(!keyShown));
            }catch(e2){ log("password toggle skipped: " + e2); }
        }).height(34).width(46);
        try{ ktoggle.expandX(); ktoggle.right(); }catch(e3){ log("eye align skipped: " + e3); }
        try{ tipOn(ktoggle.get(), "显示或隐藏密钥明文。密钥本身存在 Core.settings 里，只是遮住显示。"); }
        catch(e4){ log("eye tip skipped: " + e4); }
        t.add(krow).width(ROW_W).left().padTop(5).row();
        var mrow = mk();
        mrow.margin(8).left();
        mrow.add("模型").width(LABEL_W).left().color(Pal.gray);
        mrow.field(llmModel(), function(v){
            llmSave(LLM_MODEL_KEY, String(v));
            log("llm model = " + v);
        }).width(300).left();
        t.add(mrow).width(ROW_W).left().padTop(5).row();
        buttonRow(t, ["获取模型", "测试端点", "启用模型"], [
            function(){ showLlmModels(); },
            function(){ llmProbe(); },
            function(){ llmEnable(); }
        ], mk);
        infoIcon(lrow, "地址填 OpenAI 兼容的 /chat/completions（例：" + LLM_DEFAULT_URL
           + "），只填到 /v1 也可以，sidecar 会自动补全。密钥只存于本机 Core.settings 与 sidecar 内存，"
           + "界面默认遮成 *，点「显示密钥」可查看。改完回车保存。");

        section(t, "聊天播报");
        var crow = mk();
        crow.margin(6).left();
        var csend = crow.button("发送到服务器聊天框：" + (chatSendOn() ? "开" : "关"), function(){
            toggleChatSend();
            try{ csend.get().setText("发送到服务器聊天框：" + (chatSendOn() ? "开" : "关")); }
            catch(e0){ log("chat toggle label not updated"); }
        }).height(38).padRight(6);
        t.add(crow).width(ROW_W).left().padTop(5).row();
        infoIcon(crow, "开启后，作业的启用与完成状态会发到联机聊天框（MAI<逻辑徽标> 状态 (x,y): 信息）；"
           + "关闭时只在本机提示。默认关闭。");

        section(t, "框选控件");
        // v0.18.1（用户 m10374）：「框选控件缩放做成滑条，参考原版设置」。
        // 原版的滑条就是 arc 的 Slider（Slider.java:37 `Slider(float,float,float,boolean)`），
        // 但拿不到 changed(Runnable)，所以在 update 钩子里靠 hudSliderTick() 每帧对读数；
        // 无头环境（Core.scene 为 null，样式建不出来）退化成原来那一格按钮。
        var hrow = mk();
        hrow.margin(8).left();
        hrow.add("缩放").width(LABEL_W).left().color(Pal.gray);
        var sliderMade = false;
        try{
            if(Core.scene !== null && Core.scene !== undefined){
                var hsl = new Packages.arc.scene.ui.Slider(HUD_SCALE_MIN, HUD_SCALE_MAX, HUD_SCALE_STEP, false);
                var hsc = hrow.add(hsl).width(VALUE_W - 120).left();
                hudSlider = hsc.get();
                hudSlider.setValue(hudScale());
                hudSliderLast = hudScale();
                sliderMade = true;
            }
        }catch(e5){
            hudSlider = null;
            log("hud slider unavailable, falling back to buttons: " + e5);
        }
        if(!sliderMade){
            hudScaleBtn = hrow.button(hudScaleBtnText(), function(){ hudScaleSet(1); hudScaleRefresh(); })
                .height(38).padRight(6);
        }
        hudScaleLbl = hrow.labelWrap(hudScaleText()).width(70).left();
        infoIcon(hrow, "框角按钮和状态条的大小倍率（" + HUD_SCALE_MIN + "×–" + HUD_SCALE_MAX
           + "×，步长 " + HUD_SCALE_STEP + "×）。拖滑条即时生效，按钮、字号、留白一起缩放。");
        t.add(hrow).width(ROW_W).left().padTop(5).row();
        // v0.18.1：把「当前绑成了什么键」直接显示出来，省得去按键绑定界面猜。
        kv(t, "框选键", selKeyName(), mk,
           "在 设置 → 控制 → 按键绑定 里改：分类 MAI、条目「框选处理器（MAI）」。"
           + "按住这颗键再拖鼠标就是框选；区间绑定取第一个键。");
        kv(t, "清单键", listKeyName(), mk,
            "在 设置 → 控制 → 按键绑定 里改：分类 MAI、条目「处理器清单（MAI）」。按一下列出全图处理器。");

        // v0.18.2（用户 m10938）：「允许在设置中调节主题色和强调色」。
        section(t, "外观");
        colorRow(t, "主题色", THEME_KEY, THEME_DEFAULT, mk,
            "选框、标题、分隔线用的颜色。写 6 位十六进制（例如 " + THEME_DEFAULT + "），改完选框立刻变。");
        colorRow(t, "强调色", EMPH_KEY, emphDefaultHex(), mk,
            "框里那些逻辑处理器的高亮颜色。默认取游戏 [highlight] 的算法（accent 向白 30%）。");

        section(t, "写入");
        var awrow = mk();
        awrow.margin(6).left();
        autoWriteLbl = awrow.button(autoWriteText(), function(){
            autoWriteSet(!autoWriteOn());
            autoWriteRefresh();
        }).height(38).padRight(6);
        infoIcon(awrow, "开着时，作业生成完成、并且留了载荷，就自动写进那台处理器；"
           + "关着时点作业详情里的「写入」。默认关。");
        t.add(awrow).width(ROW_W).left().padTop(5).row();

        // v0.18.3（用户 m11124）：「诊断全部移动到最近提示页」「按钮只要一个复制」。
        // 版本 / 快检 / sidecar / 模型 / 目录都摊在这一页，出问题点一次「复制」整段带走。
        section(t, "诊断与最近提示");
        var hrow = mk();
        hrow.margin(8).left();
        var hcell = hrow.labelWrap(hintLogText()).width(VALUE_W - INFO_W).left();
        try{ lastHintLog = hcell.get(); }catch(e5){ lastHintLog = null; }
        infoIcon(hrow, "最近的提示日志（错误也在里面），只在本机保留最近 12 条；"
           + "屏幕上的提示只闪一下，要留证据就点下面的「复制」。");
        t.add(hrow).width(ROW_W).left().padTop(5).row();
        var hbtn = mk();
        hbtn.margin(6).left();
        lastHintLbl = hbtn.button("复制", function(){ hintLogCopy(); }).height(38).padRight(6);
        t.add(hbtn).width(ROW_W).left().padTop(5).row();
        for(i = 0; i < parts.length; i++){ kv(t, parts[i].k, parts[i].v, mk); }

        section(t, "运行环境");
        buttonRow(t, ["首次配置向导"], [function(){ welcomeOpen(); }], mk);
        SETTINGS_LIVE = false;
    }

    // 区域面板的布线（v0.6.0）：框选松手之后不再只弹一段纯文本，而是把「区域摘要 +
    // 每个处理器的链接候选」放进可滚动的对话框，并且每个处理器给一个「生成」按钮。
    // 这里只碰 table 的接口、不碰引擎类，所以能像设置页一样用假表在无 UI 环境里数行数。
    function buildRegionTable(t, snap, head, candText, pick){
        t.add(String(head)).left().row();
        t.labelWrap(String(candText)).left().growX().row();
        var ps = (snap && snap.processors) || [], i;
        if(ps.length === 0){
            t.add("（区域里没有处理器）").left().row();
        }
        for(i = 0; i < ps.length && i < 8; i++){
            (function(p){
                t.button("用 @" + p.x + "," + p.y + " 生成", function(){ pick(p.x, p.y); })
                 .width(240).left().row();
            })(ps[i]);
        }
        if(ps.length > 8){
            t.add("…还有 " + (ps.length - 8) + " 个处理器").left().row();
        }
        return t;
    }

    // ---------------------------------------------------------- 处理器清单（v0.7.0，F2）
    // 全图扫一遍处理器，按「问题多的排前面」列出来，点一行就跳过去并开 AI 面板。
    // 这三个只碰 table 的接口（不碰引擎类），所以 selfTest 能用假表数行数。
    function listHead(res){
        var errs = 0, warns = 0, i;
        for(i = 0; i < res.rows.length; i++){ errs += res.rows[i].err; warns += res.rows[i].warn; }
        return "全图 " + res.rows.length + " 个处理器，ERR " + errs + " / WARN " + warns
             + "，" + res.ms + " ms"
             + (res.api === true ? "" : "（快检不可用，只列清单）");
    }

    function listRowLabel(r){
        var tail = "";
        if(r.err > 0){ tail += "  ERR " + r.err; }
        if(r.warn > 0){ tail += "  WARN " + r.warn; }
        if(r.err === 0 && r.warn === 0){ tail = "  干净"; }
        return "@" + r.x + "," + r.y + "  " + r.lines + " 行" + tail;
    }

    function buildListTable(t, res, head, pick){
        var rows = res.rows || [], max = 24, i;
        t.add(String(head)).left().row();
        if(rows.length === 0){ t.add("这张图上没有处理器").left().row(); }
        for(i = 0; i < rows.length && i < max; i++){
            (function(row){
                t.button(listRowLabel(row), function(){ pick(row.x, row.y); })
                 .width(320).left().row();
            })(rows[i]);
        }
        if(rows.length > max){ t.add("…还有 " + (rows.length - max) + " 个处理器").left().row(); }
        return t;
    }


    // 无 UI 环境下也能验证设置表格的布线：喂一张假表，数它被填了多少行/格。
    // 假表只实现真实页面用到的方法 —— 少实现一个，selfTest 立刻炸；
    // 同时把用到的名字记下来，供 run_ui_smoke 去真引擎里复核「有没有重载歧义」。
    // 假表：无 UI 环境里真 Table 的 labelWrap/field 会 NPE（scene.getStyle 需要 skin），
    // 所以 selfTest 用这个骨架跑一遍布线。它同时记下「用到了哪些 Table/Cell 方法」，
    // 测试再把这些名字拿到真引擎里逐个解析 —— 重载歧义只有在真引擎里才暴露得出来。
    // 一个对象同时当「表」和「格子」用：所有方法都返回自己，链式调用就一条路。
    function fakeTable(){
        var n = 0;
        var used = [];
        function hit(m){ used.push(m); }
        var node = {
            pad: function(){ hit("pad"); return node; },
            padTop: function(){ hit("padTop"); return node; },
            padBottom: function(){ hit("padBottom"); return node; },
            padRight: function(){ hit("padRight"); return node; },
            left: function(){ hit("left"); return node; },
            right: function(){ hit("right"); return node; },
            width: function(){ hit("width"); return node; },
            height: function(){ hit("height"); return node; },
            growX: function(){ hit("growX"); return node; },
            color: function(){ hit("color"); return node; },
            colspan: function(){ hit("colspan"); return node; },
            margin: function(){ hit("margin"); return node; },
            defaults: function(){ hit("defaults"); return node; },
            row: function(){ n++; hit("row"); return node; },
            clear: function(){ hit("clear"); return node; },
            add: function(){ n++; hit("add"); return node; },
            image: function(){ n++; hit("image"); return node; },
            // 下面这些在真机上作用于 Cell / Label（不是 Table 的 API），
            // 所以这里实现但**不记进 used**：smoke 只拿 used 去真引擎里解析 Table 方法。
            size: function(){ return node; },
            expandX: function(){ return node; },
            padLeft: function(){ return node; },
            center: function(){ return node; },
            get: function(){ return node; },
            addListener: function(){ return node; },
            setText: function(){ return node; },
            setFontScale: function(){ return node; },
            setColor: function(){ return node; },
            label: function(){ n++; hit("label"); return node; },
            labelWrap: function(){ n++; hit("labelWrap"); return node; },
            field: function(v, f){ n++; hit("field"); return node; },
            button: function(s, f){ n++; hit("button"); return node; }
        };
        return {table: node, count: function(){ return n; },
                methods: function(){
                    var out = [], seen = {};
                    for(var i = 0; i < used.length; i++){
                        if(seen[used[i]] !== true){ seen[used[i]] = true; out.push(used[i]); }
                    }
                    out.sort();
                    return out.join(",");
                }};
    }

    function selfTest(){
        var ft = fakeTable();
        try{ buildSettingsTable(ft.table, function(){ return ft.table; }); }
        catch(e){ return "设置表格布线失败：" + e; }
        var rg = fakeTable();
        var fakeSnap = {processors: [{x: 1, y: 2, block: "micro-processor", links: []},
                                     {x: 5, y: 6, block: "micro-processor", links: []}]};
        try{ buildRegionTable(rg.table, fakeSnap, "区域 0,0 - 9,9 方块 4 处理器 2", "候选文本", function(){}); }
        catch(e){ return "区域表格布线失败：" + e; }
        var ls = fakeTable();
        var fakeList = {rows: [{x: 1, y: 2, lines: 4, err: 1, warn: 0, rules: "undefined-var"},
                               {x: 5, y: 6, lines: 2, err: 0, warn: 2, rules: "printflush-clears-buffer"}],
                        ms: 3, api: true};
        try{ buildListTable(ls.table, fakeList, listHead(fakeList), function(){}); }
        catch(e){ return "清单表格布线失败：" + e; }
        var fakeB = {tile: {x: 3, y: 4}, code: "set n 0\nprint n\nend", links: {size: 0}};
        var snapOk = "no";
        try{
            var back = parse(snapshotText(fakeB, "selftest"));
            snapOk = (back !== null && back.x === 3 && back.y === 4 && back.code === fakeB.code) ? "ok" : "bad";
        }catch(e){ snapOk = "fail"; }
        if(snapOk !== "ok"){ return "撤销快照序列化失败：" + snapOk; }
        return "设置表格可建，填了 " + ft.count() + " 行/格；区域表格 " + rg.count()
               + " 行；清单表格 " + ls.count() + " 行；诊断 " + diagLines().length
               + " 行；撤销快照 " + snapOk + "；用到的方法 " + ft.methods();
    }

    // 设置分类的图标用原版逻辑徽标（图标字体里的 glyph，和 @editor.worldprocessors 那个按钮同一个）。
    // 它由 Icon 的静态初始化从 Fonts.def 造出来，个别环境可能还是 null —— 那时退回无图标写法，
    // 不传 null 给 addCategory（String/Drawable 两个重载会挑不出来）。
    function categoryIcon(){
        try{
            var ic = Packages.mindustry.gen.Icon.logic;
            if(ic !== null && ic !== undefined){ return ic; }
        }catch(e){ log("logic icon unavailable: " + e); }
        return null;
    }

    function registerSettings(){
        if(settingsDone){ return true; }
        try{
            if(Vars.ui === null || Vars.ui.settings === null){ return false; }
            var icon = categoryIcon();
            if(icon !== null){
                Vars.ui.settings.addCategory("Mlog辅助", icon, function(t){ buildSettingsTable(t); });
            }else{
                Vars.ui.settings.addCategory("Mlog辅助", function(t){ buildSettingsTable(t); });
            }
            settingsDone = true;
            settingsErr = null;
            log("settings category registered");
            return true;
        }catch(e){
            settingsErr = String(e);
            log("settings category failed: " + e);
            return false;
        }
    }


    try{
        var Core    = Packages.arc.Core;
        var Events  = Packages.arc.Events;
        var Threads = Packages.arc.util.Threads;
        var Vars    = Packages.mindustry.Vars;
        var ET      = Packages.mindustry.game.EventType;
        var Groups  = Packages.mindustry.gen.Groups;
        var Pal     = Packages.mindustry.graphics.Pal;

        // P2 的两个模块。游戏只执行 scripts/main.js（Mods.loadScripts:809 只取 main.js，
        // 除非 scripts/ 下只有一个文件），其余文件必须 require 进来 —— Scripts.java:30-32
        // 给脚本作用域装了 CommonJS 模块提供器，模块里 module.exports 就是取到的东西。
        var QUICK = null, RULES = null;
        try{
            RULES = require("rules.generated");
            QUICK = require("quickcheck");
            log("quick check loaded: " + countKeys(RULES.instr) + " instruction(s)");
        }catch(e){
            log("quick check unavailable (" + e + ") -- 面板会少一行本地预检");
        }

        var base = String(Core.settings.getString(URL_KEY, DEFAULT_URL));
// 旧版本把地址存在 mlogauxiliary 之前的名字下（logicwriter-url）：存在就搬过来一次，读的人不用重配。
(function(){
    try{
        var legacy = Core.settings.getString("logic" + "writer-url", null);
        if(legacy !== null && legacy !== undefined && String(legacy).length > 0){
            if(String(Core.settings.getString(URL_KEY, "")).length === 0){
                Core.settings.put(URL_KEY, String(legacy)); base = String(legacy);
                log("migrated sidecar url from the old key: " + base);
            }
        }
    }catch(e){ log("legacy url migration skipped: " + e); }
})();
        var undoStack = [];        // P1：内存快照，重启即失效（P3 落盘）

        // ---------------------------------------------------------- 大模型（v0.11.0）
        // 玩家在设置页填 地址 / 密钥 / 模型，模组只把它们发给本机 sidecar 的 POST /llm，
        // sidecar 在进程里把 provider 换成模型档 —— 不用重启 sidecar。密钥只留在本机
        // Core.settings 与 sidecar 内存里：面板、诊断、日志永远只显示尾巴。
        var LLM_DEFAULT_URL = "https://api.deepseek.com/v1/chat/completions";
        // v0.14.4：模板档已删，默认就是「未配置模型」——没配模型时只提示，不生成任何代码。
        var llmState = {kind: "none", url: "", model: "", key_set: false, key_hint: "", timeout: 60};

        function llmGet(key, dflt){
            try{
                var v = Core.settings.getString(key, dflt);
                return (v === null || v === undefined) ? dflt : String(v);
            }catch(e){ return dflt; }
        }
        function llmUrl(){ return llmGet(LLM_URL_KEY, ""); }
        function llmModel(){ return llmGet(LLM_MODEL_KEY, ""); }
        function llmKey(){ return llmGet(LLM_KEY_KEY, ""); }
        function llmSave(key, v){
            try{ Core.settings.put(key, String(v)); }
            catch(e){ log("save " + key + " failed: " + e); }
        }

        function llmText(){
            var tier = (llmState.kind === "llm") ? "模型档"
                     : (llmState.kind === "fake" ? "回放档" : "未配置模型");
            var url = llmState.url.length > 0 ? llmState.url : (llmUrl().length > 0 ? llmUrl() : "未填");
            var model = llmState.model.length > 0 ? llmState.model
                      : (llmModel().length > 0 ? llmModel() : "未填");
            var key = llmState.key_set ? ("已填 " + String(llmState.key_hint))
                    : (llmKey().length > 0 ? "已填（未同步）" : "未填");
            return tier + " · " + url + " · " + model + " · 密钥 " + key;
        }

        function llmNote(text){
            if(Vars.ui !== null){ Vars.ui.showText("Mlog辅助 · 大模型", String(text)); }
            else{ print("[mai] llm: " + text); }
        }

        function llmRefresh(cb){
            httpJson("GET", "/llm", null, function(code, text){
                var d = parse(text);
                if(d !== null && d.ok === true){ llmState = d; }
                if(cb){ cb(code, d, text); }
            });
        }

        function llmApply(body, cb){
            httpJson("POST", "/llm", body, function(code, text){
                var d = parse(text);
                if(d !== null && d.ok === true){ llmState = d; }
                if(cb){ cb(code, d, text); }
            });
        }

        // 「获取模型」：模组自己的裸 Socket 只走明文 HTTP，所以 https 端点必须由 sidecar 代取。
        function llmFetchModels(cb){
            var url = llmUrl();
            if(url.length === 0){ cb(false, null, "先填模型地址（例如 " + LLM_DEFAULT_URL + "）"); return; }
            httpJson("POST", "/models", JSON.stringify({url: url, key: llmKey()}), function(code, text){
                var d = parse(text);
                if(d === null){ cb(false, null, "sidecar 没回 JSON：" + jobWhy(code, text, null)); return; }
                if(d.ok !== true){ cb(false, null, String(d.error || "取模型列表失败")); return; }
                cb(true, d, "");
            });
        }

        // 选模型：Dialog + cont.pane(host)，host 必须是真 Table
        // （v0.4.1 的教训：pane() 交回来的是 ScrollPane，它没有 add）。
        function showPickList(title, items, onPick){
            var d = new Packages.arc.scene.ui.Dialog(String(title));
            d.addCloseButton();
            try{ d.closeOnBack(); }catch(e){}
            var host = new Packages.arc.scene.ui.layout.Table();
            try{ d.cont.pane(host).width(560).height(360); }
            catch(e){ d.cont.add(host).growX(); }
            var n = 0;
            for(var i = 0; i < items.length && n < 80; i++){
                (function(item){
                    host.button(String(item), function(){
                        try{ onPick(String(item)); }catch(e2){ log("pick failed: " + e2); }
                        d.hide();
                    }).growX().height(36).padBottom(3).row();
                })(items[i]);
                n++;
            }
            if(n === 0){ host.add("（端点没有返回模型，可手填模型名）").left().row(); }
            d.cont.row();
            d.cont.button("关闭", function(){ d.hide(); }).height(40).width(120).padTop(8);
            d.show();
        }

        function showLlmModels(){
            log("asking the endpoint for its model list");
            llmFetchModels(function(ok, doc, err){
                if(!ok){ llmNote("取模型列表失败：" + err); return; }
                var list = (doc && doc.models) || [];
                showPickList("Mlog辅助 · 选择模型（" + list.length + "）", list, function(m){
                    llmSave(LLM_MODEL_KEY, m);
                    llmNote("模型已选：" + m + "\n点「启用模型」后生效。");
                });
            });
        }

        function llmEnable(){
            var url = llmUrl();
            if(url.length === 0){ llmNote("先填模型地址（例如 " + LLM_DEFAULT_URL + "）"); return; }
            var want = llmModel();
            llmApply(JSON.stringify({kind: "llm", url: url, model: want, key: llmKey()}),
                function(code, d, text){
                    if(d === null || d.ok !== true){
                        llmNote("启用失败：" + ((d && d.error) ? String(d.error) : jobWhy(code, text, null)));
                        return;
                    }
                    var head = "已切到模型档：" + String(d.model || "(端点默认)")
                        + "\n地址：" + String(d.url)
                        + "\n此后描述意图由模型直接写逻辑。";
                    // v0.12.1：模型名填错时端点大多不报错，只回一堆空回复，作业里只会看到 empty reply；
                    // 所以启用时顺手拿一次端点列表比一比，对不上就当场说出来。
                    llmFetchModels(function(ok, doc, err){
                        var list = (ok && doc && doc.models) || [];
                        if(ok && want.length > 0 && list.indexOf(want) < 0){
                            llmNote(head + "\n注意：模型 " + want + " 不在端点列表（现有："
                                + list.join(", ") + "）");
                        }else{
                            llmNote(head);
                        }
                    });
                });
        }

        // ---------------------------------------------------------- 传输层
        // 不用 java.net.URL.openConnection()：它返回的是 sun.net.www.protocol.http.HttpURLConnection，
        // 属于 java.base 不 export 的 JDK 内部类；Rhino 按运行期类做成员绑定（MemberBox），于是
        // setRequestMethod / getResponseCode 这类被内部类覆写的方法一调就抛
        //   IllegalAccessException: class rhino.MemberBox cannot access class
        //   sun.net.www.protocol.http.HttpURLConnection (in module java.base)
        // —— v0.4.3 真机实测。这里退回 java.net.Socket：请求行与头自己写，响应读到 EOF
        // （请求带 Connection: close，python 的 BaseHTTPRequestHandler 是 HTTP/1.0，读完就关）。
        // 只用公开类（Socket / InputStreamReader / StringBuilder），不再碰 JDK 内部类型。
        
        // "http://host:port/path" -> {host, port, path}，host 缺省 127.0.0.1、port 缺省 8765。
        // ---------------------------------------------------------- 模型档自愈（v0.12.1）

        // sidecar 重启会把它内存里的模型档配置忘干净（配置只在进程里），
        // 结果玩家明明设置过，重启后却静悄悄退回模板档、报「没匹配到样板」。
        // 模组的 Core.settings 才是源头，所以启动时与每次开面板都把设置再推一遍。
        function llmSync(cb){
            var url = llmUrl();
            if(url.length === 0){ if(cb){ cb(false, "no url saved"); } return; }
            var want = llmModel();
            llmRefresh(function(code, d, text){
                if(d === null || d.ok !== true){
                    if(cb){ cb(false, "sidecar unreachable"); }
                    return;
                }
                if(d.kind === "llm" && String(d.model || "") === want){
                    if(cb){ cb(true, "already synced"); }
                    return;
                }
                llmApply(JSON.stringify({kind: "llm", url: url, model: want, key: llmKey()}),
                    function(c2, d2, t2){
                        if(d2 !== null && d2.ok === true){
                            log("llm tier re-synced from settings: " + String(d2.url));
                            if(cb){ cb(true, "synced"); }
                        }else if(cb){
                            cb(false, (d2 && d2.error) ? String(d2.error) : jobWhy(c2, t2, null));
                        }
                    });
            });
        }

        // 「测试端点」：直接问一次端点，把 HTTP 状态、finish_reason、回复长度都摊开。
        // 作业路径只会说「empty reply」，玩家无从判断是地址错、模型名错还是被 token 截断。
        function llmProbe(){
            var url = llmUrl();
            if(url.length === 0){ llmNote("先填模型地址（例如 " + LLM_DEFAULT_URL + "）"); return; }
            llmNote("正在测试端点…");
            httpJson("POST", "/llm/probe",
                JSON.stringify({url: url, model: llmModel(), key: llmKey()}),
                function(code, text){
                    var d = parse(text);
                    if(d === null){ llmNote("sidecar 没回 JSON：" + jobWhy(code, text, null)); return; }
                    if(d.ok !== true){ llmNote("测试失败：" + String(d.error || jobWhy(code, text, null))); return; }
                    var lines = [];
                    lines.push("地址：" + String(d.url));
                    lines.push("HTTP " + String(d.status) + " · 模型 " + String(d.model || "(端点默认)"));
                    if(d.error){ lines.push("错误：" + String(d.error)); }
                    if(d.finish_reason !== undefined && d.finish_reason !== null){
                        lines.push("finish_reason：" + String(d.finish_reason));
                    }
                    if(d.content_len !== undefined){ lines.push("回复长度：" + String(d.content_len) + " 字符"); }
                    if(d.reasoning_len !== undefined){
                        lines.push("仅返回 reasoning（" + String(d.reasoning_len) + " 字符），content 为空");
                    }
                    if(d.content){ lines.push("回复：" + String(d.content).slice(0, 160)); }
                    else if(d.raw){ lines.push("原文：" + String(d.raw).slice(0, 200)); }
                    llmNote(lines.join("\n"));
                });
        }

        // ---------------------------------------------------------- 首次配置向导（v0.18.0）
        // 用户 m10072：「第一次启动时候弹出欢迎和快速配置页面，并在验证 sidecar 可用，
        // 模型配置完成之后重启。」只在第一次自动弹（标记写 Core.settings）；
        // 「稍后再说」也记标记，之后从「设置 → Mlog辅助 → 大模型」里的按钮重开。
        var WELCOME_KEY = "mlogauxiliary-welcome-done";
        var WELCOME = {dlg: null, url: "", key: "", model: "", note: "", sideNote: "",
                       okSide: false, okLlm: false, busy: false, tried: false};

        function welcomeSeen(){
            try{ return Core.settings.getBool(WELCOME_KEY, false) === true; }
            catch(e){ return true; }        // 读不到就当看过，别每次进游戏都弹
        }

        function welcomeMarkSeen(){
            try{ Core.settings.put(WELCOME_KEY, true); }
            catch(e){ log("welcome flag failed: " + e); }
        }

        function welcomeSideText(){
            return sidecarStateText() + (WELCOME.okSide ? " · 可用" : " · 未确认");
        }

        // 纯布线：真机的 Dialog.cont 与无头 smoke 的假表共用（fakeTable 只认 add/label/field/button）。
        function welcomeBuild(t, mk){
            mk = mk || realRow;
            t.defaults().pad(3).left();
            titleLine(t, "欢迎使用 Mlog辅助", 1.5, Pal.accent);
            kv(t, "", "三步配好：确认后端可用 → 填好模型 → 重启游戏。"
                    + "之后按住框选键（默认 F3）框住若干处理器，写一句话就能生成、预检并写入逻辑。", mk);

            section(t, "第 1 步 · 后端");
            kv(t, "状态", welcomeSideText(), mk,
               "后端（sidecar）是模组自带的本地服务，负责编译、预检和调用模型；随游戏自启，一般不用手动管。");
            buttonRow(t, ["启动并检测后端"], [function(){ welcomeCheckSidecar(); }], mk);
            if(WELCOME.sideNote.length > 0){ kv(t, "", WELCOME.sideNote, mk); }

            section(t, "第 2 步 · 模型");
            kv(t, "当前", llmText(), mk, "模型档：填好地址、密钥和模型名后启用，此后描述意图由模型直接写逻辑。");
            var r1 = mk();
            r1.margin(8).left();
            r1.add("地址").width(LABEL_W).left().color(Pal.gray);
            r1.field(WELCOME.url, function(v){ WELCOME.url = String(v); }).width(300).left();
            t.add(r1).width(ROW_W).left().padTop(5).row();
            var r2 = mk();
            r2.margin(8).left();
            r2.add("密钥").width(LABEL_W).left().color(Pal.gray);
            r2.field(WELCOME.key, function(v){ WELCOME.key = String(v); }).width(300).left();
            t.add(r2).width(ROW_W).left().padTop(5).row();
            var r3 = mk();
            r3.margin(8).left();
            r3.add("模型").width(LABEL_W).left().color(Pal.gray);
            r3.field(WELCOME.model, function(v){ WELCOME.model = String(v); }).width(300).left();
            t.add(r3).width(ROW_W).left().padTop(5).row();
            buttonRow(t, ["获取模型", "测试端点", "保存并启用"],
                      [function(){ welcomeModels(); },
                       function(){ welcomeTest(); },
                       function(){ welcomeApply(); }], mk);
            if(WELCOME.note.length > 0){ kv(t, "", WELCOME.note, mk); }

            section(t, "第 3 步 · 重启");
            kv(t, "", "配置存在本机。重启游戏后，设置页、框选与蓝图入口全部按新配置工作。", mk);
            buttonRow(t, ["完成（稍后自己重启）", "现在退出游戏"],
                      [function(){ welcomeFinish(); },
                       function(){ welcomeExit(); }], mk);
        }

        function welcomeRender(){
            var d = WELCOME.dlg;
            if(d === null){ return; }
            var t = d.cont;
            try{ t.clear(); }catch(e){ log("welcome clear failed: " + e); }
            welcomeBuild(t, realRow);
            t.row();
            t.button("稍后再说（随时可在设置页里改）", function(){ welcomeFinish(); })
                .height(38).width(360).padTop(10);
        }

        function welcomeOpen(){
            if(Vars.ui === null){ return "welcome: no ui"; }
            if(WELCOME.dlg !== null){
                try{ WELCOME.dlg.show(); }catch(e){ }
                return "welcome: already open";
            }
            WELCOME.url = llmUrl();
            WELCOME.key = llmKey();
            WELCOME.model = llmModel();
            if(WELCOME.url.length === 0){ WELCOME.url = LLM_DEFAULT_URL; }
            WELCOME.okSide = (SIDE.state === "up");
            WELCOME.note = "";
            WELCOME.sideNote = "";
            var d = new Packages.arc.scene.ui.Dialog("Mlog辅助 · 首次配置");
            WELCOME.dlg = d;
            try{ d.closeOnBack(); }catch(e){ }
            try{ d.cont.defaults().pad(3).left(); }catch(e){ }
            welcomeRender();
            d.show();
            welcomeMarkSeen();
            try{ welcomeCheckSidecar(); }catch(e){ log("welcome sidecar check failed: " + e); }
            return "welcome: open";
        }

        // 第 1 步：先让模组拉起后端，再自己问一次 /health（ensureSidecar 是异步的）。
        function welcomeCheckSidecar(){
            WELCOME.sideNote = "正在检测后端…";
            welcomeRender();
            try{ ensureSidecar(false); }catch(e){ log("welcome ensureSidecar failed: " + e); }
            httpJson("GET", "/health", null, function(code, text){
                var d = parse(text);
                if(d !== null && d.ok === true){
                    WELCOME.okSide = true;
                    WELCOME.sideNote = "后端可用：v" + String(d.version) + " · 端口 " + String(d.port);
                    log("welcome: sidecar ok v" + String(d.version));
                }else{
                    WELCOME.okSide = false;
                    WELCOME.sideNote = "后端还没起来（" + sidecarStateText() + "）："
                        + jobWhy(code, text, d) + "。第一次启动要等它编译完，几秒后再点一次。";
                }
                welcomeRender();
            });
        }

        // 第 2 步：取模型列表（https 端点由后端代取，模组自己的裸 Socket 只走明文 HTTP）。
        function welcomeModels(){
            llmSave(LLM_URL_KEY, WELCOME.url);
            llmSave(LLM_KEY_KEY, WELCOME.key);
            WELCOME.note = "正在取模型列表…";
            welcomeRender();
            llmFetchModels(function(ok, doc, err){
                if(!ok){
                    WELCOME.note = "取模型列表失败：" + String(err) + "（也可以直接把模型名填进去）";
                    welcomeRender();
                    return;
                }
                var list = (doc && doc.models) || [];
                WELCOME.note = "端点返回 " + list.length + " 个模型，选一个后点「保存并启用」。";
                welcomeRender();
                showPickList("Mlog辅助 · 选择模型（" + list.length + "）", list, function(m){
                    WELCOME.model = m;
                    WELCOME.note = "已选模型：" + m + "。接着点「保存并启用」。";
                    welcomeRender();
                });
            });
        }

        function welcomeTest(){
            var url = WELCOME.url;
            if(url.length === 0){
                WELCOME.note = "先填模型地址（例如 " + LLM_DEFAULT_URL + "）。";
                welcomeRender();
                return;
            }
            llmSave(LLM_URL_KEY, url);
            llmSave(LLM_KEY_KEY, WELCOME.key);
            llmSave(LLM_MODEL_KEY, WELCOME.model);
            WELCOME.note = "正在测试端点…";
            welcomeRender();
            httpJson("POST", "/llm/probe",
                JSON.stringify({url: url, model: WELCOME.model, key: WELCOME.key}),
                function(code, text){
                    var d = parse(text);
                    if(d === null){
                        WELCOME.note = "后端没回 JSON：" + jobWhy(code, text, null);
                        welcomeRender();
                        return;
                    }
                    if(d.ok !== true){
                        WELCOME.note = "测试失败：" + String(d.error || jobWhy(code, text, null));
                        welcomeRender();
                        return;
                    }
                    var note = "HTTP " + String(d.status) + " · " + String(d.model || "(端点默认)");
                    if(d.finish_reason !== undefined && d.finish_reason !== null){
                        note += " · finish_reason " + String(d.finish_reason);
                    }
                    if(d.content){ note += " · 回复 " + String(d.content).slice(0, 80); }
                    else if(d.error){ note += " · 错误 " + String(d.error); }
                    WELCOME.note = "端点可用：" + note + "。接着点「保存并启用」。";
                    welcomeRender();
                });
        }

        function welcomeApply(){
            if(WELCOME.url.length === 0){
                WELCOME.note = "先填模型地址（例如 " + LLM_DEFAULT_URL + "）。";
                welcomeRender();
                return;
            }
            llmSave(LLM_URL_KEY, WELCOME.url);
            llmSave(LLM_KEY_KEY, WELCOME.key);
            llmSave(LLM_MODEL_KEY, WELCOME.model);
            WELCOME.note = "正在保存并启用…";
            welcomeRender();
            llmApply(JSON.stringify({kind: "llm", url: WELCOME.url,
                                     model: WELCOME.model, key: WELCOME.key}),
                function(code, d, text){
                    if(d === null || d.ok !== true){
                        WELCOME.note = "启用失败：" + ((d && d.error) ? String(d.error) : jobWhy(code, text, null));
                        welcomeRender();
                        return;
                    }
                    WELCOME.okLlm = true;
                    WELCOME.note = "已启用：" + String(d.model || "(端点默认)")
                        + "。最后一步：重启游戏。";
                    welcomeRender();
                });
        }

        function welcomeFinish(){
            welcomeMarkSeen();
            if(WELCOME.dlg !== null){
                var d = WELCOME.dlg;
                WELCOME.dlg = null;
                try{ d.hide(); }catch(e){ log("welcome hide failed: " + e); }
            }
            hint("首次配置已结束。重启游戏后，按住框选键框住处理器即可开始。");
            return "welcome: done";
        }

        function welcomeExit(){
            welcomeMarkSeen();
            hint("正在退出游戏；重新打开后配置生效。");
            try{ Core.app.exit(); }
            catch(e){ log("welcome exit failed: " + e); }
            return "welcome: exit";
        }

        // 每帧调，只在「第一次进游戏、没有别的对话框」时弹一次。
        function welcomeTick(){
            if(WELCOME.tried || WELCOME.dlg !== null){ return; }
            if(welcomeSeen()){ WELCOME.tried = true; return; }
            if(Vars.ui === null || Vars.ui === undefined){ return; }
            if(Core.scene === null || Core.scene === undefined){ return; }
            try{ if(Vars.state.isMenu()){ return; } }catch(e){ return; }
            if(Core.scene.hasDialog()){ return; }        // 让别的窗先关，下一帧再说
            WELCOME.tried = true;
            welcomeOpen();
        }

        function endpoint(url){
            var s = String(url);
            var m = /^https?:\/\/([^\/:]+)(?::(\d+))?(\/[^\s]*)?$/.exec(s);
            if(m === null){ return {host: "127.0.0.1", port: 8765, path: "/"}; }
            var port = m[2] === undefined ? 80 : parseInt(m[2], 10);
            if(!(port > 0 && port < 65536)){ port = 8765; }
            return {host: m[1], port: port, path: m[3] === undefined ? "/" : m[3]};
        }
        
        function readAllStr(rd){
            var sb = new Packages.java.lang.StringBuilder();
            var cbuf = Packages.java.lang.reflect.Array.newInstance(Packages.java.lang.Character.TYPE, 8192);
            var n;
            while((n = rd.read(cbuf, 0, 8192)) !== -1){
                sb.append(new Packages.java.lang.String(cbuf, 0, n));
            }
            return String(sb.toString());
        }
        
        // 同步原生请求：返回 {code, text, head}。code = -1 表示连不上/超时（text 里是原因）。
        function httpRaw(method, url, body){
            var ep = endpoint(url);
            var sock = null;
            var res = {code: -1, text: "", head: ""};
            // 注意：这里刻意不用 try/finally —— 真 v160.7 的 Rhino 在 finally 里再嵌 try/catch
            // 时会抛 ArrayIndexOutOfBoundsException(arraycopy ...)，脚本整段静默不执行。
            // 收尾改成末尾独立的 try{ if(sock!==null){ sock.close(); } }catch(e1){}。
            try{
                sock = new Packages.java.net.Socket();
                sock.connect(new Packages.java.net.InetSocketAddress(ep.host, ep.port), TIMEOUT_MS);
                sock.setSoTimeout(TIMEOUT_MS * 4);
                var payload = (body === null || body === undefined) ? null
                    : new Packages.java.lang.String(String(body)).getBytes("UTF-8");
                var head = method + " " + ep.path + " HTTP/1.1\r\n"
                         + "Host: " + ep.host + ":" + ep.port + "\r\n"
                         + "Connection: close\r\n"
                         + "Accept: application/json\r\n";
                if(payload !== null){
                    head += "Content-Type: application/json\r\n"
                          + "Content-Length: " + payload.length + "\r\n";
                }
                head += "\r\n";
                var os = sock.getOutputStream();
                os.write(new Packages.java.lang.String(head).getBytes("UTF-8"));
                if(payload !== null){ os.write(payload); }
                os.flush();
        
                var rd = new Packages.java.io.InputStreamReader(sock.getInputStream(), "UTF-8");
                var raw = readAllStr(rd);
                try{ rd.close(); }catch(e0){}
                var idx = raw.indexOf("\r\n\r\n"), sepLen = 4;
                if(idx < 0){ idx = raw.indexOf("\n\n"); sepLen = 2; }
                var headPart = idx < 0 ? raw : raw.substring(0, idx);
                var textPart = idx < 0 ? "" : raw.substring(idx + sepLen);
                var code = -1;
                // 这里原本写 /^HTTP\/1\.[01] (\d{3})/ —— 真 v160.7 的 Rhino 编译这一条正则时
                // 直接抛 IllegalStateException: FAILED ASSERTION: invalid bytecode（正则字面量
                // 的 codegen bug，和脚本内容无关），整个 main.js 静默不执行。改成字符串比对。
                if(headPart.length >= 12 && headPart.substring(0, 5) === "HTTP/"){
                    var sp = headPart.indexOf(" ");
                    if(sp > 0){
                        var num = parseInt(headPart.substring(sp + 1, sp + 4), 10);
                        if(!isNaN(num)){ code = num; }
                    }
                }
                res = {code: code, text: textPart, head: headPart};
            }catch(e){
                res = {code: -1, text: String(e), head: ""};
            }
            try{ if(sock !== null){ sock.close(); } }catch(e1){}
            return res;
        }
        
        function httpJson(method, path, body, cb){
            Threads.daemon(function(){
                var code = -1, text = "";
                try{
                    var r = httpRaw(method, sidecarUrl() + path, body);
                    code = r.code; text = r.text;
                }catch(e){
                    text = String(e);
                }
                var c = code, t = text;
                Core.app.post(function(){
                    try{ cb(c, t); }
                    catch(e2){ log("callback failed: " + e2); }
                });
            });
        }

        function parse(text){
            try{ return JSON.parse(text); }
            catch(e){ return null; }
        }

        function base64Bytes(b64){
            return Packages.java.util.Base64.getDecoder().decode(b64);
        }

        // ---------------------------------------------------------- 目标处理器
        // 鼠标指着哪个方块就是哪个处理器。比"遍历全图找处理器"更可预期，
        // 也不需要猜玩家想改哪一个。

        function targetBuild(){
            var mw = Core.input.mouseWorld();
            var tile = Vars.world.tileWorld(mw.x, mw.y);
            if(tile === null || tile.build === null){ return null; }
            var b = tile.build;
            // 鸭子类型而不是 instanceof 嵌套类：LogicBuild 是 LogicBlock 的静态内部类，
            // 名字里的 $ 在不同版本/混淆下不稳，而这两个字段是 LogicBuild 独有的。
            if(b.executor === undefined || b.code === undefined){ return null; }
            return b;
        }

        function describe(b){
            if(b === null){ return "光标下没有处理器"; }
            return "处理器 @ " + b.tile.x + "," + b.tile.y +
                   "  代码 " + String(b.code).split("\n").length + " 行" +
                   "  链接 " + b.links.size;
        }

        // ------------------------------------------------- 撤销快照（P5 第一刀：落盘）
        // 内存栈（undoStack）重启就没了，而「写错了想回滚」常常发生在第二天。
        // 每次写入前把「坐标 + 原代码 + 链接表 + 时间」写成
        // <dataDir>/mlogauxiliary/undo/<毫秒时间戳>_<x>_<y>.json —— 文件名可排序 = 时间顺序。
        // 回滚只还原代码（写入从不动链接表），走 build.updateCode()，联机下仍然只是本地生效。
        var UNDO_KEEP = 40;

        function undoRoot(){
            try{
                var fi = Vars.dataDirectory.child("mlogauxiliary").child("undo");
                if(!fi.exists()){ fi.mkdirs(); }
                return fi;
            }catch(e){ log("undo dir failed: " + e); return null; }
        }

        function linkPairs(b){
            var out = [], ls = b && b.links, i;
            try{
                for(i = 0; i < ls.size; i++){
                    var l = ls.get(i);
                    out.push({name: String(l.name), x: l.x, y: l.y,
                              block: (l.block && l.block.name) ? String(l.block.name) : ""});
                }
            }catch(e){ }
            return out;
        }

        function snapshotText(b, why, batch){
            return JSON.stringify({schema: 1, x: b.tile.x, y: b.tile.y, at: Date.now(),
                                   why: String(why || ""),
                                   batch: String(batch || ""),
                                   code: String(b.code), links: linkPairs(b)});
        }

        function undoFilesOf(root){
            var out = [], names = [], files, i;
            if(root === null){ return out; }
            try{
                files = root.list(".json");
                for(i = 0; i < files.length; i++){ names.push(String(files[i].name())); }
                names.sort();
                names.reverse();                       // newest first
                for(i = 0; i < names.length; i++){ out.push(root.child(names[i])); }
            }catch(e){ log("undo list failed: " + e); }
            return out;
        }

        function undoFiles(){ return undoFilesOf(undoRoot()); }

        function saveUndoTo(root, b, why, batch){
            if(root === null){ return null; }
            var name = Date.now() + "_" + b.tile.x + "_" + b.tile.y + ".json";
            try{ root.child(name).writeString(snapshotText(b, why, batch), false); }
            catch(e){ log("undo save failed: " + e); return null; }
            var files = undoFilesOf(root), i;
            for(i = UNDO_KEEP; i < files.length; i++){ try{ files[i].delete(); }catch(e2){ } }
            return name;
        }

        function saveUndo(b, why){ return saveUndoTo(undoRoot(), b, why); }

        // 从磁盘上最新的一份快照回滚。处理器已经不存在（拆了 / 换图了）就跳过，继续往前找。
        function undoFromDisk(){
            var files = undoFiles(), i;
            for(i = 0; i < files.length; i++){
                var f = files[i], name = String(f.name()), parts = name.split("_");
                if(parts.length < 3){ continue; }
                var x = parseInt(parts[1], 10), y = parseInt(parts[2], 10);
                if(isNaN(x) || isNaN(y)){ continue; }
                var snap = null;
                try{ snap = parse(f.readString("UTF-8")); }catch(e){ continue; }
                if(!snap || typeof snap.code !== "string"){ continue; }
                // v0.12.0：带 batch 的快照是事务写入留下的，整批一起还原。
                if(String(snap.batch || "").length > 0){ return undoBatchFromDisk(String(snap.batch), files); }
                var b = null;
                try{ b = Vars.world.build(x, y); }catch(e2){ b = null; }
                if(b === null || b.code === undefined){ continue; }
                try{
                    b.updateCode(snap.code);
                    try{ f.delete(); }catch(e3){ }
                    return "已从磁盘快照回滚 @ " + x + "," + y + "（"
                         + snap.code.split("\n").length + " 行，快照 " + name + "）";
                }catch(e4){ return "回滚失败：" + e4; }
            }
            return "磁盘上没有可回滚的快照";
        }

        // 整批还原：把同一个 batch 的快照全找出来，逐台 updateCode，全删掉。
        function undoBatchFromDisk(batch, files){
            var back = [], n = 0, i;
            for(i = 0; i < files.length; i++){
                var f = files[i], snap = null;
                try{ snap = parse(f.readString("UTF-8")); }catch(e){ continue; }
                if(!snap || String(snap.batch || "") !== batch){ continue; }
                n++;
                var b = null;
                try{ b = Vars.world.build(snap.x, snap.y); }catch(e2){ b = null; }
                if(b === null || b.code === undefined){ try{ f.delete(); }catch(e3){ } continue; }
                try{
                    b.updateCode(String(snap.code));
                    back.push("@" + snap.x + "," + snap.y);
                    try{ f.delete(); }catch(e4){ }
                }catch(e5){ }
            }
            if(n === 0){ return "磁盘上没有可回滚的快照"; }
            return "已按事务 " + batch + " 回滚 " + back.length + "/" + n + " 台"
                 + (back.length > 0 ? "：" + back.join(" ") : "（处理器都不在了）");
        }

        function undoInfo(){
            var root = undoRoot();
            return "撤销快照 " + undoFilesOf(root).length + " 份（目录 " + dirText(root) + "）";
        }

        // ------------------------------------------------ 工作区快照（v0.14.0，W1 数据层）
        // 用户 m03632：「相当于每个选框就是一个工作区快照。即使写完一次逻辑也不会让工作区消失，
        // 保留上下文，可以选择归档或释放工作区，释放工作区就不再留存上下文。」
        // 落盘：<dataDirectory>/mlogauxiliary/workspaces/<id>.json（和 undo 快照同级，跟着存档走）。
        // 存的是**上下文**不是世界快照：bounds + 地图 + 每台处理器的描述/链接表/最后写进去的代码/投递历史；
        // 世界快照仍在投递时现算（regionSnapshot），所以工作区文件不会随方块变动而膨胀。
        // 归属判定（用户 m03985 批准）：选框中心落在某个 active 工作区的 bounds 内、且地图一致 ⇒ 复用。
        // 读写走 wsReadText / wsWriteText / wsDeleteFile 三个原语：无头模拟器里 Vars.dataDirectory
        // 可能是 null（mlogsim 不调 Vars.loadSettings），探针把它们换成内存表，逻辑照样能验。
        var WS_SCHEMA = 1;
        var WS_KEEP = 200;      // 工作区文件上限，超出按「最近更新」从旧到新释放
        var WS_HISTORY = 20;    // 每台处理器保留几次投递历史
        var WS_JOBS = 50;       // 每个工作区保留多少个作业 id
        var wsCache = {};       // id -> 已解析的工作区对象
        var wsCurrent = null;   // 当前框选挂靠的工作区 id
        var wsMem = null;       // 探针用内存表（非 null 时不碰磁盘）
        var wsSeq = 0;

        function wsStamp(){
            try{ return Packages.java.lang.System.currentTimeMillis(); }catch(e){ return 0; }
        }

        function wsRoot(){
            try{
                var fi = Vars.dataDirectory.child("mlogauxiliary").child("workspaces");
                if(!fi.exists()){ fi.mkdirs(); }
                return fi;
            }catch(e){ log("workspace dir failed: " + e); return null; }
        }

        function wsFileOf(id){
            var root = wsRoot();
            if(root === null){ return null; }
            try{ return root.child(String(id) + ".json"); }catch(e){ return null; }
        }

        function wsReadText(id){
            var key = String(id);
            if(wsMem !== null){ return (wsMem[key] === undefined) ? null : String(wsMem[key]); }
            var f = wsFileOf(key);
            if(f === null){ return null; }
            try{ return f.exists() ? String(f.readString("UTF-8")) : null; }
            catch(e){ log("workspace read failed: " + e); return null; }
        }

        function wsWriteText(id, text){
            var key = String(id);
            if(wsMem !== null){ wsMem[key] = String(text); return true; }
            var f = wsFileOf(key);
            if(f === null){ return false; }
            try{ f.writeString(String(text), false); return true; }
            catch(e){ log("workspace write failed: " + e); return false; }
        }

        function wsDeleteFile(id){
            var key = String(id);
            if(wsMem !== null){ delete wsMem[key]; return true; }
            var f = wsFileOf(key);
            if(f === null){ return false; }
            try{ if(f.exists()){ f.delete(); } return true; }
            catch(e){ log("workspace delete failed: " + e); return false; }
        }

        function wsFileNames(){
            var out = [], key;
            if(wsMem !== null){
                for(key in wsMem){ if(wsMem.hasOwnProperty(key)){ out.push(String(key) + ".json"); } }
                return out;
            }
            var root = wsRoot(), files = null, i;
            if(root === null){ return out; }
            try{ files = root.list(".json"); }catch(e){ files = null; }
            if(files === null){ return out; }
            for(i = 0; i < files.length; i++){ out.push(String(files[i].name())); }
            return out;
        }

        function wsParseText(text){
            if(text === null || text === undefined){ return null; }
            var obj = null;
            try{ obj = parse(String(text)); }catch(e){ obj = null; }
            if(obj === null || obj === undefined || typeof obj !== "object"){ return null; }
            return obj;
        }

        function wsLoad(id){
            var key = String(id);
            if(wsCache[key] !== undefined && wsCache[key] !== null){ return wsCache[key]; }
            var obj = wsParseText(wsReadText(key));
            if(obj === null || obj.id === undefined){ return null; }
            if(obj.procs === undefined || obj.procs === null){ obj.procs = []; }
            if(obj.jobs === undefined || obj.jobs === null){ obj.jobs = []; }
            if(obj.state === undefined || obj.state === null){ obj.state = "active"; }
            wsCache[key] = obj;
            return obj;
        }

        function wsSave(ws){
            if(ws === null || ws === undefined || ws.id === undefined){ return null; }
            var key = String(ws.id);
            ws.schema = WS_SCHEMA;
            ws.mod = MOD_VERSION;
            ws.updated = wsStamp();
            if(!wsWriteText(key, JSON.stringify(ws))){ return null; }
            wsCache[key] = ws;
            wsPrune();
            return ws;
        }

        function wsList(){
            var names = wsFileNames(), out = [], i;
            for(i = 0; i < names.length; i++){
                var nm = String(names[i]);
                var id = nm.substring(0, nm.length - 5);
                var obj = wsLoad(id);
                if(obj === null){ continue; }
                out.push({id: String(obj.id), name: String(obj.name || obj.id),
                          state: String(obj.state || "active"), map: String(obj.map || "?"),
                          bounds: obj.bounds || null, updated: Number(obj.updated || 0),
                          procs: (obj.procs || []).length, jobs: (obj.jobs || []).length});
            }
            out.sort(function(a, b){ return b.updated - a.updated; });
            return out;
        }

        function wsPrune(){
            var names = wsFileNames();
            if(names.length <= WS_KEEP){ return; }
            var rows = wsList(), i;
            for(i = WS_KEEP; i < rows.length; i++){
                wsDeleteFile(rows[i].id);
                delete wsCache[String(rows[i].id)];
            }
            log("workspace prune: released " + String(rows.length - WS_KEEP) + " old workspace(s)");
        }

        function wsCenter(r){
            return {x: Math.floor((Number(r.x1) + Number(r.x2)) / 2),
                    y: Math.floor((Number(r.y1) + Number(r.y2)) / 2)};
        }

        function wsNameFor(r){
            var c = wsCenter(r);
            return "区域 (" + c.x + "," + c.y + ")";
        }

        function wsNew(r, name){
            wsSeq++;
            var id = "ws" + String(wsStamp()) + "_" + String(wsSeq);
            var map = "?";
            try{ map = String(mapName); }catch(e){ map = "?"; }
            var ws = {schema: WS_SCHEMA, mod: MOD_VERSION, id: id,
                      name: String(name || ("工作区 " + id)), state: "active", map: map,
                      world: null, bounds: {x1: Number(r.x1), y1: Number(r.y1),
                                            x2: Number(r.x2), y2: Number(r.y2)},
                      created: wsStamp(), updated: wsStamp(), notes: "",
                      procs: [], jobs: [], archivedAt: 0};
            try{ ws.world = {width: Vars.world.width(), height: Vars.world.height()}; }catch(e){ ws.world = null; }
            wsCurrent = id;
            return ws;
        }

        // 按 id 找 active 工作区：选框中心落在 bounds 内 + 地图一致（用户 m03985 批准的判定口径）。
        function wsFindActive(r){
            if(r === null || r === undefined){ return null; }
            var c = wsCenter(r), map = "?";
            try{ map = String(mapName); }catch(e){ map = "?"; }
            var rows = wsList(), i;
            for(i = 0; i < rows.length; i++){
                var row = rows[i];
                if(String(row.state) !== "active"){ continue; }
                if(String(row.map) !== map){ continue; }
                var b = row.bounds;
                if(b === null || b === undefined){ continue; }
                if(c.x >= Number(b.x1) && c.x <= Number(b.x2)
                   && c.y >= Number(b.y1) && c.y <= Number(b.y2)){
                    return String(row.id);
                }
            }
            return null;
        }

        function wsProcOf(ws, b){
            var k = procKey(b), list = ws.procs || [], i;
            for(i = 0; i < list.length; i++){
                if(String(list[i].k) === k){ return list[i]; }
            }
            return null;
        }

        function wsTouchProc(ws, b, desc){
            if(ws === null || ws === undefined || b === null || b === undefined){ return null; }
            var k = procKey(b), row = wsProcOf(ws, b);
            if(row === null){
                var bn = "?";
                try{ bn = String(b.block.name); }catch(e){ bn = "?"; }
                row = {k: k, x: b.tile.x, y: b.tile.y, block: bn, desc: "", links: null,
                       code: "", accepted: 0, history: []};
                ws.procs.push(row);
            }
            var d = String(desc || "");
            if(d.length > 0){ row.desc = d; }
            row.links = linkSig(b);
            try{ row.code = String(b.code); }catch(e){ }
            return row;
        }

        // F3 批量路径：复用中心落在框内的工作区，否则新建一个（这块框选从此有了身份）。
        function wsAttach(r, targets, desc){
            var id = wsFindActive(r);
            var ws = (id === null) ? wsNew(r, wsNameFor(r)) : wsLoad(id);
            if(ws === null){ return null; }
            if(id !== null){
                // 复用的时候把框扩成并集：同一个工作区覆盖的范围只增不减
                var b = ws.bounds || {};
                ws.bounds = {x1: Math.min(Number(b.x1), Number(r.x1)),
                             y1: Math.min(Number(b.y1), Number(r.y1)),
                             x2: Math.max(Number(b.x2), Number(r.x2)),
                             y2: Math.max(Number(b.y2), Number(r.y2))};
            }
            for(var i = 0; i < (targets || []).length; i++){ wsTouchProc(ws, targets[i].b, desc); }
            wsSave(ws);
            wsCurrent = String(ws.id);
            return ws;
        }

        // 单台路径（AI 面板）：只有这台处理器已经落在某个 active 工作区里才挂靠，不凭空造工作区。
        function wsTouchHere(b, desc){
            if(b === null || b === undefined){ return null; }
            var r = null;
            try{ r = {x1: b.tile.x, y1: b.tile.y, x2: b.tile.x, y2: b.tile.y}; }catch(e){ return null; }
            var id = wsFindActive(r);
            if(id === null){ return null; }
            var ws = wsLoad(id);
            if(ws === null){ return null; }
            wsTouchProc(ws, b, desc);
            wsSave(ws);
            wsCurrent = String(ws.id);
            return ws;
        }

        // 写入成功：把「这台处理器现在跑的就是这段代码」记进工作区（按 procKey 找行，找不到就跳过）。
        function wsAccepted(b, res){
            try{
                if(b === null || b === undefined || res === null || res === undefined){ return; }
                var k = procKey(b), rows = wsList(), i, j;
                for(i = 0; i < rows.length; i++){
                    var ws = wsLoad(rows[i].id);
                    if(ws === null){ continue; }
                    var list = ws.procs || [];
                    for(j = 0; j < list.length; j++){
                        if(String(list[j].k) !== k){ continue; }
                        var row = list[j];
                        row.code = String(b.code);
                        row.accepted = Number(row.accepted || 0) + 1;
                        row.links = linkSig(b);
                        var hist = row.history || [];
                        hist.push({t: wsStamp(), template: String(res.template || "code"),
                                   statements: Number(res.statements || 0),
                                   bytes: Number(res.bytes_len || 0)});
                        while(hist.length > WS_HISTORY){ hist.shift(); }
                        row.history = hist;
                        wsSave(ws);
                        return;
                    }
                }
            }catch(e){ log("workspace accepted failed: " + e); }
        }

        function wsRecordJob(ws, jid, desc){
            if(ws === null || ws === undefined || jid === null || jid === undefined){ return; }
            try{
                ws.jobs.push({id: String(jid), desc: String(desc || ""), at: wsStamp(), state: ""});
                while(ws.jobs.length > WS_JOBS){ ws.jobs.shift(); }
                wsSave(ws);
            }catch(e){ log("workspace job failed: " + e); }
        }

        function wsRecordTerminal(ws, job){
            if(ws === null || ws === undefined || job === null || job === undefined){ return; }
            try{
                var list = ws.jobs || [], i;
                for(i = 0; i < list.length; i++){
                    if(String(list[i].id) !== String(job.id)){ continue; }
                    list[i].state = String(job.state || "");
                    list[i].template = String(job.template || "");
                    list[i].statements = Number(job.statements || 0);
                    var fs = job.findings || [], keep = [], fi;
                    for(fi = 0; fi < fs.length && keep.length < 12; fi++){
                        keep.push({sev: String(fs[fi].sev || ""), rid: String(fs[fi].rid || ""),
                                   msg: String(fs[fi].msg || "").substring(0, 300)});
                    }
                    list[i].findings = keep;
                    wsSave(ws);
                    return;
                }
            }catch(e){ log("workspace terminal failed: " + e); }
        }

        function wsArchive(id){
            var ws = wsLoad(id);
            if(ws === null){ return "工作区不存在"; }
            ws.state = "archived";
            ws.archivedAt = wsStamp();
            if(wsSave(ws) === null){ return "归档失败：无法写入磁盘"; }
            return "已归档工作区「" + String(ws.name) + "」";
        }

        function wsRestore(id){
            var ws = wsLoad(id);
            if(ws === null){ return "工作区不存在"; }
            ws.state = "active";
            ws.archivedAt = 0;
            if(wsSave(ws) === null){ return "恢复失败：无法写入磁盘"; }
            return "已恢复工作区「" + String(ws.name) + "」";
        }

        // 释放 = 删文件 + 忘掉缓存：用户要的「不再留存上下文」就是这一条。
        function wsRelease(id){
            var key = String(id);
            var ws = wsLoad(key);
            if(ws === null){ return "工作区不存在"; }
            var nm = String(ws.name);
            if(!wsDeleteFile(key)){ return "释放失败：无法删除文件"; }
            delete wsCache[key];
            if(wsCurrent === key){ wsCurrent = null; }
            return "已释放工作区「" + nm + "」（上下文不再留存）";
        }

        function wsStateText(ws){
            if(ws === null || ws === undefined){ return "没有工作区"; }
            var st = String(ws.state || "active");
            var cn = (st === "active") ? "进行中" : ((st === "archived") ? "已归档" : st);
            return "「" + String(ws.name) + "」" + cn + " · "
                 + String((ws.procs || []).length) + " 台处理器 · "
                 + String((ws.jobs || []).length) + " 个作业";
        }

        // ------------------------------------------------ 写前冲突检测（v0.10.1 / v0.12.8）
        // 作业是异步的：投递那一刻处理器上是 A，面板上点「写入」时可能已经是 B
        // （等结果的时候顺手改了代码、或者另一台处理器写了同一格）。静默覆盖会吃掉这段改动，
        // 所以写入前比对「投递作业那一刻的代码」：不一致就拒绝一次，再点一次「写入」才算确认覆盖。
        // 不用对话框是有意的 —— 无头环境里 Core.scene 是 null，弹窗会直接 NPE，
        // 而「拒绝一次 + 再点一次」在真机和测试里走的是同一条路径。
        // v0.12.8：基线不再只是代码文本，链接表一起比。等结果的那几十秒里拆/加一条链接，
        // 生成代码里的链接名就全废了 —— 那时静默写入比覆盖掉代码更糟（规划 §5 的口径里
        // 链接表本来就该一起拦）。

        var JOB_ORIG  = {};       // "x,y" -> 投递作业那一刻的代码
        var JOB_LINKS = {};       // "x,y" -> 投递作业那一刻的链接表签名
        var JOB_FORCE = {};       // "x,y" -> 已经拒绝过一次，下一次写入视为确认覆盖

        function procKey(b){
            try{ return String(b.tile.x) + "," + String(b.tile.y); }
            catch(e){ return "?"; }
        }

        // 链接表签名：名字 + 绝对坐标。把一条链接从一台方块换到另一台（同名不同位置）
        // 对生成的代码是另一个事实，必须算改动。null = 不知道这个处理器的链接表
        // （无头假处理器没有 links 字段），这时不判链接冲突。
        function linkSig(b){
            var l = linkListOrNull(b);
            if(l === null){ return null; }
            var parts = [];
            for(var i = 0; i < l.length; i++){
                parts.push(String(l[i].name) + "@" + String(l[i].x) + "," + String(l[i].y));
            }
            parts.sort();
            return parts.join(" ");
        }

        function linkSigText(sig){
            if(sig === null || sig === undefined || String(sig).length === 0){ return "空"; }
            return String(sig).split(" ").join(", ");
        }

        function rememberOrig(b){
            try{
                var k = procKey(b);
                JOB_ORIG[k] = String(b.code);
                JOB_LINKS[k] = linkSig(b);
                JOB_FORCE[k] = false;
            }catch(e){ log("rememberOrig failed: " + e); }
        }

        function forgetOrig(b){
            try{
                var k = procKey(b);
                delete JOB_ORIG[k]; delete JOB_LINKS[k]; delete JOB_FORCE[k];
            }catch(e){ }
        }

        function conflictReason(b){
            var k = procKey(b);
            var parts = [];
            var orig = JOB_ORIG[k];
            if(orig !== undefined && orig !== null && String(b.code) !== String(orig)){
                parts.push("代码 " + String(orig).split("\n").length + " 行 → "
                           + String(b.code).split("\n").length + " 行");
            }
            var olinks = JOB_LINKS[k];
            if(olinks !== undefined && olinks !== null){
                var nowLinks = linkSig(b);
                if(nowLinks !== null && nowLinks !== olinks){
                    parts.push("链接表 [" + linkSigText(olinks) + "] → ["
                               + linkSigText(nowLinks) + "]");
                }
            }
            if(parts.length === 0){ return null; }
            return "处理器在投递后已被改动（提交时 " + parts.join("；") + "）";
        }

        function conflictGate(b){
            var why = conflictReason(b);
            if(why === null){ return null; }
            var k = procKey(b);
            if(JOB_FORCE[k] === true){
                JOB_FORCE[k] = false;          // 只放行一次
                return null;
            }
            JOB_FORCE[k] = true;
            return "拒绝写入：" + why + "；再点一次「写入」以覆盖";
        }

        // ------------------------------------------------ 联机写入（v0.12.8）
        // 联机里 b.configure(bytes) 只是发一个 Call.tileConfig 给服务端，真正的写入由服务端做。
        // 服务端两道闸（Mindustry-Source/core/src/mindustry/input/InputHandler.java:710-726）：
        //   ① Units.canInteract(player, build) = build.interactable(player.team()) || rules.editor
        //   ② netServer.admins.allowAction(player, ActionType.configure, ...)
        // 拒了就发一个「撤销配置」包把客户端改回去（源码注释原话 "undo the config on the client"），
        // 但**客户端收不到任何错误** —— 看起来写成功了，其实存档里没变。这就是「无权限」要专门报的原因。
        // 所以这里做两层：判得了的（队伍不对）当场拒绝并说「无权限」；
        // 判不了的（服务端权限等级/规则）在写完一两秒后回读代码，没生效就报「无权限」。

        function netClient(){
            try{ return Vars.net !== null && Vars.net !== undefined && Vars.net.client() === true; }
            catch(e){ return false; }
        }

        function localPlayer(){
            try{ if(Vars.player === null || Vars.player === undefined){ return null; } return Vars.player; }
            catch(e){ return null; }
        }

        function isAdmin(){
            var p = localPlayer();
            try{ return p !== null && p.admin === true; }catch(e){ return false; }
        }

        // v0.13.0：原版 ChatFragment 的可见性就是 net.active() && hudfrag.shown
        // （ChatFragment.java:50-60），单机会话里聊天记录每帧都被清掉 —— 播报要据此选通道。
        function netActive(){
            try{ return Vars.net !== null && Vars.net !== undefined && Vars.net.active() === true; }
            catch(e){ return false; }
        }

        // 服务端第一道闸的客户端复刻。返回 null = 放行（或判不了，让服务端说话）。
        function permWhy(b){
            if(!netClient()){ return null; }
            var p = localPlayer();
            if(p === null){ return null; }                 // 还没拿到玩家：不猜
            var ok = true;
            try{ ok = b.interactable(p.team()) === true || Vars.state.rules.editor === true; }
            catch(e){ return null; }                       // 判不了就当放行
            if(ok){ return null; }
            var mine = "", theirs = "";
            try{ mine = String(p.team()); }catch(e1){ }
            try{ theirs = String(b.team); }catch(e2){ }
            return "无权限：处理器属于 " + theirs + "，你在 " + mine
                 + "；联机只接受本方队伍（或编辑器规则）的写入，本次未写入任何一台";
        }

        // 写后回读：联机里唯一能确认「服务端到底写没写」的办法。客户端会先把 configure
        // 应用上去，被拒时服务端的撤销包把它改回来 —— 隔一两秒回读代码就分得清。
        // 无头引擎里 Vars.net 是 null，armWriteCheck 直接不排，writeCheckTick 空转。
        var WRITE_CHECK = [];
        var WRITE_CHECK_WAIT_MS = 1500;
        var WRITE_CHECK_TRIES = 3;

        function normCode(s){
            var t = String(s === null || s === undefined ? "" : s).replace(/\r/g, "");
            while(t.length > 0){
                var c = t.charAt(t.length - 1);
                if(c === " " || c === "\n" || c === "\t"){ t = t.substring(0, t.length - 1); }
                else{ break; }
            }
            return t;
        }

        // 期望落地的代码：sidecar 返回的 code 就是载荷解码出来的文本
        // （LogicBlock.readCompressed 用同一个 charset 解 byte[]，updateCode 原样存进 code）。
        // 没有 code 字段就不核验 —— 宁可不报，也不误报。
        function wantCode(res){
            if(res === null || res === undefined){ return null; }
            if(res.code !== undefined && res.code !== null && String(res.code).length > 0){
                return String(res.code);
            }
            return null;
        }

        function armWriteCheck(b, want, label){
            if(!netClient()){ return; }
            var w = normCode(want);
            if(w.length === 0){ return; }
            if(WRITE_CHECK.length >= 16){ WRITE_CHECK.shift(); }
            var now = Packages.java.lang.System.currentTimeMillis();
            WRITE_CHECK.push({b: b, want: w, at: now + WRITE_CHECK_WAIT_MS, tries: 0,
                              label: String(label || ("@" + b.tile.x + "," + b.tile.y))});
        }

        // 每帧一次：到点回读，代码对上了就销案，连续三次对不上才报「无权限」。
        function writeCheckTick(){
            if(WRITE_CHECK.length === 0){ return; }
            var now = Packages.java.lang.System.currentTimeMillis();
            var keep = [], i;
            for(i = 0; i < WRITE_CHECK.length; i++){
                var w = WRITE_CHECK[i];
                if(now < w.at){ keep.push(w); continue; }
                var gone = false;
                try{ gone = (w.b === null || w.b.tile === null || w.b.tile.build !== w.b); }
                catch(e){ gone = true; }
                if(gone){ continue; }                            // 方块没了：不归这里管
                var got = "";
                try{ got = String(w.b.code); }catch(e2){ got = ""; }
                if(normCode(got) === w.want){ continue; }         // 服务端收下了
                w.tries++;
                if(w.tries >= WRITE_CHECK_TRIES){
                    hint("无权限：" + w.label + " 的写入未生效（服务端未接受）。"
                         + "联机逻辑块由服务端执行，需房主或管理员授权。");
                    continue;
                }
                w.at = now + WRITE_CHECK_WAIT_MS;
                keep.push(w);
            }
            WRITE_CHECK = keep;
        }

        // ---------------------------------------------------------- 写入
        // 唯一的写入口。四道闸：0 ERR、目标存在、载荷由 sidecar 生成（模组不自己拼代码）、写前没被改过。

        function writeBuild(b, res){
            if(b === null){ return "没有目标处理器"; }
            if(!res || res.ok !== true){
                return "拒绝写入：静态检查有 ERR（" + ((res && res.errors) || "?") + " 条）";
            }
            if(!res.bytes_b64){ return "拒绝写入：sidecar 未返回载荷"; }
            var noPerm = permWhy(b);
            if(noPerm !== null){ return noPerm; }
            var blocked = conflictGate(b);
            if(blocked !== null){ return blocked; }
            try{
                var snapName = saveUndo(b, "write:" + String(res.template || "code"));
                undoStack.push({b: b, code: String(b.code), snap: snapName});
                if(undoStack.length > 20){ undoStack.shift(); }
                b.configure(base64Bytes(res.bytes_b64));
                forgetOrig(b);          // 写成功之后这次作业的基线就过期了
                jobBoxWritten(b);       // v0.12.7：写入成功 = 这块框选的活干完了，框才消失
                wsAccepted(b, res);     // v0.14.0：工作区记下「这台现在跑的就是这段」
                armWriteCheck(b, wantCode(res), null);   // v0.12.8：联机里回读一次，确认服务端真收下了
                return "已写入 " + (res.template || "") + "（" + res.statements + " 条语句，"
                     + res.bytes_len + " 字节）";
            }catch(e){
                return "写入失败：" + e;
            }
        }

        // ------------------------------------------------ 事务写入（v0.12.0，P5 第三刀上半）
        // 一句话发给一片处理器时，每台一个作业（链接表、门禁、修复循环各自独立），
        // 但**写入是一次事务**：全部预检通过才逐台写；任一台写失败就把已经写过的按快照回滚，
        // 不让存档停在一半新一半旧的状态。撤销也按批次整批还原（快照 JSON 带 batch 字段）。
        // 为什么不用「一个作业产出 N 段代码」：那要扩 sidecar 的作业契约；而每台的链接名
        // 本来就不一样，让队列按台各跑一遍反而更准（规划 §5 第三刀）。
        var UNDO_BATCH = "";

        function batchId(){
            return String(Date.now()) + "_b";
        }

        function writeMany(items, why){
            if(items === null || items === undefined || items.length === 0){ return "没有要写的处理器"; }
            var i, it, bad = [], conflicts = [];
            for(i = 0; i < items.length; i++){
                it = items[i];
                if(it === null || it === undefined || it.b === null || it.b === undefined || !it.b.tile){
                    bad.push("第 " + (i + 1) + " 台：目标不存在");
                    continue;
                }
                if(!it.res || it.res.ok !== true){
                    bad.push("@" + procKey(it.b) + "：静态检查有 ERR");
                    continue;
                }
                if(!it.res.bytes_b64){ bad.push("@" + procKey(it.b) + "：sidecar 未返回载荷"); continue; }
                var np = permWhy(it.b);
                if(np !== null){ bad.push("@" + procKey(it.b) + " " + np); }
            }
            if(bad.length > 0){ return "拒绝写入（未写入任何一台）：" + bad.join("；"); }
            // 冲突检查也是整批的：只要有一台被改过，整批先拒绝一次；再调一次 = 确认覆盖。
            for(i = 0; i < items.length; i++){
                var why2 = conflictReason(items[i].b);
                if(why2 === null){ continue; }
                var k = procKey(items[i].b);
                if(JOB_FORCE[k] === true){ JOB_FORCE[k] = false; }
                else{ JOB_FORCE[k] = true; conflicts.push("@" + k + " " + why2); }
            }
            if(conflicts.length > 0){
                return "拒绝写入（未写入任何一台）：" + conflicts.join("；") + "；再点一次以覆盖";
            }
            // 先把每一台的原代码落盘（同一个 batch），再逐台 configure。
            var batch = batchId(), snaps = [], written = [];
            for(i = 0; i < items.length; i++){
                var sn = saveUndoTo(undoRoot(), items[i].b, "batch:" + String(why || ""), batch);
                snaps.push({b: items[i].b, code: String(items[i].b.code), snap: sn});
            }
            for(i = 0; i < items.length; i++){
                it = items[i];
                try{
                    it.b.configure(base64Bytes(it.res.bytes_b64));
                    written.push(it.b);
                }catch(e){
                    // configure 只吃载荷；回滚走 updateCode（快照里存的就是原代码）。
                    var back = [], j;
                    for(j = 0; j < written.length; j++){
                        try{
                            written[j].updateCode(String(snaps[j].code));
                            back.push("@" + procKey(written[j]));
                        }catch(e2){ }
                    }
                    try{ it.b.updateCode(String(snaps[i].code)); }catch(e3){ }
                    return "写入失败（@" + procKey(it.b) + "：" + e + "）；已回滚 " + back.length + " 台"
                         + (back.length > 0 ? "：" + back.join(" ") : "");
                }
            }
            for(i = 0; i < items.length; i++){ forgetOrig(items[i].b); }
            for(i = 0; i < items.length; i++){ wsAccepted(items[i].b, items[i].res); }   // v0.14.0：工作区回写
            for(i = 0; i < items.length; i++){ armWriteCheck(items[i].b, wantCode(items[i].res), null); }
            undoStack.push({many: snaps, batch: batch});
            if(undoStack.length > 20){ undoStack.shift(); }
            UNDO_BATCH = batch;
            var names = [];
            for(i = 0; i < items.length; i++){ names.push("@" + procKey(items[i].b)); }
            return "已写入 " + items.length + " 台（事务 " + batch + "）：" + names.join(" ");
        }

        function undo(){
            if(undoStack.length){
                var snap = undoStack.pop();
                if(snap.many){
                    // v0.12.0：事务写入的撤销是整批的，一台都不能少。
                    var back = [], i;
                    for(i = 0; i < snap.many.length; i++){
                        try{
                            snap.many[i].b.updateCode(snap.many[i].code);
                            if(snap.many[i].snap){ try{ undoRoot().child(snap.many[i].snap).delete(); }catch(e0){ } }
                            back.push("@" + procKey(snap.many[i].b));
                        }catch(e1){ }
                    }
                    return "已回滚 " + back.length + " 台（事务 " + String(snap.batch || "") + "）：" + back.join(" ");
                }
                try{
                    snap.b.updateCode(snap.code);      // P1：仅本地回滚，联机下请重进地图
                    if(snap.snap){ try{ undoRoot().child(snap.snap).delete(); }catch(e){ } }
                    return "已回滚到上一版代码（" + String(snap.code).split("\n").length + " 行）";
                }catch(e){ return "回滚失败：" + e; }
            }
            return undoFromDisk();
        }

        // ---------------------------------------------------------- 展示
        // 结果用原版文本对话框显示：这是最稳的一层（面板表格在无头环境里验不了）。

        function findingsText(res, b){
            var lines = [];
            lines.push(quickSummary(quickRun(res.code, b)));
            lines.push("模板：" + (res.template || "-") + "    语句：" + res.statements
                     + "    字节：" + res.bytes_len);
            lines.push("检查：" + (res.ok ? "通过（无 ERR）" : "有 ERR，禁止写入")
                     + "    ERR " + res.errors + " / WARN " + res.warns);
            lines.push("");
            lines.push("--- 代码 ---");
            lines.push(res.code);
            if(res.findings && res.findings.length){
                lines.push("");
                lines.push("--- 问题 ---");
                for(var i = 0; i < res.findings.length; i++){
                    var f = res.findings[i];
                    lines.push("L" + f.line + " " + f.sev + " [" + (f.rid || "-") + "] " + f.msg);
                }
            }
            if(res.cost){
                lines.push("");
                lines.push("--- 成本 ---");
                lines.push(res.cost);
            }
            return lines.join("\n");
        }

        function showResult(res, b){
            var text = findingsText(res, b);
            if(!res.ok){
                Vars.ui.showText("Mlog辅助 · 已拒绝", text);
                return;
            }
            Vars.ui.showConfirm("Mlog辅助 · 确认写入",
                text + "\n\n写入目标：" + describe(b),
                function(){
                    var msg = writeBuild(b, res);
                    log(msg);
                    Vars.ui.showText("Mlog辅助", msg);
                });
        }

        // ------------------------------------------ 原版逻辑编辑器里的审查
        // LogicDialog 是 Vars.ui.logic 上的单例（UI.java:228 `logic = new LogicDialog()`），
        // 它的构造里 clearChildren() 之后只放了三样东西：add(canvas).grow() / row() /
        // add(buttons).growX()（LogicDialog.java:39-72）。所以最低侵入的接法是往它自己
        // 身上再补一行：不改原版源码、不碰 canvas，只在底部加一条「审查」状态栏，
        // 代码一变（canvas.save() 变了）就重算一次。
        // 无头环境里 Vars.ui 是 null（Vars.java:209/332），每次访问都套 try。

        var reviewHost = null, reviewBar = null, reviewLast = "", reviewFrame = 0, reviewRes = null;

        // LogicDialog.executor 是**包私有**字段（真 v160.7 反射实测：canvas 是 public，
        // executor / privileged / consumer / wasRows 全是包私有），而 Rhino 只按 public
        // 成员绑定 —— 写 L.executor 拿到的是 undefined，不是 null，于是 v0.4.6 及之前的
        // 审查栏永远认为「没有处理器」，把链接表当成空表喂给 quickcheck。
        // 唯一能读到它的路是反射：getDeclaredField + setAccessible（Mindustry 在
        // classpath 上，没有模块系统挡着，实测 setAccessible 可用）。
        var logicExecField = null, logicExecProbed = false;

        function logicExecutor(){
            try{
                var L = Vars.ui.logic;
                if(L === null || L === undefined){ return null; }
                if(!logicExecProbed){
                    logicExecProbed = true;
                    try{
                        var cls = Packages.java.lang.Class.forName("mindustry.logic.LogicDialog");
                        var f = cls.getDeclaredField("executor");
                        f.setAccessible(true);
                        logicExecField = f;
                    }catch(e){ log("LogicDialog.executor is not reachable: " + e); }
                }
                if(logicExecField === null){ return null; }
                var ex = logicExecField.get(L);
                return (ex === undefined) ? null : ex;
            }catch(e){ return null; }
        }

        function reviewTargets(){
            // LExecutor.build 是 public（v160.7 实测），所以拿到 executor 之后这一步直读即可。
            var ex = logicExecutor();
            if(ex === null){ return null; }
            try{
                var b = ex.build;
                return (b === undefined) ? null : b;
            }catch(e){ return null; }
        }

        function reviewCode(){
            try{ return String(Vars.ui.logic.canvas.save()); }catch(e){ return null; }
        }

        // 审查栏/详情里都要说清「链接表是数出来的还是不知道」——v0.4.6 的假 WARN 就是
        // 因为把「不知道」当成了「一个链接都没有」。
        function linkLine(res){
            var st = (res === null || res === undefined) ? null : res.stats;
            if(st === null || st === undefined){ return "链接 ?"; }
            if(st.linkKnown === true){ return "链接 " + st.linkCount; }
            return "链接 ?（读不到处理器）";
        }

        function reviewCount(res){
            var err = 0, warn = 0, info = 0, f = res.findings;
            for(var k = 0; k < f.length; k++){
                if(f[k].sev === "ERR"){ err++; } else if(f[k].sev === "WARN"){ warn++; } else { info++; }
            }
            return {err: err, warn: warn, info: info, total: f.length};
        }

        function refreshReview(force){
            if(reviewBar === null){ return; }
            var code = reviewCode();
            if(code === null){ return; }
            if(!force && code === reviewLast){ return; }
            reviewLast = code;
            var res = quickRun(code, reviewTargets());
            reviewRes = res;
            // 审查栏重画之后把行内箭头也同步一遍（箭头贴在原版画布上，见下面的「行内箭头」段）
            syncArrows(res, null, null);
            var c = reviewCount(res);
            reviewBar.clear();
            reviewBar.left();
            reviewBar.add("审查").padRight(8);
            if(c.total === 0){
                reviewBar.add("没有发现问题").color(Pal.accent).padRight(16);
            }else{
                if(c.err > 0){ reviewBar.add("ERR " + c.err).color(Pal.remove).padRight(8); }
                if(c.warn > 0){ reviewBar.add("WARN " + c.warn).color(Pal.lightOrange).padRight(8); }
                if(c.info > 0){ reviewBar.add("INFO " + c.info).padRight(8); }
            }
            reviewBar.add("语句 " + res.stats.statements + " · 线性 " + res.stats.linearCost
                          + " · ipt " + res.stats.ticksPerPass
                          + " · " + linkLine(res)).padRight(16);
            reviewBar.button("详情", function(){ showReview(); }).width(90);
            reviewBar.button("AI 辅助", function(){ showAiPanel(); }).width(120);
        }

        function reviewText(){
            // 控制台/诊断用的纯文本版审查（showReview 会弹对话框）
            var b = reviewTargets();
            var code = reviewCode();
            if(code === null && b !== null){ code = String(b.code); }
            var res = quickRun(code === null ? "" : code, b);
            if(res.missing){ return "本地快检不可用（quickcheck.js 未加载）"; }
            var out = [quickSummary(res)];
            var ls = quickLines(res);
            for(var i = 0; i < ls.length; i++){ out.push(ls[i]); }
            return out.join("\n");
        }

        function showReview(){
            var code = reviewCode();
            var res = reviewRes;
            if(code !== null){ res = quickRun(code, reviewTargets()); }
            if(res === null){ if(Vars.ui !== null){ Vars.ui.showText("Mlog辅助 · 审查", "没有可审查的处理器。"); } return; }
            var out = [quickSummary(res)];
            var ls = quickLines(res);
            for(var i = 0; i < ls.length; i++){ out.push(ls[i]); }
            if(Vars.ui !== null){ Vars.ui.showText("Mlog辅助 · 审查", out.join("\n")); }
        }

        // ------------------------------------------ 行内箭头（v0.4.7）
        // 用户要的交互：哪一行有问题就在那一行贴个箭头，点箭头看这一行的原因。
        //
        // 为什么不做「往原版行里插一个 Cell」：Arc 的 Table.cells 是私有字段
        // （arc/scene/ui/layout/Table.java:35），插不到 addressLabel 前面；追加到行尾
        // 又会被行内按钮挤位置。所以改成覆盖层：一个铺在 statements 父容器里的 Table，
        // 箭头是它的普通孩子（不是 cell），手动摆位、每帧跟着布局重算。
        //
        // 三个依据（真 v160.7 实测 / 源码）：
        //  * LCanvas.statements 是 public 字段（LCanvas.java:32），子元素按语句顺序排，
        //    每个 StatementElem 的第一行（表头）高 38（LCanvas.java:427-491）。
        //  * quickcheck 的 finding.line 是 1 起的语句序号（quickcheck.js:97），
        //    所以第 i 个孩子对应 line = i + 1。
        //  * 覆盖层用 Touchable.childrenOnly：空白处不拦鼠标（原版画布要能拖行、点按钮），
        //    只有箭头自己被命中（Group.hit 先测孩子，Element.hit 对非 enabled 返回 null）。
        var ARROW = 16;                       // 箭头边长（像素）
        var arrowHost = null, arrowParent = null, arrowRows = [], arrowByLine = {};
        var arrowDraw;                        // undefined = 还没找过，null = 这个环境没有图标

        function sevRank(sev){
            if(sev === "ERR"){ return 0; }
            if(sev === "WARN"){ return 1; }
            return 2;
        }

        function sevColor(sev){
            if(sev === "ERR"){ return Pal.remove; }
            if(sev === "WARN"){ return Pal.lightOrange; }
            return Pal.accent;
        }

        // 箭头图标：原版行内按钮用的是 Icon（v160.7 里 Icon 是「带静态 Drawable 字段的类」，
        // 不是 v159.3 那个 enum）。无头引擎没有图集，Icon.right 是 null —— 那时返回 null，
        // 审查栏照常工作，只是不画箭头。
        function arrowDrawable(){
            if(arrowDraw !== undefined){ return arrowDraw; }
            arrowDraw = null;
            try{
                var d = Packages.mindustry.gen.Icon.right;
                if(d !== null && d !== undefined){ arrowDraw = d; }
            }catch(e){ /* ignore */ }
            if(arrowDraw === null){ log("no Icon.right in this build -- review arrows stay off"); }
            return arrowDraw;
        }

        function reviewStatements(){
            try{
                var L = Vars.ui.logic;
                if(L === null || L === undefined){ return null; }
                var s = L.canvas.statements;      // canvas 是 public（v160.7 反射实测）
                return (s === undefined) ? null : s;
            }catch(e){ return null; }
        }

        function arrowHostFor(statements){
            var parent = null;
            try{ parent = statements.parent; }catch(e){ return null; }
            if(parent === null || parent === undefined){ return null; }
            if(arrowHost !== null && arrowParent === parent){ return arrowHost; }
            var host = new Packages.arc.scene.ui.layout.Table();
            host.setFillParent(true);
            host.touchable = Packages.arc.scene.event.Touchable.childrenOnly;
            try{ parent.addChild(host); }catch(e){ return null; }
            host.update(function(){ layoutArrows(); });
            arrowHost = host;
            arrowParent = parent;
            return host;
        }

        // 第 i 个孩子 = 第 i+1 条语句；纵向取表头行的中心（表头在行顶，高 38）。
        // statements 只给测试传（无头引擎没有真画布），真机由每帧 update 回调传空。
        function layoutArrows(statements){
            if(arrowHost === null || arrowRows.length === 0){ return; }
            if(statements === null || statements === undefined){ statements = reviewStatements(); }
            if(statements === null){ return; }
            for(var i = 0; i < arrowRows.length; i++){
                var r = arrowRows[i], e = r.elem, img = r.img;
                try{
                    if(e === null || e === undefined){ img.visible = false; continue; }
                    img.visible = true;
                    var x = statements.x + e.x - ARROW - 2;     // 优先放进行左侧的空白
                    if(x < 2){ x = statements.x + e.x + 2; }     // 没空白就压进行内左端
                    var y = statements.y + e.y + e.getHeight() - 19 - (ARROW / 2);
                    img.setPosition(x, y);
                }catch(err){ img.visible = false; }
            }
        }

        function showLineReview(line){
            var out = ["第 " + line + " 行"];
            var by = arrowByLine[line];
            if(by === undefined){
                out.push("（这一行现在没有问题了）");
            }else{
                for(var i = 0; i < by.items.length; i++){
                    var f = by.items[i];
                    out.push(f.sev + " [" + (f.rid || "-") + "] " + f.msg);
                }
            }
            print("[mai] " + out.join("\n"));
            if(Vars.ui !== null){ Vars.ui.showText("Mlog辅助 · 第 " + line + " 行", out.join("\n")); }
        }

        // Rhino 里 var 是函数作用域，循环里直接闭包会全部指向最后一个值 —— 用工厂函数。
        function tapLine(line){
            return function(){ showLineReview(line); };
        }

        // 测试用：把箭头的颜色翻译回严重度（比较 Color 对象，不依赖 toString 格式）
        function sevNameOf(img){
            try{
                if(img.color.equals(Pal.remove)){ return "ERR"; }
                if(img.color.equals(Pal.lightOrange)){ return "WARN"; }
                if(img.color.equals(Pal.accent)){ return "INFO"; }
            }catch(e){ /* ignore */ }
            return "?";
        }

        function clearArrows(){
            if(arrowHost !== null){ try{ arrowHost.clearChildren(); }catch(e){ /* ignore */ } }
            arrowRows = [];
        }

        // res.findings -> 每行一个箭头。statements / drawable 只给测试用，真机传 null。
        function syncArrows(res, statements, drawableOverride){
            if(statements === null || statements === undefined){ statements = reviewStatements(); }
            if(statements === null){
                clearArrows();
                arrowByLine = {};
                return 0;
            }
            var host = arrowHostFor(statements);
            if(host === null){ return 0; }
            var byLine = {}, lines = [], i;
            var f = (res === null || res === undefined || res.findings === undefined) ? [] : res.findings;
            for(i = 0; i < f.length; i++){
                var ln = f[i].line;
                if(byLine[ln] === undefined){ byLine[ln] = {sev: f[i].sev, items: []}; lines.push(ln); }
                byLine[ln].items.push(f[i]);
                if(sevRank(f[i].sev) < sevRank(byLine[ln].sev)){ byLine[ln].sev = f[i].sev; }
            }
            arrowByLine = byLine;
            clearArrows();
            var draw = (drawableOverride === null || drawableOverride === undefined) ? arrowDrawable() : drawableOverride;
            if(draw === null || draw === undefined || lines.length === 0){ return 0; }
            var kids = null;
            try{ kids = statements.getChildren(); }catch(e){ return 0; }
            for(i = 0; i < lines.length; i++){
                var line = lines[i], idx = line - 1;
                if(idx < 0 || idx >= kids.size){ continue; }
                var elem = kids.get(idx), img = null;
                try{ img = new Packages.arc.scene.ui.Image(draw); }catch(e){ continue; }
                try{ img.setSize(ARROW, ARROW); }catch(e){ /* ignore */ }
                try{ img.setColor(sevColor(byLine[line].sev)); }catch(e){ /* ignore */ }
                try{ img.touchable = Packages.arc.scene.event.Touchable.enabled; }catch(e){ /* ignore */ }
                try{ img.tapped(tapLine(line)); }catch(e){ /* ignore */ }
                try{ host.addChild(img); }catch(e){ continue; }
                arrowRows.push({elem: elem, img: img, line: line, items: byLine[line].items.length});
            }
            layoutArrows(statements);
            return arrowRows.length;
        }
        function injectReview(){
            try{
                var L = Vars.ui.logic;
                if(L === null || L === undefined || reviewHost === L){ return false; }
                var bar = new Packages.arc.scene.ui.layout.Table();
                bar.left();
                L.row();
                L.add(bar).growX().height(46).pad(6);
                reviewHost = L;
                reviewBar = bar;
                log("review bar injected into the vanilla logic editor");
                refreshReview(true);
                return true;
            }catch(e){ log("review inject failed: " + e); return false; }
        }

        // 每帧被调一次：面板没开时只做两次属性读，代价可忽略。
        function reviewTick(){
            try{
                if(Vars.ui === null || Vars.ui.logic === null || !Vars.ui.logic.isShown()){ return; }
                if(reviewHost !== Vars.ui.logic){ injectReview(); }
                reviewFrame++;
                if(reviewFrame % 30 === 0){ refreshReview(false); }
            }catch(e){ /* 每帧回调里绝不抛出去 */ }
        }

        // ------------------------------------------ AI 辅助编程（F3 单格选中）
        // 一句话 → sidecar POST /plan → 代码 + 预检 → 确认写入。
        // 现在是规则式意图路由（tools/logicd.py 的 INTENTS 表），模型的接入点只在
        // /plan 的实现里，模组这一侧不用改。

        var aiPanel = null;

        function aiShown(){
            try{ return aiPanel !== null && aiPanel.isShown(); }catch(e){ return false; }
        }

        // Link coordinates travel as absolute tile positions (LogicLink.x/y), but the
        // byte[] payload stores them relative to the processor
        // (LogicBlock.config -> readCompressed(data, true), LogicBlock.java:304-307).
        // Omitting the origin silently wrote a link table pointing at empty tiles.
        function originOf(b){
            try{
                if(b !== null && b !== undefined && b.tile){
                    return {x: b.tile.x, y: b.tile.y};
                }
            }catch(e){ }
            return null;
        }

        // target 可选：区域面板里点「生成」时直接指定那个处理器，不必再把鼠标移过去。
        function showAiPanel(target){
            try{ llmSync(); }catch(e){ log("llm sync skipped"); }
            if(aiShown()){ try{ aiPanel.toFront(); return; }catch(e){ /* ignore */ } }
            var b = (target === undefined || target === null) ? targetBuild() : target;
            var d = new Packages.arc.scene.ui.Dialog("Mlog辅助 · AI 辅助编程 v" + MOD_VERSION);
            d.cont.defaults().pad(4).left();
            d.cont.add("目标：" + describe(b)).row();
            d.cont.add("描述这段逻辑要做什么，然后点「生成」：").padTop(6).row();
            var intent = "";
            d.cont.field("", function(v){ intent = String(v); }).width(560).row();
            d.cont.add("例：巡逻，遇到敌人就开火 / 容器里铜够 250 就打印 / 每 2 秒打印一次计数").row();
            // 同 openPanel：pane() 给的是 ScrollPane，必须自己造 Table 交给它托管。
            var host = new Packages.arc.scene.ui.layout.Table();
            host.defaults().pad(2).left();
            host.add("（还没生成）").row();
            try{
                d.cont.pane(host).height(300).width(600);
            }catch(e){
                log("pane() overload unavailable, falling back to a plain table: " + e);
                d.cont.add(host).growX();
            }
            d.buttons.defaults().pad(4);
            // v0.8.0：默认走作业队列（落盘 + 更严的门禁 + 可取消）。
            d.buttons.button("生成", function(){ startJob(b, host, d, intent); }).width(140);
            d.buttons.button("取消作业", function(){ hint(cancelJob()); }).width(120);
            d.buttons.button("关闭", function(){ d.hide(); }).width(140);
            d.closeOnBack();
            d.show();
            aiPanel = d;
        }

        // ---------------------------------------------------------- 作业队列（v0.8.0 / P4 第二刀）
        // AI 面板的「生成」不再直接 /plan，而是往 sidecar 的作业队列里投一个 job：
        // 作业会落盘、sidecar 崩了能续跑、过比分析器更严的门禁（link-name-not-bound
        // 从 WARN 提升成 ERR，见 tools/jobqueue.py 的 FATAL_RIDS）。
        // 游戏这边只做两件事：投递 + 每秒查一次状态；生成逻辑全在 sidecar。
        // 契约：docs/Mlog辅助-规划.md §4.1；端点：POST /jobs、GET /jobs?id=、DELETE /jobs?id=。
        var aiJob = null;                 // {id, b, desc, host, d, lastPoll, job}
        var JOB_POLL_MS = 1000;
        var JOB_TERMINAL = {done: 1, rejected: 1, failed: 1, cancelled: 1};
        // v0.12.5: F3 松手之后框不立刻消失——它代表「这一批作业还在跑」，一直到这一批
        // 全部终态再多留 8 秒，玩家才有机会点右下角的徽标看思考过程。
        var jobBox = null;                // {r, ids, jobs, pend, started, termAt, lastPoll}
        var jobBadge = null;              // 徽标在世界坐标里的点击区 {x, y, w, h}
        // v0.12.7（用户 2026-10-31）：「选框不应该自行消失，只能在应用后消失，或者主动关闭。」
        // 所以这里没有「留几秒」的常量：框一直画着，直到写入成功（jobBoxWritten）或玩家主动关（closeJobBox）。
        // 管理窗的列表是异步拉回来的：点进详情之后，迟到的列表回调不能往详情页里塞行。
        var managerSeq = 0;

        // v0.15.1（D2）：hint 只闪一下就没了，错误也跟着没。最后一条留在这里，
        // 「设置 → 最近提示」能看、能复制。hint() 里写，纯函数好自检。
        var LAST_HINT = {text: "", at: 0};
        var lastHintLbl = null;
        // v0.18.1（用户 m10327）：「最近提示改成可复制的文本日志」。
        // 保留 LAST_HINT（最后一条，探针与旧逻辑还在读），另外攒一份最近 HINT_LOG_MAX 条。
        var HINT_LOG = [], HINT_LOG_MAX = 12, lastHintLog = null;

        function hintLogPush(at, text){
            HINT_LOG.push({at: at, text: String(text)});
            if(HINT_LOG.length > HINT_LOG_MAX){ HINT_LOG.splice(0, HINT_LOG.length - HINT_LOG_MAX); }
        }

        function hintClock(at){
            try{
                var fmt = new Packages.java.text.SimpleDateFormat("HH:mm:ss");
                return String(fmt.format(new Packages.java.util.Date(Number(at))));
            }catch(e){ return "--:--:--"; }
        }

        function hintLogText(){
            if(HINT_LOG.length === 0){ return "（还没有提示）"; }
            var out = [], i;
            for(i = 0; i < HINT_LOG.length; i++){
                out.push(hintClock(HINT_LOG[i].at) + "  " + HINT_LOG[i].text);
            }
            return out.join("\n");
        }

        function hintLogCopy(){
            if(HINT_LOG.length === 0){ hint("还没有提示可复制"); return "no hint"; }
            var t = hintLogText();
            try{ Core.app.setClipboardText(t); }catch(e){ return "clipboard failed: " + e; }
            hint("已复制最近 " + HINT_LOG.length + " 条提示");
            return "copied:" + t.length;
        }

        function hintLogClear(){
            HINT_LOG.splice(0, HINT_LOG.length);
            LAST_HINT.text = "";
            LAST_HINT.at = 0;
            lastHintLblRefresh();
            return "hint log cleared";
        }

        // v0.15.1（D3）：作业管理窗的过滤状态（只在模组侧过滤，sidecar 只认 state= 精确值）。
        var managerFilter = {text: "", onlyBad: false, limit: 100};
        var managerFilterBtn = null;

        function isJobTerminal(state){
            return JOB_TERMINAL[String(state)] === 1;
        }

        // job 的 snapshot 只是给 sidecar 存档/将来自由档用的世界事实；
        // 处理器周围 13×13 足够，太大了会把整张图的格子塞进请求里。
        function jobBody(b, desc, wsInfo, batch){
            var snap = null;
            try{
                if(b !== null && b !== undefined && b.tile){
                    snap = regionSnapshot(b.tile.x - 6, b.tile.y - 6, b.tile.x + 6, b.tile.y + 6);
                }
            }catch(e){ snap = null; }
            if(snap === null){
                snap = {schema: 1, cells: 0, truncated: false, grid: [], processors: []};
            }
            var job = {mode: "new", desc: String(desc), links: linkListOrNull(b), ipt: 2,
                       origin: originOf(b), snapshot: snap};
            // v0.15.1（A4）：同一句话发给一批处理器时，告诉模型它是第几台 ——
            // 每台的链接表各不相同，照抄隔壁那台的程序会被预检拒掉。
            if(batch !== null && batch !== undefined){ job.batch = batch; }
            if(wsInfo !== null && wsInfo !== undefined){
                job.mode = "improve";                  // v0.14.1：同一块区域的上一版当依据
                job.workspace = wsInfo;
            }
            return {job: job};
        }

        function jobSubmit(b, desc, cb, wsInfo, batch){
            ensureSidecar(true);             // v0.13.1：装包版里后端是模组自己拉起来的
            // v0.17.0（用户 m09440 / 6）：后端还在起，就先把这次投递排上，
            // 就绪那一刻自动发出去 —— 不再让人等一会儿再点一遍。
            if(SIDE.state === "starting"){
                PENDING_SUBMITS.push({b: b, desc: desc, cb: cb, wsInfo: wsInfo, batch: batch,
                                      at: Packages.java.lang.System.currentTimeMillis()});
                if(!pendingHinted){ pendingHinted = true; hint("后端启动中，就绪后自动投递"); }
                return;
            }
            // v0.14.4：模板档已删，没有模型就不生成任何代码 —— 先说清楚要配什么，
            // 别让玩家等一个注定失败的作业。llmState 只反映 sidecar 内存，落盘配置另算。
            if(llmState.kind !== "llm" && llmState.kind !== "fake"){
                if(llmUrl().length === 0 || llmModel().length === 0){
                    cb(null, "未配置模型：先在「设置 → Mlog辅助 → 大模型」填地址、密钥、模型，"
                           + "再点「启用模型」");
                    return;
                }
                llmAutoSync();               // 配置在、sidecar 忘了（刚重启）：先推一遍再投
            }
            rememberOrig(b);                 // v0.10.1：投递那一刻的代码就是冲突基线
            httpJson("POST", "/jobs", JSON.stringify(jobBody(b, desc, wsInfo, batch)), function(code, text){
                var res = parse(text);
                if(code !== 200 || res === null || res.ok !== true){
                    cb(null, jobWhy(code, text, res));
                    return;
                }
                cb(res, null);
            });
        }

        function jobGet(id, cb){
            httpJson("GET", "/jobs?id=" + String(id), null, function(code, text){
                var res = parse(text);
                if(code !== 200 || res === null || res.ok !== true || !res.job){
                    cb(null, jobWhy(code, text, res));
                    return;
                }
                cb(res.job, null);
            });
        }

        function jobCancel(id, cb){
            httpJson("DELETE", "/jobs?id=" + String(id), null, function(code, text){
                var res = parse(text);
                if(code !== 200 || res === null || res.ok !== true){
                    cb(null, jobWhy(code, text, res));
                    return;
                }
                cb(res.job || null, null);
            });
        }

        function jobSevCounts(job){
            var f = (job && job.findings) ? job.findings : [];
            var c = {err: 0, warn: 0, info: 0, total: f.length};
            for(var i = 0; i < f.length; i++){
                var s = String(f[i].sev);
                if(s === "ERR"){ c.err++; } else if(s === "WARN"){ c.warn++; } else { c.info++; }
            }
            return c;
        }

        // job -> /plan 那套渲染要的形状。writeBuild 只认 ok/bytes_b64/statements/bytes_len，
        // renderPlan 还要 findings/template/reason，所以这里一次补齐。
        function jobAsPlan(job){
            var c = jobSevCounts(job);
            return {ok: String(job.state) === "done",
                    template: job.template || "",
                    reason: job.explanation || "",
                    findings: job.findings || [],
                    code: job.code || "",
                    statements: job.statements || 0,
                    bytes_b64: job.bytes_b64 || "",
                    bytes_len: job.bytes_len || 0,
                    errors: c.err, warns: c.warn};
        }

        function jobProgressText(job){
            if(job === null || job === undefined){ return "作业：查不到状态"; }
            var st = String(job.state), a = job.attempts || 0, id = String(job.id || "?");
            var c = jobSevCounts(job);
            if(st === "queued"){ return "作业 " + id + "：排队中（第 " + a + " 次尝试）"; }
            if(st === "running"){ return "作业 " + id + "：生成中（第 " + a + " 次尝试）"; }
            if(st === "done"){
                return "作业 " + id + "：完成（" + (job.statements || 0) + " 条语句，"
                     + (job.bytes_len || 0) + " 字节，尝试 " + a + " 次）";
            }
            if(st === "rejected"){
                return "作业 " + id + "：被门禁拒绝（尝试 " + a + " 次，ERR " + c.err + " 条）";
            }
            if(st === "cancelled"){ return "作业 " + id + "：已取消"; }
            return "作业 " + id + "：" + st
                 + (job.explanation ? "（" + String(job.explanation) + "）" : "");
        }

        // v0.12.6：作业还在跑的时候，findings 里躺着的是「上一次尝试」的问题，不是结论。
        // 真机上截断那一下（模型把预算花在思考上）就被人当成「生成失败」——所以非终态的
        // findings 必须挂一个「正在重试」的抬头，再补一句人话说明截断。
        // v0.15.0（A2+A3）：作业现在自己带着诊断（provider/model/budget/usage/attempt_log），
        // 失败也带类别（kind）。面板以前只照抄一行「ERR L0 empty reply」——玩家看不出
        // 下一步该动什么，也看不出这次调用到底花了多少。这里按类别说人话。
        var JOB_WHY = {
            "empty": "端点回了空内容：多半是代理吞了回复，或者模型的预算全花在思考上",
            "transport": "连不上端点：地址、网络、代理任一处断了",
            "http-status": "端点拒绝了这次调用：密钥、额度或模型名要核对",
            "bad-json": "端点回的不是 JSON：地址多半填错了一层（/v1 与 /chat/completions）",
            "truncated": "模型把预算花在思考上就没了：sidecar 会自动加大预算重试",
            "unparsable": "模型回的不是可用的 mlog：看它的说明改一句再试",
            "config": "还没配模型：设置 → Mlog辅助 → 大模型"
        };

        function jobKindOf(f){
            var k = "";
            try{ k = String((f === null || f === undefined) ? "" : (f.kind || "")); }
            catch(e){ k = ""; }
            return k;
        }

        function jobWhyLines(job, maxLines){
            var fs = job.findings || [], out = [], seen = {}, cap = maxLines || 3;
            for(var i = 0; i < fs.length && out.length < cap; i++){
                var k = jobKindOf(fs[i]);
                if(k === "" || seen[k] === true){ continue; }
                var w = JOB_WHY[k];
                if(w === undefined){ continue; }
                seen[k] = true;
                out.push("为什么：" + w);
            }
            return out;
        }

        function jobLastAttempt(job){
            var a = job.attempt_log;
            if(a === null || a === undefined || a.length === undefined || a.length === 0){ return null; }
            return a[a.length - 1];
        }

        function jobAttemptLine(job){
            var a = jobLastAttempt(job);
            var d = (a === null) ? (job.diag || null) : a;
            if(d === null || d === undefined){ return ""; }
            var bits = [];
            if(d.provider){ bits.push(String(d.provider)); }
            if(d.model){ bits.push(String(d.model)); }
            if(d.budget){ bits.push("预算 " + d.budget); }
            if(d.ms){ bits.push((d.ms / 1000).toFixed(1) + " s"); }
            if((d.prompt_tokens || 0) > 0 || (d.completion_tokens || 0) > 0){
                bits.push("token " + (d.prompt_tokens || 0) + "→" + (d.completion_tokens || 0));
            }
            if(d.finish_reason){ bits.push("finish_reason=" + d.finish_reason); }
            if(d.http_status){ bits.push("HTTP " + d.http_status); }
            return bits.join(" · ");
        }

        function jobEvidenceLines(job){
            var out = [], head = [];
            if(job.provider){ head.push(String(job.provider)); }
            if(job.model){ head.push(String(job.model)); }
            if(head.length > 0){ out.push("模型：" + head.join(" · ")); }
            var a = jobLastAttempt(job);
            var n = (a === null) ? (job.attempts || 1) : (a.n || job.attempts || 1);
            var line = jobAttemptLine(job);
            if(line !== ""){ out.push("第 " + n + " 次调用：" + line); }
            return out;
        }

        function jobFindingLines(job, maxLines){
            var fs = job.findings || [], out = [], i, m, n = 0;
            for(i = 0; i < fs.length && n < maxLines; i++){
                m = String(fs[i].msg);
                out.push(String(fs[i].sev) + " L" + fs[i].line + " " + m);
                n++;
                if(m.indexOf("finish_reason=length") >= 0 && n < maxLines){
                    out.push("  （模型把预算用在思考上，sidecar 会自动加大预算重试）");
                    n++;
                }
            }
            return out;
        }

        function jobRetryHead(job){
            if(isJobTerminal(String(job.state))){ return ""; }
            var fs = job.findings || [];
            if(fs.length === 0){ return ""; }
            return "--- 上一次尝试的问题（第 " + (job.attempts || 0) + " 次，还在重试）---";
        }

        // v0.12.4：模型自己的思考过程现在跟着作业一起回来（sidecar 的 job.reasoning），
        // 面板直接摊开——推理模型想歪、或者把预算全花在想上时，光看 ERR 一行看不出为什么。
        // 端点不返回 reasoning_content 的话这里是空串，面板就什么都不加。
        function jobReasoningText(job){
            if(job === null || job === undefined){ return ""; }
            var t = job.reasoning;
            return (t === null || t === undefined) ? "" : String(t);
        }

        var REASON_LINE = 40, REASON_LINES = 16;

        function jobReasoningLines(job, maxLines, perLine){
            var t = jobReasoningText(job);
            if(t.length === 0){ return []; }
            var w = perLine || REASON_LINE, cap = (maxLines || REASON_LINES) * w;
            var body = (t.length > cap) ? t.substring(0, cap) : t;
            var out = [];
            for(var i = 0; i < body.length; i += w){ out.push(body.substring(i, i + w)); }
            return out;
        }

        // v0.18.4（用户 m13036）：「为什么不能查看当时的思考内容，而只能看到工作状态」。
        // 思考其实一直都在作业里（sidecar 的 job.reasoning，非流式 ⇒ 作业收尾那一刻才落盘），
        // 但以前只有两个隐蔽详情窗显示开头 16 行 × 40 字，终态点「查看」只看得到代码/一行状态。
        // 这里补三个口子：全量文本、复制全量、可滚动的思考原文窗。
        function jobThinkAll(job){
            return jobReasoningText(job);
        }

        function jobThinkCopy(job){
            var t = jobThinkAll(job);
            if(t.length === 0){
                hint("这个作业没有思考内容（端点没返回 reasoning_content）");
                return "no reasoning";
            }
            try{ Core.app.setClipboardText(t); }
            catch(e){ hint("复制失败：" + e); return "clipboard failed: " + e; }
            hint("已复制思考（" + t.length + " 字符）");
            return "copied " + t.length;
        }

        // 整段摊开（最多 100000 行 × 60 字符，reasoning 本体上限 20000 字符）。
        function openThinkDialog(job){
            var t = jobThinkAll(job);
            if(t.length === 0){ return "no reasoning"; }
            if(Vars.ui === null || Vars.ui === undefined){ return "no ui"; }
            var d = new Packages.arc.scene.ui.Dialog("MAI · 模型思考（" + t.length + " 字符）");
            try{ d.addCloseButton(); }catch(e0){ }
            try{ d.closeOnBack(); }catch(e1){ }
            var host = new Packages.arc.scene.ui.layout.Table();
            try{ d.cont.pane(host).width(640).height(420).row(); }
            catch(e2){ d.cont.add(host).growX().row(); }
            var rows = jobReasoningLines(job, 100000, 60);
            for(var i = 0; i < rows.length; i++){ host.add(rows[i]).left().row(); }
            try{
                d.buttons.defaults().pad(4);
                d.buttons.button("复制全部", function(){ jobThinkCopy(job); }).width(140);
                if(String(job.state) === "done" && String(job.code || "").length > 0){
                    d.buttons.button("看代码", function(){ d.hide(); previewLogic(job); }).width(140);
                }
                d.buttons.button("关闭", function(){ d.hide(); }).width(140);
            }catch(e3){ log("think dialog buttons failed: " + e3); }
            d.show();
            return "shown " + t.length;
        }

        // 作业列表接口故意剥掉 reasoning（tools/logicd.py 的 BRIEF_DROP），
        // 所以从「作业管理」点进来的行要按 id 再拉一次全量作业，才有思考可看。
        function jobThinkFetchOpen(job){
            if(job === null || job === undefined){ return "no job"; }
            if(jobReasoningText(job).length > 0){ return openThinkDialog(job); }
            jobGet(String(job.id), function(full, why){
                if(full === null){ hint("读不到思考：" + why); return; }
                var r = openThinkDialog(full);
                if(r === "no reasoning"){
                    hint("这个作业没有思考内容（端点没返回 reasoning_content）");
                }
            });
            return "fetching";
        }

        // v0.13.2：作业视图以前只给 findings/代码。两样东西一直存在但没人看得见：
        // 模型的 assumptions（它替玩家假定的前提，错了就是「生成的逻辑不对」的源头）
        // 和 cost 报告（里面有 UNREACHABLE 段落——永远执行不到的语句）。
        function jobAssumptionLines(job, maxLines, perLine){
            var a = job.assumptions || [], out = [], i, s, w = perLine || 88;
            for(i = 0; i < a.length && i < (maxLines || 4); i++){
                s = String(a[i]);
                out.push(s.length > w ? s.substring(0, w - 1) + "…" : s);
            }
            return out;
        }

        function jobCostLines(job, maxLines){
            var c = job.cost;
            if(c === null || c === undefined || String(c).length === 0){ return []; }
            var rows = String(c).split("\n"), out = [];
            for(var i = 0; i < rows.length && i < (maxLines || 8); i++){ out.push(rows[i]); }
            return out;
        }

        function renderJob(host, job, b, d){
            if(host === null || host === undefined || job === null || job === undefined){ return; }
            host.clear();
            host.add(jobProgressText(job)).row();
            var think = jobReasoningLines(job, REASON_LINES, REASON_LINE);
            if(think.length > 0){
                var whole = jobReasoningText(job).length;
                host.add("--- 模型思考（" + whole + " 字符"
                         + (whole > REASON_LINES * REASON_LINE ? "，只显示开头" : "") + "）---").row();
                for(var ti = 0; ti < think.length; ti++){ host.add(think[ti]).row(); }
            }
            var st = String(job.state);
            if(st === "done"){
                var res = jobAsPlan(job), c = jobSevCounts(job);
                host.add(c.err === 0 ? "预检：通过（0 条 ERR）" + (c.total ? "，另有 " + c.warn + " 条 WARN" : "")
                                     : ("预检：ERR " + c.err + " / WARN " + c.warn)).row();
                if(job.explanation){ host.add(String(job.explanation)).row(); }
                var dev = jobEvidenceLines(job);
                for(var di = 0; di < dev.length; di++){ host.add("  " + dev[di]).row(); }
                var ls = job.findings || [];
                for(var i = 0; i < ls.length && i < 6; i++){
                    host.add("  " + ls[i].sev + " L" + ls[i].line + " " + ls[i].msg).row();
                }
                var as = jobAssumptionLines(job, 4, 88);
                if(as.length > 0){
                    host.add("--- 模型假定的前提（错了就是逻辑不对的源头）---").row();
                    for(var ai = 0; ai < as.length; ai++){ host.add("  · " + as[ai]).row(); }
                }
                var costRows = jobCostLines(job, 8);
                if(costRows.length > 0){
                    host.add("--- 成本（UNREACHABLE = 永远执行不到的语句）---").row();
                    for(var ci = 0; ci < costRows.length; ci++){ host.add(costRows[ci]).row(); }
                }
                host.add("--- 代码（" + (job.statements || 0) + " 条语句，" + (job.bytes_len || 0) + " 字节）---").row();
                var code = String(job.code || "").split("\n");
                for(var k = 0; k < code.length && k < 40; k++){ host.add(code[k]).row(); }
                if(code.length > 40){ host.add("… 共 " + code.length + " 行").row(); }
                if(d !== null && d !== undefined){
                    d.buttons.clearChildren();
                    d.buttons.defaults().pad(4);
                    d.buttons.button("写入", function(){ hint(writeBuild(b, res)); d.hide(); }).width(140);
                    // v0.18.4：整段思考 + 复制全部（面板里只摊开头 16 行）。
                    d.buttons.button("思考", function(){ openThinkDialog(job); }).width(110);
                    d.buttons.button("关闭", function(){ d.hide(); }).width(140);
                }
                return;
            }
            if(st === "rejected" || st === "failed" || st === "cancelled"){
                var fs = job.findings || [];
                for(var i2 = 0; i2 < fs.length && i2 < 6; i2++){
                    host.add("  " + fs[i2].sev + " L" + fs[i2].line + " " + fs[i2].msg).row();
                }
                if(job.explanation){ host.add(String(job.explanation)).row(); }
                var why = jobWhyLines(job, 3);
                for(var wi = 0; wi < why.length; wi++){ host.add(why[wi]).row(); }
                var ev = jobEvidenceLines(job);
                if(ev.length > 0){
                    host.add("--- 诊断 ---").row();
                    for(var ei = 0; ei < ev.length; ei++){ host.add("  " + ev[ei]).row(); }
                }
                // v0.18.4：失败/被拒恰恰是最想看思考的时候，以前这里一个按钮都没有。
                if(d !== null && d !== undefined){
                    d.buttons.clearChildren();
                    d.buttons.defaults().pad(4);
                    d.buttons.button("思考", function(){ openThinkDialog(job); }).width(110);
                    d.buttons.button("关闭", function(){ d.hide(); }).width(140);
                }
                return;
            }
            var rh = jobRetryHead(job);
            if(rh !== ""){
                host.add(rh).row();
                var rl = jobFindingLines(job, 4);
                for(var ri = 0; ri < rl.length; ri++){ host.add("  " + rl[ri]).row(); }
                var rw = jobWhyLines(job, 2);
                for(var rwi = 0; rwi < rw.length; rwi++){ host.add("  " + rw[rwi]).row(); }
            }
            host.add("（每 " + (JOB_POLL_MS / 1000) + " 秒查一次，可先做别的）").row();
        }

        // 每帧调一次：只有「有作业 + 面板还开着 + 到点了」才发请求。
        function jobTick(){
            if(aiJob === null){ return; }
            if(!aiShown()){ aiJob = null; return; }       // 面板关了就别轮询了
            var now = Packages.java.lang.System.currentTimeMillis();
            if(now - aiJob.lastPoll < JOB_POLL_MS){ return; }
            aiJob.lastPoll = now;
            var mine = aiJob;
            jobGet(mine.id, function(job, why){
                if(aiJob !== mine){ return; }             // 期间换了作业/取消了
                if(job === null){ return; }               // 网络抖一下：下次再查
                mine.job = job;
                try{ renderJob(mine.host, job, mine.b, mine.d); }
                catch(e){ log("renderJob failed: " + e); }
                if(isJobTerminal(job.state)){
                    jobChatDone(job);
                    // v0.17.0：开了自动写入就在这里落盘（单台 AI 面板那条路）
                    try{ autoWriteApply(mine.b, job); }catch(e2){ log("auto write failed: " + e2); }
                    aiJob = null;
                }
            });
        }

        function startJob(b, host, d, intent){
            if(b === null || b === undefined){ host.add("光标下没有处理器").row(); return "没有目标处理器"; }
            var desc = String(intent);
            if(desc.length === 0){ host.add("先写一句要做什么").row(); return "描述是空的"; }
            var ws = wsTouchHere(b, desc);   // v0.14.0：这台已经在某个工作区里就挂靠过去
            host.clear();
            host.add("正在投递作业…").row();
            jobSubmit(b, desc, function(res, why){
                if(res === null){ host.clear(); host.add("投递失败：" + why).row(); return; }
                aiJob = {id: String(res.id), b: b, desc: desc, host: host, d: d,
                         lastPoll: 0, job: res.job || null};
                jobChatStart(b, String(res.id), desc);   // v0.13.0：启用即播报
                wsRecordJob(ws, String(res.id), desc);   // v0.14.0
                try{ renderJob(host, res.job, b, d); }
                catch(e){ log("renderJob failed: " + e); }
            });
            return "已投递";
        }

        function cancelJob(){
            if(aiJob === null){ return "没有进行中的作业"; }
            var mine = aiJob;
            aiJob = null;
            jobCancel(mine.id, function(job, why){
                hint(why === null ? ("已取消作业 " + mine.id) : ("取消失败：" + why));
            });
            return "已请求取消 " + mine.id;
        }

        // 同步跑完一个作业：**只给控制台/自检用**（会阻塞当前线程）。
        // 游戏内 UI 走的是上面那条异步路径 —— 主线程 busy-wait 永远等不到 Core.app.post 的回调。
        function jobSync(b, desc, timeoutMs){
            rememberOrig(b);                 // v0.10.1：这条路径也是一次投递，基线同样要记
            var t0 = Packages.java.lang.System.currentTimeMillis();
            var limit = (timeoutMs > 0) ? timeoutMs : 20000;
            var r1 = httpRaw("POST", base + "/jobs", JSON.stringify(jobBody(b, desc)));
            var j1 = parse(r1.text);
            if(r1.code !== 200 || j1 === null || j1.ok !== true){
                return JSON.stringify({ok: false, code: r1.code, text: String(r1.text).substring(0, 300)});
            }
            var id = String(j1.id), job = j1.job || null, tries = 0;
            while(tries < 400){
                tries++;
                if(job !== null && isJobTerminal(job.state)){ break; }
                if(Packages.java.lang.System.currentTimeMillis() - t0 > limit){ break; }
                try{ Packages.java.lang.Thread.sleep(50); }catch(e0){ }
                var r2 = httpRaw("GET", base + "/jobs?id=" + id, null);
                var j2 = parse(r2.text);
                if(r2.code === 200 && j2 !== null && j2.ok === true && j2.job){ job = j2.job; }
            }
            if(job === null){ return JSON.stringify({ok: false, code: 0, text: "no job state"}); }
            return JSON.stringify(job);
        }


        // ---------------------------------------------------------- 作业框 / 徽标 / 作业管理（v0.12.5）
        // 用户 2026-10-31：框选区域在作业完成之前保留；正在进行的作业在框选区域右下角显示；
        // 点击作业可以看详细思考过程；要一个统一的作业管理窗口；F3 之后的窗口只剩一个描述框。

        function jobBoxState(){
            if(jobBox === null){ return null; }
            var ids = jobBox.ids || [], i, st, terminal = 0, ok = 0, failed = 0;
            for(i = 0; i < ids.length; i++){
                var j = jobBox.jobs[ids[i]];
                st = (j === null || j === undefined) ? "queued" : String(j.state);
                if(isJobTerminal(st)){
                    terminal++;
                    if(st === "done"){ ok++; } else { failed++; }
                }
            }
            return {total: ids.length, terminal: terminal, done: ok, failed: failed,
                    pending: (jobBox.pend || 0), finished: ids.length > 0 && terminal === ids.length};
        }

        function jobBadgeText(){
            if(jobBox === null){ return ""; }
            var s = jobBoxState();
            if(s === null){ return ""; }
            var written = jobBox.written || 0;
            if(s.total === 0){ return (s.pending > 0) ? "投递中…" : ""; }
            if(!s.finished){ return "作业 " + s.terminal + "/" + s.total + " · 生成中"; }
            if(written > 0){
                return "作业 " + s.total + "/" + s.total + " · 已写入"
                       + (written > 1 ? " " + written + " 台" : "");
            }
            if(s.failed === 0){ return "作业 " + s.total + "/" + s.total + " · 完成"; }
            return "作业 " + s.total + "/" + s.total + " · " + s.done + " 成 " + s.failed + " 败";
        }

        function jobBoxFinishIfDone(){
            if(jobBox === null){ return; }
            var s = jobBoxState();
            if(s !== null && s.finished && jobBox.termAt === 0){
                jobBox.termAt = Packages.java.lang.System.currentTimeMillis();
            }
        }

        // v0.12.7：框选框只有两个消失理由 —— 写入成功，或者玩家主动关。
        function closeJobBox(why){
            if(jobBox === null){ return "没有框选框"; }
            jobBox = null;
            jobBadge = null;
            return (why === null || why === undefined || why === "") ? "框选框已关掉" : String(why);
        }

        function jobBoxContainsBuild(b){
            if(jobBox === null || jobBox.r === null || b === null || b === undefined) { return false; }
            try{
                var t = b.tile;
                if(t === null || t === undefined){ return false; }
                var r = jobBox.r;
                return t.x >= r.x1 && t.x <= r.x2 && t.y >= r.y1 && t.y <= r.y2;
            }catch(e){ return false; }
        }

        // v0.14.2：写入成功不再把框（和工作区）一起抹掉 —— 框留着、工作区留着，玩家点「取消」才收。
        // 这里只记一笔「写了几台」，徽标状态行据此改口。
        function jobBoxWritten(b){
            if(jobBox === null){ return; }
            if(!jobBoxContainsBuild(b)){ return; }
            jobBox.written = (jobBox.written || 0) + 1;
        }

        function jobBoxTick(){
            if(jobBox === null){ return; }
            var now = Packages.java.lang.System.currentTimeMillis();
            // v0.12.7：这一批都终态了就不再轮询，但框和徽标留着 —— 等写入或玩家主动关。
            if(jobBox.termAt > 0){ return; }
            if(now - (jobBox.lastPoll || 0) < JOB_POLL_MS){ return; }
            jobBox.lastPoll = now;
            var ids = jobBox.ids || [], i;
            for(i = 0; i < ids.length; i++){
                var jid = ids[i], known = jobBox.jobs[jid];
                if(known !== null && known !== undefined && isJobTerminal(String(known.state))){ continue; }
                (function(want){
                    jobGet(want, function(job, why){
                        if(jobBox === null){ return; }
                        jobBox.jobs[want] = (job === null)
                            ? {id: want, state: "failed", explanation: String(why)}
                            : job;
                        jobChatDone(jobBox.jobs[want]);   // v0.13.0：完成即播报（同一作业只报一次）
                        if(jobBox.ws !== null && jobBox.ws !== undefined){   // v0.14.0：终态记回工作区
                            wsRecordTerminal(wsLoad(jobBox.ws), jobBox.jobs[want]);
                        }
                        // v0.17.0：自动写入（框选批量那条路）—— 只写框里那一台
                        try{
                            var awj = jobBox.jobs[want];
                            if(awj !== null && awj !== undefined){ autoWriteApply(lookupBuild(awj), awj); }
                        }catch(e4){ log("auto write failed: " + e4); }
                        jobBoxFinishIfDone();
                    });
                })(jid);
            }
            jobBoxFinishIfDone();
        }

        // v0.14.2：作业框的操作区 —— 状态一行 + 两个固定像素尺寸的按钮（查看 / 取消）。
        // 原来那行「（点开看 · 右键关）」是画在世界坐标里的说明字：随镜头缩放会变形，也没有
        // 可点的边界。现在按钮的像素尺寸固定（JOB_BTN_H 屏幕像素高），世界坐标只按
        // 1/camerascale 换算，画面与命中区共用同一份几何（jobButtonLayout）。
        var JOB_BTN_H = 18;      // 按钮高（屏幕像素）
        var JOB_BTN_PAD = 9;     // 按钮左右内边距（屏幕像素）
        var JOB_BTN_GAP = 6;     // 两个按钮之间（屏幕像素）
        var JOB_BTN_EDGE = 4;    // 按钮行离框右边 / 底边（屏幕像素）

        // 量一段字：优先 GlyphLayout 真量，量不出来按字数估（宁可点区大一点，也不要看得见点不着）。
        function jobTextMeasure(font, text, unit){
            var w = 0, hh = 13 * unit;
            if(font !== null && font !== undefined){
                try{
                    font.getData().setScale(unit);
                    var layout = new Packages.arc.graphics.g2d.GlyphLayout(font, text);
                    if(layout.width > 0){ w = layout.width; }
                    if(layout.height > 0){ hh = layout.height; }
                }catch(e){ }
            }
            if(!(w > 0)){ w = text.length * 9 * unit; }
            return {w: w, h: hh};
        }

        // 状态行 + 两个按钮的全部几何（世界坐标）。画和点都只认这一份。
        // 排版：右对齐贴在框右下角外侧，从上到下是「状态行」再「按钮行」，取消在最右、查看在左。
        function jobButtonLayout(a){
            var text = jobBadgeText();
            if(text === ""){ return null; }
            var scale = 1;
            try{ scale = Vars.renderer.camerascale; }catch(e0){ }
            if(!(scale > 0)){ scale = 1; }
            var unit = 1 / scale;
            var font = Packages.mindustry.ui.Fonts.outline;
            var st = jobTextMeasure(font, text, unit);
            var h = JOB_BTN_H * unit, pad = JOB_BTN_PAD * unit, gap = JOB_BTN_GAP * unit;
            var right = a.x + a.w - JOB_BTN_EDGE * unit;      // 按钮行右端
            var statusTop = a.y - JOB_BTN_EDGE * unit;        // 状态行顶端（整行在框外）
            var statusY = statusTop - st.h;                   // 状态行基线（font.draw 的 y 是基线）
            var btnTop = statusY - JOB_BTN_EDGE * unit;       // 按钮行顶端
            var btnY = btnTop - h;
            var defs = [{id: "cancel", label: "取消"}, {id: "view", label: "查看"},
                        {id: "follow", label: "续聊"}];
            var out = [], i, x = right;
            for(i = 0; i < defs.length; i++){
                var tm = jobTextMeasure(font, defs[i].label, unit);
                var w = tm.w + 2 * pad;
                x = x - w;
                out.push({id: defs[i].id, label: defs[i].label,
                          x: x, y: btnY, w: w, h: h, tw: tm.w, th: tm.h});
                x = x - gap;
            }
            return {text: text, scale: scale, unit: unit, box: a,
                    status: {text: text, x: right - st.w, y: statusY, w: st.w, h: st.h},
                    buttons: out, top: statusTop, right: right, h: h, btnTop: btnTop};
        }

        function drawJobButtons(r, a){
            try{
                var m = jobButtonLayout(a);
                if(m === null){ jobBadge = null; return; }
                var font = Packages.mindustry.ui.Fonts.outline;
                var D = Packages.arc.graphics.g2d.Draw;
                var L = Packages.arc.graphics.g2d.Lines;
                var F = Packages.arc.graphics.g2d.Fill;
                var z = D.z();
                D.z(Packages.mindustry.core.Layer.endPixeled);
                var mx = 0, my = 0, hasMouse = true;
                try{ mx = Core.input.mouseWorldX(); my = Core.input.mouseWorldY(); }catch(e0){ hasMouse = false; }
                var i, b, hot;
                for(i = 0; i < m.buttons.length; i++){
                    b = m.buttons[i];
                    hot = hasMouse && mx >= b.x && mx <= b.x + b.w && my >= b.y && my <= b.y + b.h;
                    D.color(Pal.accentBack);
                    D.alpha(hot ? 0.7 : 0.4);
                    F.rect(b.x, b.y, b.w, b.h);
                    D.alpha(1);
                    L.stroke(hot ? 2 : 1);
                    D.color(Pal.accent);
                    L.rect(b.x, b.y, b.w, b.h);
                }
                D.color();
                if(font !== null && font !== undefined){
                    // 原版画选区尺寸时也关掉整数对齐：开着的话 1/camerascale 的小数位置会被
                    // 量化成整数，字会整体飘开框的角——真机上看到的就是「错位」。
                    var ints = true;
                    try{ ints = font.usesIntegerPositions(); font.setUseIntegerPositions(false); }catch(e1){ }
                    font.getData().setScale(m.unit);
                    font.setColor(Pal.accent);
                    font.draw(m.status.text, m.status.x, m.status.y);
                    for(i = 0; i < m.buttons.length; i++){
                        b = m.buttons[i];
                        font.draw(b.label, b.x + (b.w - b.tw) / 2,
                                  b.y + (b.h - b.th) / 2 + b.th * 0.78);
                    }
                    font.setColor(Packages.arc.graphics.Color.white);
                    font.getData().setScale(1);
                    try{ font.setUseIntegerPositions(ints); }catch(e2){ }
                }
                D.z(z);
                jobBadge = {buttons: m.buttons, scale: m.scale, top: m.top, right: m.right};
            }catch(e){ jobBadge = null; }
        }

        // 点「查看」= 开作业管理窗；点「取消」= 关掉这个框选框（工作区留着，只是不画框了）。
        // 世界坐标命中，和 drawJobButtons 用的是同一份 jobButtonLayout 几何。
        function jobButtonsClick(){
            // v0.14.4：控件在管这一帧就交给 arc，别再走世界坐标命中（会双触发）。
            if(jobWidget !== null && jobWidget.attached === true){ return false; }
            if(jobBox === null || jobBadge === null || selDragging){ return false; }
            var right = Core.input.keyTap(Packages.arc.input.KeyCode.mouseRight);
            if(!right && !Core.input.keyTap(Packages.arc.input.KeyCode.mouseLeft)){ return false; }
            var mx = Core.input.mouseWorldX(), my = Core.input.mouseWorldY();
            var bs = jobBadge.buttons || [], i, b;
            for(i = 0; i < bs.length; i++){
                b = bs[i];
                if(mx < b.x || mx > b.x + b.w){ continue; }
                if(my < b.y || my > b.y + b.h){ continue; }
                if(b.id === "cancel" || right){
                    hint(closeJobBox(b.id === "cancel" ? "框选框已关掉（取消按钮）" : "框选框已关掉（右键）"));
            jobWidgetHide();
                    return true;
                }
                if(b.id === "follow"){          // v0.15.1（D1）：对框里这一台继续改进
                    try{ jobFollowAsk(); }
                    catch(e2){ hint("续聊打不开：" + e2); }
                    return true;
                }
                try{
                    openJobManager();
                }catch(e){
                    hint("作业窗口打不开：" + e);
                }
                return true;
            }
            return false;
        }


        // v0.14.4（用户 2026-11-01）：「按照你说的修改UI」——框角的「查看 / 取消」改成原版控件：
        // 往 Vars.ui.hudGroup（UI.java:53/192/239-244，setFillParent + touchable=childrenOnly）
        // 挂一张自己的 Table，按钮用 Styles.defaultt 的 TextButton，由 arc 负责排版、缩放、
        // 命中与悬停。上面那一套即时绘制（drawJobButtons / jobButtonLayout）留作降级：
        // 拿不到 hudGroup 或建表失败时仍能看、能点，绝不因为 UI 变了就失去入口。
        // 坐标：Camera.project(world) 给的是屏幕像素（原点左下、y 向上），与 arc scene 同一套；
        // 若 scene 根节点另有缩放，用 graphics/scene 宽高比换算回来。
        var jobWidget = null;       // {table, label, group, attached}
        var jobWidgetTried = false;

        function worldToScreen(x, y){
            var px = x, py = y;
            try{
                var v = Core.camera.project(x, y);
                if(v !== null && v !== undefined){ px = v.x; py = v.y; }
            }catch(e){ }
            var k = 1;
            try{
                var gw = Core.graphics.getWidth(), sw = Core.scene.getWidth();
                if(gw > 0 && sw > 0){ k = gw / sw; }
            }catch(e2){ }
            return {x: px * k, y: py * k};
        }

        function jobWidgetBuild(){
            if(jobWidget !== null){ return true; }
            if(jobWidgetTried){ return false; }
            jobWidgetTried = true;
            try{
                var ui = Vars.ui;
                if(ui === null || ui === undefined){ return false; }
                var group = ui.hudGroup;
                if(group === null || group === undefined){ return false; }
                var S = Packages.arc.scene.ui.layout.Scl;
                var t = new Packages.arc.scene.ui.layout.Table();
                var lbl = new Packages.arc.scene.ui.Label("", new Packages.arc.scene.ui.Label.LabelStyle(
                    Packages.mindustry.ui.Fonts.outline, Pal.accent));
                var hk = hudScale();
                try{ lbl.setFontScale(hk); }
                catch(ef){ log("label scale skipped: " + ef); }
                t.add(lbl).right().padBottom(S.scl(3) * hk).row();
                var btns = new Packages.arc.scene.ui.layout.Table();
                btns.button("查看", Packages.mindustry.ui.Styles.defaultt, function(){ jobWidgetView(); })
                    .size(S.scl(84) * hk, S.scl(40) * hk).padLeft(S.scl(6) * hk);
                btns.button("取消", Packages.mindustry.ui.Styles.defaultt, function(){ jobWidgetCancel(); })
                    .size(S.scl(84) * hk, S.scl(40) * hk).padLeft(S.scl(6) * hk);
                btns.button("续聊", Packages.mindustry.ui.Styles.defaultt, function(){ jobWidgetFollow(); })
                    .size(S.scl(84) * hk, S.scl(40) * hk).padLeft(S.scl(6) * hk);
                t.add(btns).right().row();
                t.pack();
                jobWidget = {table: t, label: lbl, group: group, attached: false};
                log("job widget ready (native buttons)");
                return true;
            }catch(e){
                log("job widget unavailable, keeping the drawn buttons: " + e);
                jobWidget = null;
                return false;
            }
        }

        // v0.18.1（用户 m10374）：「查看详情的UI窗口做的不好……彻底移除旧版的查看详情」。
        // 现在「查看」只有两种结果：
        //   已完成 → 开原版逻辑编辑器预览模型写的逻辑（executor 传 null、privileged 传 false：
        //            只预览，不绑定任何处理器，改了也写不回去）；
        //   没完成 → 把思考过程打印到本机聊天框（ChatFragment.addMessage 只 append 本地列表，
        //            不走 Call.sendChatMessage，所以不会发到服务器）。
        function jobViewPick(){
            if(jobBox === null || jobBox.ids === null || jobBox.ids === undefined){ return null; }
            var ids = jobBox.ids, i, j;
            try{
                var b = aimedBuild();
                if(b !== null){
                    for(i = 0; i < ids.length; i++){
                        j = jobBox.jobs[ids[i]];
                        if(j !== null && j !== undefined && lookupBuild(j) === b){ return j; }
                    }
                }
            }catch(e0){ log("job view pick by aim failed: " + e0); }
            for(i = 0; i < ids.length; i++){
                j = jobBox.jobs[ids[i]];
                if(j !== null && j !== undefined){ return j; }
            }
            return null;
        }

        // 只进本机聊天框：ChatFragment.java:311 的 addMessage(String) 只是往本地 list 里加一条。
        function chatLocalSay(line){
            if(Vars.ui === null || Vars.ui === undefined){ return false; }
            var frag = null;
            try{ frag = Vars.ui.chatfrag; }catch(e0){ frag = null; }
            if(frag === null || frag === undefined){ return false; }
            try{ frag.addMessage(String(line)); return true; }
            catch(e){ log("local chat failed: " + e); return false; }
        }

        function jobThinkToChat(job){
            var shown = 0;
            try{
                var ls = jobReasoningLines(job, REASON_LINES, REASON_LINE);
                for(var i = 0; i < ls.length; i++){
                    if(!chatLocalSay("MAI · " + ls[i])){ break; }
                    shown++;
                }
            }catch(e){ log("thinking to chat failed: " + e); }
            if(shown === 0){ chatLocalSay("MAI · " + jobProgressText(job)); }
            return shown;
        }

        function previewLogic(job){
            var code = "";
            try{ code = String(job && job.code ? job.code : ""); }catch(e0){ code = ""; }
            if(code.length === 0){ return "作业里没有代码可看（载荷还没回来）"; }
            try{
                if(Vars.ui === null || Vars.ui.logic === null || Vars.ui.logic === undefined){
                    return "逻辑编辑器不可用（无界面环境）";
                }
                // 原版调用形状见 LogicBlock.java:754：ui.logic.show(code, executor, privileged, cons)。
                Vars.ui.logic.show(code, null, false, function(txt){ log("logic preview closed"); });
                return "";
            }catch(e){
                try{ Vars.ui.showText("Mlog辅助 · 逻辑预览", code); return ""; }
                catch(e2){ return "预览失败：" + e; }
            }
        }

        function jobWidgetView(){
            var job = jobViewPick();
            if(job === null){
                hint("这一批作业还没回来，稍等一下再点「查看」。");
                return;
            }
            var state = String(job.state);
            if(isJobTerminal(state)){
                // v0.18.4：终态以前 done 只弹代码预览、失败只弹一行状态，思考看不到。
                // 现在先开思考窗（全量 + 复制全部）；端点没给思考才退回老行为。
                var tr = openThinkDialog(job);
                if(tr === "no reasoning" || tr === "no ui"){
                    if(state === "done"){
                        var why = previewLogic(job);
                        if(why !== ""){ hint(why); }
                    }else{
                        hint("作业" + stateCn(state) + "：" + jobProgressText(job)
                             + "（端点没返回思考内容）");
                    }
                }
                return;
            }
            if(!chatLocalSay("MAI · " + jobProgressText(job))){
                hint(jobProgressText(job));
            }
            jobThinkToChat(job);
        }

        function jobWidgetCancel(){
            // v0.15.2：框没了就得把控件一起摘掉，不然「查看 / 续聊」留在屏幕上。
            hint(closeJobBox("框选框已关掉（取消按钮）"));
            jobWidgetHide();
        }

        function jobWidgetFollow(){
            try{ jobFollowAsk(); }
            catch(e){ hint("续聊打不开：" + e); }
        }

        function jobWidgetHide(){
            if(jobWidget === null || jobWidget.attached !== true){ return; }
            try{ jobWidget.table.remove(); }
            catch(e){ log("job widget detach failed: " + e); }
            jobWidget.attached = false;
        }

        // 倍率变了要按新尺寸重建：摘掉旧的、把 jobWidgetTried 复位，
        // 下一帧 jobButtonsSync 的 jobWidgetBuild() 就会用新倍率重新排一遍。
        function jobWidgetDrop(){
            jobWidgetHide();
            try{ if(jobWidget !== null && jobWidget.table !== null){ jobWidget.table.remove(); } }
            catch(e){ log("job widget drop failed: " + e); }
            jobWidget = null;
            jobWidgetTried = false;
        }

        // 每帧同步：框还在就把控件摆到框右下角外侧，框没了就摘下来。
        // 返回 true 表示「这一帧由控件负责」，调用方就别再画即时版了。
        function jobButtonsSync(){
            if(!jobWidgetBuild()){ return false; }
            var w = jobWidget;
            var text = jobBadgeText();
            var show = (jobBox !== null && jobBox.r !== null && !selDragging && text !== "");
            if(!show){ jobWidgetHide(); return true; }
            var a = selDrawArgs(jobBox.r);
            var p = worldToScreen(a.x + a.w, a.y);
            var hk2 = hudScale();
            var edge = JOB_BTN_EDGE * hk2;
            try{ edge = Packages.arc.scene.ui.layout.Scl.scl(JOB_BTN_EDGE) * hk2; }catch(e0){ }
            try{
                w.label.setText(text);
                w.table.pack();
                var tw = 0, th = 0;
                try{ tw = w.table.width; th = w.table.height; }catch(e1){ }
                w.table.setPosition(p.x - edge - tw, p.y - edge - th);
                if(!w.attached){
                    w.group.addChild(w.table);
                    w.attached = true;
                }
            }catch(e2){
                log("job widget sync failed, falling back to drawn buttons: " + e2);
                jobWidgetHide();
                return false;
            }
            return true;
        }

        // Rhino 的 String.prototype 不一定有 trim（v160.7 里 split 都能等价空匹配），自己来。
        function trimText(s){
            var t = String(s === null || s === undefined ? "" : s);
            var a = 0, b = t.length;
            while(a < b && t.charAt(a) <= " "){ a++; }
            while(b > a && t.charAt(b - 1) <= " "){ b--; }
            return t.substring(a, b);
        }

        function stateCn(state){
            var s = String(state);
            if(s === "queued"){ return "排队"; }
            if(s === "running"){ return "生成中"; }
            if(s === "done"){ return "完成"; }
            if(s === "rejected"){ return "被拒"; }
            if(s === "failed"){ return "失败"; }
            if(s === "cancelled"){ return "已取消"; }
            return s;
        }

        // ---------------------------------------------------------- 聊天框播报（v0.13.0）
        // 用户 2026-10-31：作业「启用」和「完成」的时候把状态发到聊天框，格式
        //   MAI<逻辑徽标> Generating (12,34): 给核心补铜（job 3f2a1b9c）
        // 聊天框画字用的是 Fonts.def，图标必须是真 glyph：UI.formatIcons(":logic:")
        // 会把 :name: 换成 Iconc 里的字符（和设置分类那个 Icon.logic 同源），
        // 所以不要写 "[logic]" —— 那只是标签语法，聊天框不认。
        var CHAT_DONE = {};        // 作业 id -> 终态已经播报过（一个作业只报一次）
        var CHAT_ICON = null;      // 逻辑徽标字符；拿不到就是空串（照样播报，只是没图标）

        function chatIcon(){
            if(CHAT_ICON !== null){ return CHAT_ICON; }
            CHAT_ICON = "";
            try{
                var s = String(Packages.mindustry.core.UI.formatIcons(":logic:"));
                if(s.length > 0 && s !== ":logic:"){ CHAT_ICON = s; }
            }catch(e){ log("chat icon unavailable: " + e); }
            return CHAT_ICON;
        }

        // v0.15.2（用户 2026-11-01）：尖括号不能省，:logic: 用原文 —— 原版聊天在发送前会过
        // UI.formatIcons（ChatFragment.java:230），:logic: 那一刻才变成逻辑图标。
        // 所以原始行就是 <MAI:logic:><状态>(x,y):详情，转换只发生在发送 / 显示那一刻。
        function chatLine(status, x, y, info){
            var tail = (info === null || info === undefined || String(info) === "") ? "" : (":" + String(info));
            return "<MAI:logic:><" + String(status) + ">(" + String(x) + "," + String(y) + ")" + tail;
        }

        // 发送 / 显示前的唯一一次转换：:logic: -> 图标（拿不到图标就原样保留）。
        function chatPretty(text){
            var s = String(text);
            try{ return String(Packages.mindustry.core.UI.formatIcons(s)); }
            catch(e){ log("formatIcons failed: " + e); return s; }
        }

        // v0.15.2（用户 2026-11-01）：「发送到聊天框」要真发一条聊天消息，不是本地提示。
        // 走原版同一条路 —— Call.sendChatMessage(String)（ChatFragment.java:236），
        // 客机也能发（服务端广播），房主和客机看到的一样。
        // 单机 / 无头（没有聊天通道）仍退回淡出提示，状态不会因为没网络就消失。
        function chatSay(text){
            var s = chatPretty(text);
            try{ log("[chat] " + s); }catch(e){ }
            if(chatSendOn() && netActive()){
                try{
                    Packages.mindustry.gen.Call.sendChatMessage(s);
                    return "chat";
                }catch(e){ log("chatSay failed, falling back to hint: " + e); }
            }
            hint(s);
            return "hint";
        }

        // v0.14.4（用户 2026-11-01）：播报要不要进服务器聊天框，做成设置项，默认关。
        // 关掉时仍走 hint（本机淡出提示），状态不会因为「不想进聊天框」就消失。
        function chatSendOn(){
            try{ return Core.settings.getBool(CHAT_SEND_KEY, false) === true; }
            catch(e){ return false; }
        }

        function toggleChatSend(){
            var want = !chatSendOn();
            try{ Core.settings.put(CHAT_SEND_KEY, want); }
            catch(e){ log("save " + CHAT_SEND_KEY + " failed: " + e); }
            log("chat broadcast = " + want);
            return want;
        }

        // v0.14.5（用户 2026-11-01）：「做一个hud倍率自己调就好」。
        // 只影响框角那两个原版控件（查看 / 取消）和它们上面的状态条，不影响游戏其它 HUD。
        function hudScaleClamp(v){
            var n = v;
            if(!(n > 0)){ n = 1; }
            if(n < HUD_SCALE_MIN){ n = HUD_SCALE_MIN; }
            if(n > HUD_SCALE_MAX){ n = HUD_SCALE_MAX; }
            return Math.round(n * 100) / 100;
        }

        // v0.18.1（用户 m10374）：「框选控件缩放……而且好像改不了」—— 确实改不了。
        // 根因：arc 的 Settings.getFloat 就是 `(float)get(name, def)`（Settings.java:357-359），
        // Java 里这是「先当 Float 再拆箱」，而 JS 数字经 put(String,Object) 进 values 是
        // java.lang.Double ⇒ 每次读都抛 ClassCastException，被下面的 catch 吞掉后永远退回 1×。
        // 现在存字符串、读 Object 再 parseFloat（老存档里可能已经是 Double，所以也不能走 getString）。
        function hudScaleRaw(){
            var o = null;
            try{ o = Core.settings.get(HUD_SCALE_KEY, null); }catch(e0){ o = null; }
            if(o === null || o === undefined){ return 1; }
            var n = 0;
            try{ n = parseFloat(String(o)); }catch(e1){ n = 0; }
            return n;
        }

        function hudScale(){
            return hudScaleClamp(hudScaleRaw());
        }

        function hudScaleText(){
            var s = String(hudScale());
            return s + "×";
        }

        function hudScaleBtnText(){
            return "倍率 " + hudScaleText();
        }

        function hudScaleRefresh(){
            try{
                if(hudScaleBtn !== null && hudScaleBtn !== undefined){
                    hudScaleBtn.get().setText(hudScaleBtnText());
                }
            }catch(e){ log("hud scale label not updated"); }
        }

        function hudScaleSet(v){
            var n = hudScaleClamp(v);
            try{ Core.settings.put(HUD_SCALE_KEY, String(n)); }
            catch(e){ log("save " + HUD_SCALE_KEY + " failed: " + e); }
            jobWidgetDrop();          // 按新倍率重建，下一帧 jobButtonsSync 会重做
            log("hud scale = " + n);
            return n;
        }

        function hudScaleStep(dir){
            return hudScaleSet(hudScale() + (dir > 0 ? HUD_SCALE_STEP : -HUD_SCALE_STEP));
        }

        // 滑条没有 changed 回调（ProgressBar 只有 setValue/getValue），所以每帧对一次读数：
        // 拖动时立刻跟手，松手后也停在原处。
        function hudSliderTick(){
            if(hudSlider === null || hudSlider === undefined){ return; }
            try{
                var v = hudSlider.getValue();
                if(hudSliderLast >= 0 && Math.abs(v - hudSliderLast) < 0.005){ return; }
                hudSliderLast = v;
                hudScaleSet(v);
                try{
                    if(hudScaleLbl !== null && hudScaleLbl !== undefined){
                        hudScaleLbl.get().setText(hudScaleText());
                    }
                }catch(e2){ log("hud scale label not updated"); }
            }catch(e){
                hudSlider = null;
                log("hud slider dropped: " + e);
            }
        }

        function chatTrim(s, max){
            var v = String(s === null || s === undefined ? "" : s);
            if(v.length > max){ v = v.substring(0, max) + "…"; }
            return v;
        }

        function chatXY(b){
            try{
                if(b !== null && b !== undefined && b.tile !== null && b.tile !== undefined){
                    return {x: b.tile.x, y: b.tile.y};
                }
            }catch(e){ }
            return {x: "?", y: "?"};
        }

        // 启用：投递成功那一刻播报（真正的模型调用紧跟着开始）。
        function jobChatStart(b, id, desc){
            var p = chatXY(b);
            var d = chatTrim(desc, 24);
            var info = (d.length > 0 ? d : "（没写描述）") + "（job " + String(id) + "）";
            chatSay(chatLine("Generating", p.x, p.y, info));
        }

        // 完成：终态只播报一次（jobTick 和 jobBoxTick 可能同时看到同一个作业）。
        function jobChatDone(job){
            if(job === null || job === undefined){ return; }
            var st = String(job.state);
            if(!isJobTerminal(st)){ return; }
            var id = (job.id === undefined || job.id === null) ? "" : String(job.id);
            if(id.length > 0){
                if(CHAT_DONE[id] === true){ return; }
                CHAT_DONE[id] = true;
            }
            var x = "?", y = "?";
            try{
                var o = job.origin;
                if(o !== null && o !== undefined && o.x !== undefined){ x = o.x; y = o.y; }
            }catch(e){ }
            if(x === "?"){
                var lb = lookupBuild(job);
                var q = chatXY(lb);
                if(q.x !== "?"){ x = q.x; y = q.y; }
            }
            var status, info;
            if(st === "done"){
                status = "Done";
                var c = jobSevCounts(job), tpl = String(job.template || "");
                info = (tpl.length > 0 ? tpl + " · " : "") + String(job.statements || 0) + " 条语句 · "
                     + String(job.bytes_len || 0) + " 字节 · " + (c.err === 0 ? "0 ERR" : ("ERR " + c.err));
            }else if(st === "rejected"){
                status = "Rejected";
                var c2 = jobSevCounts(job);
                info = "静态检查有 ERR（" + c2.err + " 条）";
            }else if(st === "cancelled"){
                status = "Cancelled";
                info = "已取消";
            }else{
                status = "Failed";
                info = chatTrim(String(job.explanation || ""), 60);
                if(String(info) === ""){ info = "没有说明"; }
            }
            chatSay(chatLine(status, x, y, info));
        }

        // 一个描述框 → 这块区域里每个处理器各投一个作业（P5 批量入口，v0.12.5）。
        function startJobBatch(targets, desc, rect, improve, wsIn){
            // v0.17.1：签名挂载 / 已认出的工作区直接沿用，别再新建一个
            var ws = (wsIn === undefined || wsIn === null)
                ? wsAttach(rect, targets, desc)            // v0.14.0：这块框选 = 一个工作区快照
                : wsAttachCtx(wsIn, rect, targets, desc);
            var box = {r: rect, ws: (ws === null ? null : String(ws.id)),
                       desc: desc, targets: targets,      // v0.15.1：续聊要认得出框里哪几台
                       ids: [], jobs: {}, pend: targets.length,
                       started: Packages.java.lang.System.currentTimeMillis(),
                       termAt: 0, lastPoll: 0};
            jobBox = box;
            jobBadge = null;
            var failed = 0;
            for(var i = 0; i < targets.length; i++){
                (function(t){
                    var payload = (improve === true) ? wsImprovePayload(ws, t.b) : null;
                    jobSubmit(t.b, desc, function(res, why){
                        box.pend--;
                        if(res === null || res.id === undefined || res.id === null){
                            failed++;
                            hint("投递失败（@" + t.x + "," + t.y + "）：" + String(why));
                        }else{
                            var jid = String(res.id);
                            box.ids.push(jid);
                            box.jobs[jid] = res.job ? res.job : {id: jid, state: "queued", desc: desc};
                            jobChatStart(t.b, jid, desc);     // v0.13.0：启用即播报
                            wsRecordJob(ws, jid, desc);       // v0.14.0：工作区记住这次投递
                        }
                        if(box.pend === 0){
                            if(box.ids.length === 0){
                                jobBox = null;
                                hint("这批作业一个也没投出去：" + String(why));
                            }else{
                                hint("已投 " + box.ids.length + " 个作业：点框右下角看进度；写入成功后框才消失（右键徽标关闭）"
                                     + (ws === null ? "" : "；工作区「" + String(ws.name) + "」已保存，继续框选会复用"));
                            }
                        }
                        jobBoxFinishIfDone();
                    }, payload, {i: i + 1, n: targets.length});
                })(targets[i]);
            }
            return "batch:" + targets.length;
        }

        // v0.12.5：F3 松手只弹这一个框（用户：f3 后的窗口简化到只有一个描述框）。
        // 框住的处理器有几个就投几个作业；矩形留着，直到这一批全部终态。
        function openQuickJob(r){
            if(Vars.ui === null || Vars.ui === undefined){ return "no ui"; }
            var targets = [], i, ps = [];
            try{ ps = regionSnapshot(r.x1, r.y1, r.x2, r.y2).processors || []; }catch(e){ ps = []; }
            var seenQ = {};
            for(i = 0; i < ps.length; i++){
                var pb = processorAt(ps[i].x, ps[i].y);
                if(pb === null){ continue; }
                // v0.18.3（用户 m11320）：同一台处理器只投一单（防多格建筑重复枚举）。
                var qid = null;
                try{ qid = String(pb.id); }catch(eQ){ qid = null; }
                if(qid === null || qid === "undefined"){ qid = "t" + ps[i].x + "," + ps[i].y; }
                if(seenQ[qid] === true){ continue; }
                seenQ[qid] = true;
                targets.push({b: pb, x: ps[i].x, y: ps[i].y});
            }
            if(targets.length === 0){
                hint("区域里没有处理器：拖框要把处理器框进去（F2 看全图清单）");
                return "no processor in region";
            }
            var desc = "";
            var sig = sigCtxInRegion(targets);                       // v0.17.1：签名挂载优先
            var wsHit = null, wsFromSignature = false, i2;
            if(sig !== null && String(sig.meta.ws).length > 0){
                wsHit = wsLoad(String(sig.meta.ws));
                wsFromSignature = (wsHit !== null);
            }
            if(wsHit === null && sig !== null && wsFromSignature === false && String(sig.meta.ws).length === 0){
                wsHit = wsFromSig(r, sig);
                wsFromSignature = (wsHit !== null);
            }
            if(wsHit === null){
                var wsId = wsFindActive(r);                          // v0.14.0 老路子
                wsHit = (wsId === null) ? null : wsLoad(wsId);
            }
            var d = new Packages.arc.scene.ui.Dialog("Mlog辅助 · 描述 v" + MOD_VERSION
                + (wsHit === null ? "" : " · 工作区「" + String(wsHit.name) + "」"));
            d.cont.defaults().pad(6).left();
            if(wsFromSignature && sig !== null){
                d.cont.add("已从签名挂载读到上下文：" + String(wsHit.name)
                    + (String(sig.meta.job).length > 0 ? "（作业 " + String(sig.meta.job) + "）" : "")
                    + " · 来自 @" + String(sig.x) + "," + String(sig.y)).row();
            }else if(wsHit !== null){
                d.cont.add("复用已有工作区：写完不消失，可继续改进；归档 / 释放见作业管理窗").row();
            }
            d.cont.add(targets.length === 1
                        ? ("目标：@" + targets[0].x + "," + targets[0].y)
                        : ("目标：区域里 " + targets.length + " 个处理器，每个处理器生成一段")).row();
            var prefill = "";
            if(wsFromSignature && wsHit !== null){ prefill = String(wsHit.notes || ""); }
            d.cont.field(prefill, function(v){ desc = String(v); }).width(520).row();
            // v0.17.0：生成界面也给一个自动写入开关（用户 m09440 / 1）
            var awCell = d.cont.button(autoWriteText(), function(){
                autoWriteSet(!autoWriteOn());
                try{ awCell.get().setText(autoWriteText()); }catch(e0){ log("auto write label not updated"); }
            }).width(240).left().row();
            d.buttons.button("生成", function(){
                var text = trimText(desc);
                if(text.length === 0){ hint("先写一句描述：这段逻辑要做什么"); return; }
                d.hide();
                startJobBatch(targets, text, r, false, wsHit);
            }).width(120);
            if(wsHit !== null){
                var canImprove = false, pl = wsHit.procs || [];
                for(i = 0; i < pl.length; i++){ if(String(pl[i].code || "").length > 0){ canImprove = true; } }
                if(canImprove){
                    d.buttons.button("改进上一版", function(){
                        var text = trimText(desc);
                        if(text.length === 0){ text = "在上一版的基础上继续改进"; }
                        d.hide();
                        startJobBatch(targets, text, r, true, wsHit);
                    }).width(140);
                }
            }
            d.buttons.button("取消", function(){ d.hide(); }).width(120);
            d.closeOnBack();
            d.show();
            return "quick job: " + targets.length + " processor(s)";
        }

        // v0.15.1（D1）：框还在的时候，对框里某一台「再说一句」。
        // 以前只有工作区窗里的「继续改进」能带上一版代码，框还在时反而没入口。
        // 目标怎么认：光标下那台在框里就用它；否则框里只有一台就用那一台。
        function jobFollowPick(){
            if(jobBox === null || jobBox.r === null){ return null; }
            var pl = jobBox.targets || [], i;
            var mx = -1, my = -1;
            try{ mx = Core.input.mouseWorldX(); my = Core.input.mouseWorldY(); }catch(e){ }
            if(mx >= 0 && my >= 0){
                var b = processorAt(mx, my);
                if(b !== null){
                    for(i = 0; i < pl.length; i++){ if(pl[i].b === b){ return pl[i]; } }
                }
            }
            return pl.length === 1 ? pl[0] : null;
        }

        function jobFollowAsk(){
            if(jobBox === null || jobBox.r === null){
                hint("还没有框选框：先 F3 框一台处理器");
                return "no box";
            }
            var t = jobFollowPick();
            if(t === null){
                hint("框里有 " + String((jobBox.targets || []).length)
                     + " 台处理器：把光标移到要续聊的那台上再点「续聊」（或只框一台）");
                return "ambiguous";
            }
            var ws = (jobBox.ws === null || jobBox.ws === undefined) ? null : wsLoad(jobBox.ws);
            var desc = "";
            if(ws !== null && String(ws.notes || "").length > 0){ desc = String(ws.notes); }
            else if(String(jobBox.desc || "").length > 0){ desc = String(jobBox.desc); }
            var d = new Packages.arc.scene.ui.Dialog("Mlog辅助 · 续聊 @" + t.x + "," + t.y
                + " v" + MOD_VERSION);
            d.cont.defaults().pad(6).left();
            d.cont.add("目标：@" + t.x + "," + t.y
                       + (ws === null ? "" : ("　工作区「" + String(ws.name) + "」"))
                       + "　在这台上接着上一版改，别的处理器不受影响").row();
            d.cont.field(desc, function(v){ desc = String(v); }).width(520).row();
            d.buttons.button("生成", function(){
                var text = trimText(desc);
                if(text.length === 0){ hint("先写一句：这台处理器接下来要改什么"); return; }
                d.hide();
                jobFollowSubmit(t, text);
            }).width(120);
            d.buttons.button("取消", function(){ d.hide(); }).width(120);
            d.closeOnBack();
            d.show();
            return "follow ask: @" + t.x + "," + t.y;
        }

        // 投出去之后不再新建框：作业挂到当前这个框上（徽标计数、进度、写入都归这一批）。
        function jobFollowSubmit(t, desc){
            if(jobBox === null || jobBox.r === null){ return "no box"; }
            var r = jobBox.r;
            var ws = wsAttach(r, [{b: t.b, x: t.x, y: t.y}], desc);
            var payload = wsImprovePayload(ws, t.b);
            jobSubmit(t.b, desc, function(res, why){
                if(res === null || res.id === undefined || res.id === null){
                    hint("续聊投递失败（@" + t.x + "," + t.y + "）：" + String(why));
                    return;
                }
                var jid = String(res.id);
                jobBox.ids.push(jid);
                jobBox.jobs[jid] = res.job ? res.job : {id: jid, state: "queued", desc: desc};
                jobBox.termAt = 0;
                jobBox.lastPoll = 0;
                jobChatStart(t.b, jid, desc);
                wsRecordJob(ws, jid, desc);
                hint("已续聊 @" + t.x + "," + t.y + "：点框角「查看」看进度");
            }, payload);
            return "follow: @" + t.x + "," + t.y;
        }

        function lookupBuild(job){
            try{
                var o = job ? job.origin : null;
                if(o === null || o === undefined){ return null; }
                return processorAt(Number(o.x), Number(o.y));
            }catch(e){ return null; }
        }

        function jobRowLabel(job){
            var desc = String(job.desc || "");
            if(desc.length > 20){ desc = desc.substring(0, 20) + "…"; }
            return "#" + String(job.id) + "   " + stateCn(job.state) + "   " + desc;
        }

        function renderManagerDetail(host, d, job, b){
            managerSeq++;                       // 作废还挂在路上的列表回调
            try{ host.clear(); }catch(e){ return; }
            host.add("作业 #" + String(job.id)
                     + (job.retry_of ? ("（重跑自 " + String(job.retry_of) + "）") : "")).row();
            host.add(jobProgressText(job)).row();
            var think = jobReasoningLines(job, REASON_LINES, REASON_LINE);
            if(think.length > 0){
                var whole = jobReasoningText(job).length;
                host.add("--- 模型思考（" + whole + " 字符"
                         + (whole > REASON_LINES * REASON_LINE ? "，只显示开头" : "") + "）---").row();
                for(var ti = 0; ti < think.length; ti++){ host.add(think[ti]).row(); }
            }
            var state = String(job.state);
            var head = jobRetryHead(job);
            if(head !== ""){ host.add(head).row(); }
            var fs = job.findings || [];
            for(var fi = 0; fi < fs.length && fi < 6; fi++){
                host.add(String(fs[fi].sev) + " " + String(fs[fi].rid) + "  "
                         + String(fs[fi].msg)).row();
                if(String(fs[fi].msg).indexOf("finish_reason=length") >= 0){
                    host.add("  （模型把预算用在思考上，sidecar 会自动加大预算重试）").row();
                }
            }
            if(state === "done" && b !== null && b !== undefined && job.bytes_b64){
                host.button("写入到 @" + b.tile.x + "," + b.tile.y, function(){
                    hint(writeBuild(b, jobAsPlan(job)));
                    d.hide();
                }).width(280).row();
            }
            if(!isJobTerminal(state)){
                host.button("取消这个作业", function(){
                    jobCancel(String(job.id), function(canceled, why){
                        hint(canceled !== null ? ("已取消 " + String(job.id)) : ("取消失败：" + why));
                        renderManager(host, d);
                    });
                }).width(280).row();
            }
            // v0.18.4：列表接口不带 reasoning，按钮里按 id 再拉一次全量作业。
            (function(jb){
                var n = jobReasoningText(jb).length;
                host.button(n > 0 ? ("思考（" + n + " 字符）") : "思考（读取全文）", function(){
                    jobThinkFetchOpen(jb);
                }).width(280).row();
            })(job);
            host.button("返回列表", function(){ renderManager(host, d); }).width(280).row();
        }

        function renderManager(host, d){
            var seq = ++managerSeq;
            try{ host.clear(); }catch(e){ return; }
            var s = jobBoxState();
            host.add("— 本次框选 —").row();
            if(s !== null && jobBox !== null && s.total > 0){
                var ids = jobBox.ids || [], shown = 0;
                for(var i = 0; i < ids.length; i++){
                    (function(jid){
                        var job = jobBox.jobs[jid] || {id: jid, state: "queued", desc: ""};
                        if(!jobHit(job)){ return; }
                        shown++;
                        host.button(jobRowLabel(job), function(){
                            renderManagerDetail(host, d, job, lookupBuild(job));
                        }).width(560).row();
                    })(ids[i]);
                }
                if(shown === 0){ host.add("（本次框选里没有符合过滤条件的作业）").row(); }
            }else{
                host.add("（框选一块区域，写一句描述，作业就出现在这里）").row();
            }
            host.add("— 全部作业（最近 " + String(managerFilter.limit) + " 个）—").row();
            httpJson("GET", "/jobs?all=1&limit=" + String(managerFilter.limit), null, function(code, text){
                if(seq !== managerSeq){ return; }
                var res = parse(text);
                if(code !== 200 || res === null || res.ok !== true){
                    host.add("读不到作业列表：" + httpWhy(code, text)).row();
                    return;
                }
                var rows = res.jobs || [], k, hit = 0;
                for(k = 0; k < rows.length; k++){ if(jobHit(rows[k])){ hit++; } }
                if(rows.length === 0){ host.add("（sidecar 还没有作业）").row(); return; }
                host.add("符合条件 " + String(hit) + " / " + String(rows.length) + " 个").row();
                if(hit === 0){ host.add("（换个关键字，或关掉「只看失败」）").row(); return; }
                for(k = 0; k < rows.length; k++){
                    (function(job){
                        if(!jobHit(job)){ return; }
                        host.button(jobRowLabel(job), function(){
                            renderManagerDetail(host, d, job, lookupBuild(job));
                        }).width(560).row();
                    })(rows[k]);
                }
            });
        }

        function openJobManager(){
            if(Vars.ui === null || Vars.ui === undefined){ return "no ui"; }
            var d = new Packages.arc.scene.ui.Dialog("Mlog辅助 · 作业管理 v" + MOD_VERSION);
            d.cont.defaults().pad(4).left();
            var host = new Packages.arc.scene.ui.layout.Table();
            try{ d.cont.pane(host).height(320).width(600).row(); }catch(e){ d.cont.add(host).row(); }
            var frow = new Packages.arc.scene.ui.layout.Table();
            frow.margin(4).left();
            frow.add("过滤：");
            frow.field(String(managerFilter.text || ""), function(v){
                managerFilter.text = String(v);
                renderManager(host, d);
            }).width(220).padRight(6);
            managerFilterBtn = frow.button(managerFilterBtnText(), function(){
                managerFilter.onlyBad = !(managerFilter.onlyBad === true);
                try{ managerFilterBtn.get().setText(managerFilterBtnText()); }catch(e0){ }
                renderManager(host, d);
            }).width(150).padRight(6);
            frow.button("显示更多", function(){
                managerFilter.limit = Math.min(200, managerFilter.limit + 100);
                renderManager(host, d);
            }).width(120);
            d.cont.add(frow).left().row();
            d.buttons.button("刷新", function(){ renderManager(host, d); }).width(110);
            d.buttons.button("存成蓝图", function(){ blueprintSaveBoxAsk(null); }).width(130);   // v0.16.0 B1
            d.buttons.button("工作区快照", function(){ openWorkspaceDialog(); }).width(140);   // v0.14.1
            d.buttons.button("关闭框选框", function(){ hint(closeJobBox("框选框已关掉")); d.hide(); }).width(140);
            d.buttons.button("关闭", function(){ d.hide(); }).width(110);
            d.closeOnBack();
            d.show();
            renderManager(host, d);
            return "job manager";
        }

        // ------------------------------------------------ 工作区界面（v0.14.1，W2/W3/W4）
        // 仿蓝图对话框：上面是列表（进行中在前，已归档在后），点一行看预览，
        // 预览里有「恢复选框 / 归档或恢复 / 释放 / 改名 / 导出 JSON / 继续改进」。
        // 无头环境（Vars.ui === null）不建窗口，只跑纯函数，测试走 api.maiWorkspace*。

        function wsTimeText(ms){
            var n = Number(ms || 0);
            if(n <= 0){ return "?"; }
            try{
                var d = new Date(n);
                return (d.getMonth() + 1) + "-" + d.getDate() + " "
                     + d.getHours() + ":" + (d.getMinutes() < 10 ? "0" : "") + d.getMinutes();
            }catch(e){ return "?"; }
        }

        function wsRowLabel(ws){
            var st = String(ws.state || "active") === "active" ? "进行中" : "已归档";
            return String(ws.name) + "   · " + String((ws.procs || []).length) + " 台 · "
                 + st + " · " + wsTimeText(ws.updated);
        }

        function wsBoundsText(ws){
            var b = ws.bounds || {};
            return "地图 " + String(ws.map || "?") + "  (" + String(b.x1) + "," + String(b.y1)
                 + ")-(" + String(b.x2) + "," + String(b.y2) + ")";
        }

        function wsProcLines(ws, maxLines){
            var out = [], list = ws.procs || [], i;
            for(i = 0; i < list.length && i < maxLines; i++){
                var r = list[i];
                var n = 0;
                try{ n = String(r.code || "").split("\n").length; }catch(e){ n = 0; }
                out.push("@" + String(r.x) + "," + String(r.y) + "  " + String(r.block || "?")
                         + "  " + (String(r.code || "").length > 0 ? (n + " 行") : "（还没写入过）")
                         + (Number(r.accepted || 0) > 0 ? ("  · 写过 " + String(r.accepted) + " 次") : "")
                         + (String(r.desc || "").length > 0 ? ("  · " + String(r.desc).substring(0, 24)) : ""));
            }
            if(list.length > maxLines){ out.push("… 还有 " + String(list.length - maxLines) + " 台"); }
            if(out.length === 0){ out.push("（这个工作区还没有处理器）"); }
            return out;
        }

        function wsJobLines(ws, maxLines){
            var out = [], list = ws.jobs || [], i;
            for(i = list.length - 1; i >= 0 && out.length < maxLines; i--){
                var j = list[i];
                out.push("#" + String(j.id) + "  " + stateCn(j.state || "queued") + "  "
                         + String(j.desc || "").substring(0, 30) + "  " + wsTimeText(j.at));
            }
            if(out.length === 0){ out.push("（还没有投过作业）"); }
            return out;
        }

        // 「恢复选框」：把框选框重新指到这个工作区的范围上。
        // 故意不给 ids —— jobBoxState() 会因为 total === 0 不画徽标，也不会去轮询空作业。
        function wsRestoreBox(id){
            var ws = wsLoad(String(id));
            if(ws === null){ return "工作区不存在"; }
            var b = ws.bounds || {};
            jobBox = {r: {x1: Number(b.x1), y1: Number(b.y1), x2: Number(b.x2), y2: Number(b.y2)},
                      ws: String(ws.id), ids: [], jobs: {}, pend: 0,
                      started: Packages.java.lang.System.currentTimeMillis(),
                      termAt: 0, lastPoll: 0};
            jobBadge = null;
            wsCurrent = String(ws.id);
            return "已恢复工作区「" + String(ws.name) + "」的选框（右键徽标或「关闭框选框」关闭）";
        }

        function wsRename(id, name){
            var ws = wsLoad(String(id));
            if(ws === null){ return "工作区不存在"; }
            var nm = String(name || "").replace(/[\r\n\t]/g, " ").replace(/^\s+|\s+$/g, "");
            if(nm.length === 0){ return "名字不能是空的"; }
            if(nm.length > 40){ nm = nm.substring(0, 40); }
            ws.name = nm;
            if(wsSave(ws) === null){ return "改名失败：无法写入磁盘"; }
            return "已改名为「" + nm + "」";
        }

        // 导出 / 导入：一个工作区就是一个 JSON 文件，给「换台机器接着干」用。
        function wsExportText(id){
            var ws = wsLoad(String(id));
            if(ws === null){ return ""; }
            try{ return JSON.stringify(ws, null, 2); }catch(e){ return ""; }
        }

        function wsExportToDisk(id){
            var ws = wsLoad(String(id));
            if(ws === null){ return "工作区不存在"; }
            var text = wsExportText(id);
            if(text === ""){ return "导出失败：JSON 序列化失败"; }
            var root = wsRoot();
            if(root === null){ return "导出失败：拿不到数据目录"; }
            try{
                var fi = root.child("export").child(String(ws.id) + ".json");
                if(!fi.parent().exists()){ fi.parent().mkdirs(); }
                fi.writeString(text, false);
                return "已导出到 " + dirText(fi);
            }catch(e){ return "导出失败：" + e; }
        }

        // 导入：校验必需字段，撞 id 就换一个（导入是「复制」，不覆盖现有的工作区）。
        function wsImportText(text){
            var obj = parse(String(text || ""));
            if(obj === null || obj === undefined){ return ""; }
            if(!obj.bounds || !obj.procs){ return ""; }
            var ws = {schema: WS_SCHEMA, mod: MOD_VERSION,
                      id: String(obj.id || ("ws" + wsStamp() + "_" + (++wsSeq))),
                      name: String(obj.name || "导入的工作区").substring(0, 40),
                      state: (String(obj.state) === "archived" ? "archived" : "active"),
                      map: String(obj.map || "?"),
                      world: obj.world || null,
                      bounds: {x1: Number(obj.bounds.x1), y1: Number(obj.bounds.y1),
                               x2: Number(obj.bounds.x2), y2: Number(obj.bounds.y2)},
                      created: Number(obj.created || wsStamp()),
                      updated: wsStamp(),
                      notes: String(obj.notes || "").substring(0, 500),
                      procs: [], jobs: [], archivedAt: Number(obj.archivedAt || 0)};
            var src = obj.procs || [], i;
            for(i = 0; i < src.length && i < 200; i++){
                var r = src[i] || {};
                ws.procs.push({k: String(r.k || ""), x: Number(r.x || 0), y: Number(r.y || 0),
                               block: String(r.block || "?"), desc: String(r.desc || ""),
                               links: r.links || null, code: String(r.code || ""),
                               accepted: Number(r.accepted || 0), history: []});
            }
            if(wsLoad(ws.id) !== null){ ws.id = "ws" + wsStamp() + "_" + (++wsSeq); }
            if(wsSave(ws) === null){ return ""; }
            return ws.id;
        }

        // W3：把「同一块区域上一次的版本」当依据交给 sidecar。
        function wsImprovePayload(ws, b){
            if(ws === null || ws === undefined || b === null || b === undefined){ return null; }
            var row = wsProcOf(ws, b);
            var prevCode = (row === null) ? "" : String(row.code || "");
            if(prevCode.length === 0){ return null; }        // 没写过就没有「上一版」可改进
            var prevJob = "", prevFindings = [], list = ws.jobs || [], i;
            for(i = list.length - 1; i >= 0; i--){
                if(String(list[i].state) === "done" && String(list[i].id).length > 0){
                    prevJob = String(list[i].id);
                    break;
                }
            }
            for(i = 0; i < (row.history || []).length; i++){
                var h = row.history[i] || {};
                if(h.findings && h.findings.length){
                    for(var k = 0; k < h.findings.length && prevFindings.length < 12; k++){
                        prevFindings.push(h.findings[k]);
                    }
                }
            }
            return {id: String(ws.id), name: String(ws.name || ""),
                    notes: String(ws.notes || "").substring(0, 500),
                    prevCode: prevCode, prevJob: prevJob, prevFindings: prevFindings};
        }

        // 「继续改进」：这个工作区里每一台写过代码的处理器，各投一单 mode=improve。
        function wsImproveBatch(id, desc){
            var ws = wsLoad(String(id));
            if(ws === null){ return "工作区不存在"; }
            var list = ws.procs || [], targets = [], i;
            for(i = 0; i < list.length; i++){
                var r = list[i];
                if(String(r.code || "").length === 0){ continue; }
                var b = buildAt(Number(r.x), Number(r.y));
                if(b === null){ continue; }
                targets.push({b: b, x: Number(r.x), y: Number(r.y)});
            }
            if(targets.length === 0){ return "工作区里没有写过代码的处理器，先新建一单"; }
            var text = String(desc || "");
            if(text.replace(/\s/g, "").length === 0){ text = "在上一版的基础上继续改进"; }
            startJobBatch(targets, text, ws.bounds, true);
            return "已投 " + String(targets.length) + " 个改进作业（工作区「" + String(ws.name) + "」）";
        }

        // ------------------------------------------------ 工作区界面 v0.19.0（A+C 改版）
        // 方案：docs/Mlog辅助-工作区快照-界面改版-方案.md
        // 左列表（搜索/过滤/排序 + 卡片行）/ 右详情（处理器表 + 作业时间线 + 动作分层），
        // 世界层再画一层矩形（drawWorkspaceGhosts），选中与「⌖ 定位」联动镜头。
        // 无头环境（Vars.ui === null）不建窗口，纯函数照跑，测试走 api.maiWorkspace3Probe。

        var wsUI = {open: false, d: null, host: null, listHost: null, detailHost: null,
                    sel: null, q: "", filter: "all", sort: "updated",
                    single: false, showDetail: false, ghosts: [],
                    needGeom: false,   // v0.19.5：开窗后第一帧 dump 一次真实宽度（wsGeomDump）
                    listW: 380, detailW: 380};   // v0.19.2：由 openWorkspaceDialog 按屏幕算

        function wsMapName(){
            try{ return String(mapName); }catch(e){ return "?"; }
        }

        // 三态：进行中 / 已归档 / 地图不符（只有能确定当前地图时才判不符）
        function wsStatusOf(ws, mapName){
            var st = (String((ws === null || ws === undefined ? "" : ws.state) || "active") === "active")
                   ? "active" : "archived";
            var cur = String(mapName || "");
            var map = String((ws === null || ws === undefined ? "" : ws.map) || "");
            if(cur.length > 0 && cur !== "?" && map.length > 0 && map !== "?" && map !== cur){
                return "mismatch";
            }
            return st;
        }

        function wsStatusCn(key){
            if(key === "archived"){ return "已归档"; }
            if(key === "mismatch"){ return "地图不符"; }
            return "进行中";
        }

        // 这版 Pal 没有 scarlet（Pal.java:76-88 只有 remove=e55454 / health=ff341c），
        // 危险动作与「地图不符」统一用 Pal.remove，别写 Pal.scarlet。
        function wsStatusColor(key){
            if(key === "archived"){ return Pal.gray; }
            if(key === "mismatch"){ return Pal.remove; }
            return themeColor();
        }

        // wsList() 的 procs/jobs 是**数量**（摘要），界面要全文，所以单独取完整对象。
        function wsAll(){
            var names = wsFileNames(), out = [], i;
            for(i = 0; i < names.length; i++){
                var nm = String(names[i]);
                var obj = wsLoad(nm.substring(0, nm.length - 5));
                if(obj !== null && obj !== undefined){ out.push(obj); }
            }
            return out;
        }

        function wsLastJobText(ws){
            var list = ws.jobs || [], i;
            for(i = list.length - 1; i >= 0; i--){
                var j = list[i];
                if(j === null || j === undefined){ continue; }
                return "#" + String(j.id) + " " + stateCn(String(j.state || "queued"));
            }
            return "还没投过作业";
        }

        function wsRowParts(ws, mapName){
            var key = wsStatusOf(ws, mapName);
            var n = (ws.procs || []).length;
            return {id: String(ws.id), name: String(ws.name || "?"), state: key,
                    colorKey: key, stateCn: wsStatusCn(key),
                    sub: String(n) + " 台 · " + wsTimeText(ws.updated) + " · 最近作业 " + wsLastJobText(ws)};
        }

        function wsCodeLines(code){
            var s = String(code || "");
            if(s.length === 0){ return 0; }
            return s.split("\n").length;
        }

        // 详情模型：处理器最多 12 行、作业最多 6 条（多的折成「还有 N 台」）。
        function wsDetailModel(ws, mapName){
            var key = wsStatusOf(ws, mapName);
            var list = ws.procs || [], procs = [], i;
            for(i = 0; i < list.length && procs.length < 12; i++){
                var row = list[i];
                var n = wsCodeLines(row.code);
                procs.push({pos: "@" + String(row.x) + "," + String(row.y),
                            block: String(row.block || "?"),
                            code: n > 0 ? (String(n) + " 行") : "（空）",
                            desc: String(row.desc || ""),
                            written: Number(row.accepted || 0)});
            }
            var jobs = [], jl = ws.jobs || [];
            for(i = jl.length - 1; i >= 0 && jobs.length < 6; i--){
                var j = jl[i];
                if(j === null || j === undefined){ continue; }
                jobs.push({id: String(j.id), state: String(j.state || "queued"),
                           cn: stateCn(String(j.state || "queued")),
                           desc: String(j.desc || "").substring(0, 40), at: wsTimeText(j.at)});
            }
            var b = ws.bounds || {};
            var w = Number(b.x2) - Number(b.x1) + 1, h = Number(b.y2) - Number(b.y1) + 1;
            return {name: String(ws.name || "?"), state: key, stateCn: wsStatusCn(key),
                    map: String(ws.map || "?"),
                    boundsLine: "地图 " + String(ws.map || "?") + "  (" + String(b.x1) + "," + String(b.y1)
                              + ")-(" + String(b.x2) + "," + String(b.y2) + ")",
                    size: (w > 0 && h > 0) ? (String(w) + "×" + String(h)) : "?",
                    procTotal: list.length, procs: procs,
                    jobTotal: jl.length, jobs: jobs,
                    notes: String(ws.notes || ""),
                    created: wsTimeText(ws.created), updated: wsTimeText(ws.updated),
                    canLocate: (key !== "mismatch")};
        }

        function wsMatchText(ws, q){
            if(String(ws.name || "").toLowerCase().indexOf(q) >= 0){ return true; }
            if(String(ws.notes || "").toLowerCase().indexOf(q) >= 0){ return true; }
            var list = ws.procs || [], i;
            for(i = 0; i < list.length; i++){
                if(String(list[i].desc || "").toLowerCase().indexOf(q) >= 0){ return true; }
                if(String(list[i].block || "").toLowerCase().indexOf(q) >= 0){ return true; }
            }
            return false;
        }

        // opt = {q, filter: all|active|archived|mismatch, map}
        function wsFilterList(rows, opt){
            var q = String((opt && opt.q) || "").toLowerCase();
            var flt = String((opt && opt.filter) || "all");
            var map = String((opt && opt.map) || "");
            var out = [], i;
            for(i = 0; i < (rows || []).length; i++){
                var ws = rows[i];
                var key = wsStatusOf(ws, map);
                if(flt === "active" && key !== "active"){ continue; }
                if(flt === "archived" && key !== "archived"){ continue; }
                if(flt === "mismatch" && key !== "mismatch"){ continue; }
                if(q.length > 0 && !wsMatchText(ws, q)){ continue; }
                out.push(ws);
            }
            return out;
        }

        // mode: updated（最近更新，默认）| name | procs（处理器多在前）
        function wsSortList(rows, mode){
            var arr = [], i;
            for(i = 0; i < (rows || []).length; i++){ arr.push(rows[i]); }
            var m = String(mode || "updated");
            arr.sort(function(a, b){
                if(m === "name"){
                    var x = String(a.name || ""), y = String(b.name || "");
                    return (x < y) ? -1 : ((x > y) ? 1 : 0);
                }
                if(m === "procs"){ return (b.procs || []).length - (a.procs || []).length; }
                return Number(b.updated || 0) - Number(a.updated || 0);
            });
            return arr;
        }

        function wsSortCn(mode){
            if(mode === "name"){ return "名字"; }
            if(mode === "procs"){ return "处理器数"; }
            return "最近更新";
        }

        function wsNextSort(mode){
            if(mode === "updated"){ return "name"; }
            if(mode === "name"){ return "procs"; }
            return "updated";
        }

        function wsReleaseConfirmText(ws){
            var n = (ws.procs || []).length;
            return "工作区「" + String(ws.name || "?") + "」（" + String(n) + " 台处理器）将被释放，\n"
                 + "上下文不再留存，也不能恢复。\n"
                 + "想留着就先用「归档」；只想复用范围，用「恢复选框」。";
        }

        function wsCanLocate(ws, mapName){ return wsStatusOf(ws, mapName) !== "mismatch"; }

        // 镜头定位：世界坐标 = 格子中心 * 8 + 4（先例见 showAiPanel 的 Core.camera.position.set）。
        function wsLocate(ws){
            if(ws === null || ws === undefined){ return "没有这个工作区"; }
            if(!wsCanLocate(ws, wsMapName())){
                return "地图不符（工作区在「" + String(ws.map) + "」），跳不过去";
            }
            var b = ws.bounds || {};
            var cx = (Number(b.x1) + Number(b.x2)) / 2, cy = (Number(b.y1) + Number(b.y2)) / 2;
            try{ Core.camera.position.set(cx * 8 + 4, cy * 8 + 4); }
            catch(e){ return "镜头跳转失败：" + e; }
            return "已定位到 (" + String(Math.round(cx)) + "," + String(Math.round(cy)) + ")";
        }

        // 世界层细框：只画「进行中 + 地图一致」的；坐标口径同 selDrawArgs（左下角 + 宽高，退半格）。
        function wsGhostRects(rows, mapName){
            var out = [], i;
            for(i = 0; i < (rows || []).length; i++){
                var ws = rows[i];
                if(wsStatusOf(ws, mapName) !== "active"){ continue; }
                var b = ws.bounds;
                if(b === null || b === undefined){ continue; }
                out.push({id: String(ws.id), name: String(ws.name || "?"),
                          x1: Number(b.x1), y1: Number(b.y1), x2: Number(b.x2), y2: Number(b.y2)});
            }
            return out;
        }

        function wsT(){ return new Packages.arc.scene.ui.layout.Table(); }

        function wsSelect(id){
            wsUI.sel = String(id);
            if(wsUI.single){
                wsUI.showDetail = true;
                wsRefreshDetail();
                return;
            }
            wsRefreshList();
            wsRefreshDetail();
        }

        function wsRefreshAll(){
            if(wsUI.single && wsUI.showDetail){ wsRefreshDetail(); return; }
            wsRefreshList();
            if(!wsUI.single){ wsRefreshDetail(); }
        }

        // 释放前必须确认一次（规划稿 §4 的要求，v0.14 实现时漏了）。
        function wsAskRelease(ws){
            if(ws === null || ws === undefined){ return; }
            var doIt = function(){
                hint(wsRelease(ws.id));
                wsUI.sel = null;
                wsRefreshAll();
            };
            try{
                if(Vars.ui === null || Vars.ui === undefined){ doIt(); return; }
                Vars.ui.showConfirm("释放工作区", wsReleaseConfirmText(ws), doIt);
            }catch(e){ log("release confirm failed: " + e); doIt(); }
        }

        function renderWorkspaces(host, d){
            if(host === null || host === undefined){ return; }
            managerSeq++;
            if(d !== null && d !== undefined){ wsUI.d = d; }
            wsUI.host = host;
            wsUI.listHost = host;
            if(wsUI.detailHost === null || wsUI.detailHost === undefined){ wsUI.detailHost = host; }
            if(wsUI.single && wsUI.showDetail){ wsRefreshDetail(); return; }
            wsRefreshList();
            if(!wsUI.single){ wsRefreshDetail(); }
        }

        // 面板宽度：openWorkspaceDialog 算好后存进 wsUI；探针 / 无窗时用缺省值。
        function wsListPaneWidth(){
            var w = Number(wsUI.listW);
            if(!(w > 0)){ w = 380; }
            return w;
        }
        function wsDetailPaneWidth(){
            var w = Number(wsUI.detailW);
            if(!(w > 0)){ w = wsListPaneWidth(); }
            return w;
        }
        // v0.19.4（用户 m01690）：按钮宽度一律走这两个纯函数 —— 面板再宽，chips 和主按钮也有上限，
        // 不会被撑成「半栏宽」（之前 chips 是四等分、主按钮是半栏，宽屏下就是被拉伸的样子）。
        // 纯函数的好处：探针能直接断言上限，改布局时不会静默退化。
        function wsChipWidth(iw){
            var v = Math.floor((Number(iw) - 14) / 4);
            if(!(v > 0)){ v = 56; }
            return Math.round(Math.max(56, Math.min(96, v)));
        }
        function wsPrimaryWidth(iw){
            var v = Math.floor((Number(iw) - 8) / 2);
            if(!(v > 0)){ v = 100; }
            return Math.round(Math.max(100, Math.min(168, v)));
        }

        function wsFilterCn(key){
            var k = String(key === null || key === undefined ? "all" : key);
            if(k === "active"){ return "进行中"; }
            if(k === "archived"){ return "已归档"; }
            if(k === "mismatch"){ return "地图不符"; }
            return "全部";
        }

        // v0.19.2（用户截图：字重叠 / 宽度不够 / 按钮折行）：
        // 所有可能超长的文字一律「固定宽度 + ellipsis(true)」，能自适应的按钮（排序 / 底部动作）
        // 不再写死宽度 —— 写死宽度时按钮 label 会折行，固定宽度又没 ellipsis 的文字会画到邻居身上。
        // v0.19.3（截图里搜索框和「排序」按钮叠字）：刷新会在控件回调里被再次触发，
        // 重入时两次 host.clear() 交错，同一个面板里就留下两套控件。这里用 busy 标记挡住重入。
        function wsRefreshList(){
            if(wsUI.busyList === true){ return; }
            wsUI.busyList = true;
            try{ wsRefreshListBody(); }
            catch(e){ log("ws list refresh failed: " + e); }
            wsUI.busyList = false;
        }

        function wsRefreshListBody(){
            var host = wsUI.listHost;
            if(host === null || host === undefined){ return; }
            try{ host.clear(); }catch(e){ return; }
            var map = wsMapName();
            var all = wsAll();
            wsUI.ghosts = wsGhostRects(all, map);
            var rows = wsSortList(wsFilterList(all, {q: wsUI.q, filter: wsUI.filter, map: map}), wsUI.sort);
            var pw = wsListPaneWidth();
            var iw = Math.max(200, pw - 22);
            // v0.19.5（用户 m01884）：搜索框固定 150、排序按钮走自然宽度 —— 原来「搜索框 = 栏宽 - 158」
            // 加上 158 的排序按钮，两行都恰好填满整栏，看起来就是「被拉得很宽、占满一整行」。
            var bar = wsT();
            bar.left();
            try{
                var tf = new Packages.arc.scene.ui.TextField();
                tf.setMessageText("搜索名字/备注");
                tf.setMaxLength(40);
                try{ tf.setText(String(wsUI.q || "")); }catch(e2){ }
                tf.setTextFieldListener(function(field, c){
                    try{ wsUI.q = String(field.getText()); }catch(e3){ wsUI.q = ""; }
                    // v0.19.3：搜索框是随列表一起重建的，重建后要把焦点和光标还回去，
                    // 否则打完一个字输入框就被换掉、后面的字打不进去。
                    wsUI.focusSearch = true;
                    wsRefreshList();
                });
                bar.add(tf).width(150).height(34).padRight(6).left();
                // 重建后恢复焦点（上面 listener 里置的标志）。
                try{
                    if(wsUI.focusSearch === true){
                        wsUI.focusSearch = false;
                        if(Core.scene !== null && Core.scene !== undefined){ Core.scene.setKeyboardFocus(tf); }
                        try{ tf.setCursorPosition(String(wsUI.q || "").length); }catch(ec){ }
                    }
                }catch(ef){ log("ws search focus restore skipped: " + ef); }
            }catch(e){ log("ws search field failed: " + e); }
            bar.button("排序：" + wsSortCn(wsUI.sort), function(){
                wsUI.sort = wsNextSort(wsUI.sort);
                wsRefreshList();
            }).height(34).ellipsis(true).left();
            host.add(bar).growX().left().row();
            // 筛选 chips：四等分 + 省略号；当前筛选写在下面计数行里（省掉「● 」前缀，那个会把文字挤折行）。
            var chips = wsT();
            chips.left();
            // v0.19.6：标签改短（「地图不符」→「不符」+ tooltip），并且不再按 wsChipWidth 截断 ——
            // 原来 maxWidth 卡到 72 时「进行中/已归档/地图不符」全被省略成「进行…/已归…/地图…」。
            // 自然宽度 60+77+76+55 + 间距 = 280，放得进 302 的栏，不用再截。
            var defs = [["all", "全部", ""], ["active", "进行中", ""], ["archived", "已归档", ""],
                        ["mismatch", "不符", "地图不符（工作区记录的地图跟当前地图不一样）"]];
            for(var ci = 0; ci < defs.length; ci++){
                (function(key, cn, tip){
                    var cell = chips.button(cn, function(){
                        wsUI.filter = key;
                        wsRefreshList();
                    }).height(30).padRight(4).left();
                    if(tip !== ""){ try{ cell.tooltip(tip); }catch(et){ } }
                })(defs[ci][0], defs[ci][1], defs[ci][2]);
            }
            host.add(chips).growX().left().row();
            host.add("— " + String(rows.length) + " 个工作区 · 筛选：" + wsFilterCn(wsUI.filter) + " —")
                .color(Pal.gray).width(iw).left().ellipsis(true).row();
            if(rows.length === 0){
                host.add(all.length === 0
                    ? "（框选区域并投递作业后，这里会出现一个工作区）"
                    : "（没有符合条件的，换个词或点「全部」）")
                    .width(iw).left().ellipsis(true).row();
            }
            for(var i = 0; i < rows.length; i++){
                var ws = rows[i];
                var p = wsRowParts(ws, map);
                (function(pr){
                    try{
                        var card = new Packages.arc.scene.ui.Button();
                        // v0.19.6（用户 m02101「标题还是居中的」）：根因是 Arc 的单元格 align 不继承表格 ——
                        // Table.java:1243 `align = c.align;`，cell.align 默认 0 就走 Table.java:1249 的居中分支，
                        // 所以 card.left() 管不住 cell。色条原来没写 .left()，在副行撑宽的第 0 列里居中；
                        // 名字被挤到第 1 列最右边裁掉。现在每个 cell 显式 .left()，副行 colspan(2) 跨两列。
                        card.left().top();
                        var col = wsStatusColor(pr.state);
                        try{ card.image(Tex.whiteui).width(4).height(26).color(col).left(); }catch(e4){ }
                        card.add((String(wsUI.sel) === String(pr.id) ? "▸ " : "") + String(pr.name))
                            .color(col).left().padLeft(6).width(Math.max(80, iw - 34)).ellipsis(true);
                        card.row();
                        card.add(String(pr.sub)).color(Pal.gray).left().padLeft(12).colspan(2)
                            .fontScale(0.85).width(Math.max(80, iw - 40)).ellipsis(true);
                        card.changed(function(){ wsSelect(pr.id); });
                        // 不写死卡片高度：高度跟着两行字走（写死 46 时大字号下两行会压到下一张卡）。
                        host.add(card).growX().padTop(2).padBottom(4).row();
                    }catch(e5){ log("ws card failed: " + e5); }
                })(p);
            }
        }

        function wsRefreshDetail(){
            if(wsUI.busyDetail === true){ return; }
            wsUI.busyDetail = true;
            try{ wsRefreshDetailBody(); }
            catch(e){ log("ws detail refresh failed: " + e); }
            wsUI.busyDetail = false;
        }

        function wsRefreshDetailBody(){
            var dp = wsUI.detailHost;
            if(dp === null || dp === undefined){ return; }
            try{ dp.clear(); }catch(e){ return; }
            var iw = Math.max(200, wsDetailPaneWidth() - 22);
            if(wsUI.single){
                dp.button("← 返回列表", function(){
                    wsUI.showDetail = false;
                    wsRefreshList();
                }).height(32).padBottom(4).left().ellipsis(true).row();
            }
            var ws = (wsUI.sel === null || wsUI.sel === undefined) ? null : wsLoad(String(wsUI.sel));
            if(ws === null){
                dp.add("← 左边选一个工作区").color(Pal.gray).width(iw).left().ellipsis(true).row();
                dp.add("· 恢复选框：把选框重新指到它的范围").color(Pal.gray).fontScale(0.85)
                    .width(iw).left().ellipsis(true).row();
                dp.add("· 继续改进：拿它的代码再投一单").color(Pal.gray).fontScale(0.85)
                    .width(iw).left().ellipsis(true).row();
                dp.add("· 世界里的细框 = 进行中的工作区（选中的会加粗）").color(Pal.gray).fontScale(0.85)
                    .width(iw).left().ellipsis(true).row();
                return;
            }
            var map = wsMapName();
            var m = wsDetailModel(ws, map);
            var col = wsStatusColor(m.state);
            var head = wsT();
            head.left();
            head.add(String(m.name)).color(col).left().width(Math.max(90, iw - 86)).ellipsis(true);
            head.add("  " + String(m.stateCn)).color(col).fontScale(0.85).width(80).ellipsis(true).left();
            dp.add(head).growX().left().row();
            dp.add(String(m.boundsLine) + "   " + String(m.size) + " 格").color(Pal.gray)
                .width(iw).left().ellipsis(true).row();
            var meta = wsT();
            meta.left();
            meta.add("创建 " + String(m.created) + " · 更新 " + String(m.updated))
                .color(Pal.gray).fontScale(0.85).width(Math.max(80, iw - 100)).ellipsis(true).left();
            meta.button("⌖ 定位", function(){ hint(wsLocate(ws)); }).height(28).padLeft(6).ellipsis(true).left();
            dp.add(meta).growX().left().row();
            if(String(m.notes).length > 0){
                dp.add("备注：" + String(m.notes)).color(Pal.gray).width(iw).left().ellipsis(true).row();
            }
            dp.add("── 处理器 " + String(m.procTotal) + " 台 ──").color(Pal.gray)
                .width(iw).left().ellipsis(true).row();
            if(m.procs.length === 0){
                dp.add("（还没有处理器）").width(iw).left().ellipsis(true).row();
            }else{
                // 列宽跟着面板宽度走：位置 / 写入 固定，方块列吃掉剩余宽度（方块名可能很长）。
                var cPos = 92, cCode = 92, cWrite = 60;
                var cBlock = Math.max(80, iw - cPos - cCode - cWrite - 6);
                var hdr = wsT();
                hdr.left();
                hdr.add("位置").width(cPos).color(Pal.gray).ellipsis(true).left();
                hdr.add("方块").width(cBlock).color(Pal.gray).ellipsis(true).left();
                hdr.add("代码").width(cCode).color(Pal.gray).ellipsis(true).left();
                hdr.add("写入").width(cWrite).color(Pal.gray).ellipsis(true).left();
                dp.add(hdr).growX().left().row();
                for(var pi = 0; pi < m.procs.length; pi++){
                    var pr = m.procs[pi];
                    var rowT = wsT();
                    rowT.left();
                    rowT.add(String(pr.pos)).width(cPos).ellipsis(true).left();
                    rowT.add(String(pr.block)).width(cBlock).ellipsis(true).left();
                    rowT.add(String(pr.code)).width(cCode).ellipsis(true).left();
                    rowT.add(String(pr.written) + " 次").width(cWrite).ellipsis(true).left();
                    if(String(pr.desc).length > 0){
                        try{ tipOn(rowT, String(pr.desc)); }catch(e6){ }
                    }
                    dp.add(rowT).growX().left().row();
                }
                if(m.procTotal > m.procs.length){
                    dp.add("… 还有 " + String(m.procTotal - m.procs.length) + " 台").color(Pal.gray)
                        .width(iw).left().ellipsis(true).row();
                }
            }
            dp.add("── 最近作业 " + String(m.jobTotal) + " 条 ──").color(Pal.gray)
                .width(iw).left().ellipsis(true).row();
            if(m.jobs.length === 0){
                dp.add("（还没有投过作业）").width(iw).left().ellipsis(true).row();
            }else{
                var cState = 64, cId = 90, cTime = 86;
                var cDesc = Math.max(60, iw - 8 - 6 - cState - cId - cTime - 6);
                for(var ji = 0; ji < m.jobs.length; ji++){
                    var j = m.jobs[ji];
                    var jr = wsT();
                    jr.left();
                    var jc = (j.state === "done") ? themeColor() : (j.state === "failed" ? Pal.remove : Pal.gray);
                    try{ jr.image(Tex.whiteui).width(8).height(8).color(jc).left().padRight(6); }catch(e7){ }
                    jr.add(String(j.cn)).color(jc).width(cState).ellipsis(true).left();
                    jr.add("#" + String(j.id)).width(cId).ellipsis(true).left();
                    jr.add(String(j.desc)).width(cDesc).ellipsis(true).left();
                    jr.add(String(j.at)).color(Pal.gray).width(cTime).right().ellipsis(true);
                    dp.add(jr).growX().left().row();
                }
            }
            // 动作三层：主（恢复选框 / 继续改进）/ 次（存蓝图 / 归档 / 改名 / 导出）/ 危险（释放，独立一行靠右）
            var a1 = wsT();
            a1.left();
            var bw = wsPrimaryWidth(iw);
            a1.button("恢复选框", function(){ hint(wsRestoreBox(ws.id)); try{ wsUI.d.hide(); }catch(e8){ } })
              .width(bw).height(40).padRight(6).ellipsis(true).left();
            a1.button("继续改进", function(){ hint(wsImproveBatch(ws.id, "在上一版的基础上继续改进")); try{ wsUI.d.hide(); }catch(e9){ } })
              .width(bw).height(40).ellipsis(true).left();
            dp.add(a1).growX().left().row();
            var a2 = wsT();
            a2.left();
            a2.button("存成蓝图", function(){ blueprintSaveBoxAsk(ws); }).height(32).padRight(4).ellipsis(true).left();
            a2.button(String(ws.state) === "archived" ? "取消归档" : "归档", function(){
                hint(String(ws.state) === "archived" ? wsRestore(ws.id) : wsArchive(ws.id));
                wsRefreshAll();
            }).height(32).padRight(4).ellipsis(true).left();
            a2.button("改名", function(){
                if(Vars.ui === null || Vars.ui === undefined){ return; }
                Vars.ui.showTextInput("工作区改名", "名字", 40, String(ws.name || ""), function(nm){
                    hint(wsRename(ws.id, String(nm)));
                    wsRefreshAll();
                });
            }).height(32).padRight(4).ellipsis(true).left();
            a2.button("导出 JSON", function(){ hint(wsExportToDisk(ws.id)); }).height(32).ellipsis(true).left();
            dp.add(a2).growX().left().row();
            var a3 = wsT();
            a3.right();
            a3.button("释放工作区…", function(){ wsAskRelease(ws); }).height(32).ellipsis(true).right();
            dp.add(a3).growX().right().row();
        }

        // 世界层：每帧画所有 active 工作区的细框（只读缓存，不每帧读盘）。
        function drawWorkspaceGhosts(){
            if(!wsUI.open){ return; }
            if(wsUI.listHost === null || wsUI.listHost === undefined){ return; }
            try{
                if(Vars.state === null || Vars.state === undefined){ return; }
            }catch(e){ return; }
            var rects = wsUI.ghosts || [];
            if(rects.length === 0){ return; }
            var L = Packages.arc.graphics.g2d.Lines;
            var base = themeColor();
            for(var i = 0; i < rects.length; i++){
                var g = rects[i];
                var sel = (wsUI.sel !== null && wsUI.sel !== undefined && String(wsUI.sel) === String(g.id));
                var c = base.cpy();
                c.a = sel ? 1 : 0.35;
                L.stroke(sel ? 2 : 1, c);
                L.rect((g.x1 - 0.5) * 8, (g.y1 - 0.5) * 8,
                       (g.x2 - g.x1 + 1) * 8, (g.y2 - g.y1 + 1) * 8);
            }
        }

        // v0.19.5（用户 m01884「MAI 的左栏仍然没变…按钮/输入框还是被拉得很宽」）：真机排版自检。
        // 控件的真实宽度只有 validate（布局）之后才有，所以不能在 wsRefreshListBody 里读 ——
        // openWorkspaceDialog 置 wsUI.needGeom，Trigger.draw 第一帧之后 dump 一次到 last_log.txt。
        // 下一轮排查直接看 `[mai] ws geom:` 的数字，不用再靠截图猜。
        function wsGeomCls(e){
            try{ return String(e.getClass().getSimpleName()); }catch(e1){ return "?"; }
        }
        function wsGeomText(e){
            try{ if(typeof e.getText === "function"){ return String(e.getText()); } }catch(e2){ }
            return "";
        }
        function wsGeomOne(e){
            var w = -1, x = -1, h = -1;
            try{ w = Math.round(Number(e.getWidth())); }catch(e1){ }
            // v0.19.6：Element 没有无参 getX()（只有 getX(int alignment)，Element.java:380），上一版每帧
            // 抛异常被吞掉，dump 出来的 x 恒为 -1。x/y 是 public 字段（Element.java:37），直接读。
            try{ x = Math.round(Number(e.x)); }catch(e2){ }
            try{ h = Math.round(Number(e.getHeight())); }catch(e3){ }
            var s = wsGeomCls(e) + " w=" + String(w) + " x=" + String(x) + " h=" + String(h);
            var t = wsGeomText(e);
            if(t !== ""){ s += " t=" + (t.length > 12 ? t.substring(0, 12) : t); }
            return s;
        }
        function wsGeomDump(){
            if(wsUI.needGeom !== true){ return; }
            var host = wsUI.listHost;
            if(host === null || host === undefined){ return; }
            wsUI.needGeom = false;
            try{
                var dlg = "?";
                try{
                    if(wsUI.d !== null && wsUI.d !== undefined){ dlg = wsGeomOne(wsUI.d); }
                }catch(e0){ }
                // v0.19.6：标题行也 dump 出来（上一版只 dump 左栏，标题居中的问题查不到数字）。
                var titleLine = "?";
                try{
                    if(wsUI.d !== null && wsUI.d !== undefined){
                        var tt = wsUI.d.titleTable;
                        var tk = tt.getChildren();
                        var tl = wsGeomOne(tt);
                        for(var ti = 0; ti < Number(tk.size) && ti < 3; ti++){
                            tl += " [" + wsGeomOne(tk.get(ti)) + "]";
                        }
                        titleLine = tl;
                    }
                }catch(e0t){ }
                var pane = "?";
                try{
                    var par = host.getParent();
                    if(par !== null && par !== undefined){ pane = wsGeomOne(par); }
                }catch(e1){ }
                var parts = [];
                var kids = host.getChildren();
                var n = Number(kids.size);
                for(var i = 0; i < n && i < 24; i++){
                    var e = kids.get(i);
                    var line = "#" + String(i) + " " + wsGeomOne(e);
                    try{
                        var sub = e.getChildren();
                        var sn = Number(sub.size);
                        for(var j = 0; j < sn && j < 6; j++){
                            line += " [" + wsGeomOne(sub.get(j)) + "]";
                        }
                    }catch(e2){ }
                    parts.push(line);
                }
                log("ws geom: dialog(" + dlg + ") title(" + titleLine + ") pane(" + pane + ") host(" + wsGeomOne(host) + ") "
                    + parts.join(" | "));
            }catch(e3){ log("ws geom failed: " + e3); }
        }

        // W4：蓝图列表。无头环境 Vars.schematics 是 null，必须能吃 null 回空数组。
        function blueprintList(maxRows){
            var out = [];
            try{
                if(Vars.schematics === null || Vars.schematics === undefined){ return out; }
                var all = Vars.schematics.all();
                if(all === null || all === undefined){ return out; }
                var n = 0;
                try{ n = (typeof all.size === "function") ? Number(all.size()) : Number(all.size); }catch(e){ n = 0; }
                for(var i = 0; i < n && out.length < (maxRows || 24); i++){
                    var s = null;
                    try{ s = all.get(i); }catch(e){ s = null; }
                    if(s === null || s === undefined){ continue; }
                    var nm = "?";
                    try{ nm = String(s.name()); }catch(e){ nm = String(s.name); }
                    out.push({name: nm, width: Number(s.width || 0), height: Number(s.height || 0)});
                }
            }catch(e){ log("blueprint list failed: " + e); }
            return out;
        }

        // B1（v0.16.0）：把一块矩形存成原版蓝图，全部走原版接口。
        // create(x,y,x2,y2) 复制方块（LogicBlock 的 config 一并带上，所以处理器代码随蓝图走）
        // -> 往 tags 里填 name（不填会存成 unknown.msch）-> add()（按名字找文件、写盘、进列表）。
        // add() 内部出异常只 ui.showException 不抛，所以写完必须回读 all() 校验，不能信返回值。
        function blueprintHasName(name){
            var list = blueprintList(400), i;
            for(i = 0; i < list.length; i++){
                if(String(list[i].name) === String(name)){ return true; }
            }
            return false;
        }

        function blueprintSaveBox(r, name, cb){
            var done = function(msg){
                if(cb !== null && cb !== undefined){ cb(msg); } else { hint(msg); }
                return msg;
            };
            if(Vars.schematics === null || Vars.schematics === undefined){ return done("蓝图接口不可用"); }
            if(r === null || r === undefined){ return done("没有可保存的框选"); }
            var nm = trimText(String(name === null || name === undefined ? "" : name));
            if(nm.length === 0){ nm = "MAI " + String(Number(r.x1)) + "," + String(Number(r.y1)); }
            var s = null;
            try{ s = Vars.schematics.create(Number(r.x1), Number(r.y1), Number(r.x2), Number(r.y2)); }
            catch(e){ log("blueprint create failed: " + e); return done("存蓝图失败：原版接口报错（" + e + "）"); }
            if(s === null || s === undefined){ return done("存蓝图失败：原版没有返回蓝图"); }
            var tiles = 0;
            try{ tiles = seqSize(s.tiles); }catch(e0){ tiles = 0; }
            if(tiles <= 0){ return done("这块区域是空的，没有可保存的方块"); }
            try{ s.tags.put("name", nm); }catch(e1){ log("blueprint name failed: " + e1); }
            try{ s.tags.put("description", "Mlog辅助 工作区 · " + nm + " · v" + MOD_VERSION); }catch(e2){ }
            var before = blueprintList(400).length;
            try{ Vars.schematics.add(s); }
            catch(e3){ log("blueprint add failed: " + e3); return done("存蓝图失败：写入时报错（" + e3 + "）"); }
            var after = blueprintList(400).length;
            if(after <= before && !blueprintHasName(nm)){
                return done("存蓝图失败：蓝图列表里没出现（原版把错误显示在控制台了）");
            }
            return done("已存成蓝图「" + nm + "」：方块 " + String(tiles) + " 个，原版蓝图页可见");
        }

        // 入口：有工作区就用它的 bounds，否则用当前框选框。
        function blueprintSaveBoxAsk(ws){
            var r = null;
            if(ws !== null && ws !== undefined && ws.bounds !== null && ws.bounds !== undefined){ r = ws.bounds; }
            else if(jobBox !== null && jobBox.r !== null && jobBox.r !== undefined){ r = jobBox.r; }
            if(r === null){ hint("没有可保存的框选"); return "no rect"; }
            if(Vars.schematics === null || Vars.schematics === undefined){ hint("蓝图接口不可用"); return "no schematics"; }
            var def = (ws !== null && ws !== undefined)
                ? String(ws.name)
                : ("MAI " + String(Number(r.x1)) + "," + String(Number(r.y1)));
            if(Vars.ui === null || Vars.ui === undefined){ return blueprintSaveBox(r, def, null); }
            Vars.ui.showTextInput("存成蓝图", "蓝图名（写进原版蓝图列表）", 64, def, function(name){
                blueprintSaveBox(r, name, null);
            });
            return "asked";
        }

        // ---------------------------------------------------------- B2/B4（v0.16.1）
        // 蓝图页底栏加一个「MAI 控制面板」，原版「蓝图 → 详细」页里也挂一个「MAI」。
        // 上下文从处理器代码的签名 payload v2 里读回来：token 尾部是
        // <flag><base64url>，flag 为 z（zlib 压过的 JSON）或 j（原文 JSON）。
        var BP_TITLE = "";
        var BP_BTN_ADDED = false;
        var BP_CHAT = {name: "", code: "", meta: null, jobId: null, jobState: "", newCode: "",
                       lastWhy: "", pollAt: 0, host: null, dialog: null};

        // 原版「详细」页标题形如 "[[schematic] 蓝图名"（arc 用 [[ 转义一个 [），取最后一段。
        function blueprintTitleName(txt){
            var s = String(txt === null || txt === undefined ? "" : txt);
            var i = s.lastIndexOf("] ");
            if(i >= 0){ s = s.substring(i + 2); }
            return trimText(s);
        }

        // v0.17.1（用户 m09822 / 4）：框选之后先看框里的处理器有没有「签名挂载」的上下文
    // （payload v2 的 m 段：工作区 id / 名字 / 当时的描述 / 作业 id），有就拿它当这块框选的上下文，
    // 没有才回落到「按选框中心找 active 工作区」的老路子。
    function sigCtxInRegion(targets){
        var list = targets || [], i;
        for(i = 0; i < list.length; i++){
            var b = list[i].b;
            if(b === null || b === undefined){ continue; }
            var code = "";
            try{ code = String(b.code || ""); }catch(e0){ code = ""; }
            if(code.length === 0 || code.indexOf("aigenerated:") < 0){ continue; }
            var meta = null;
            try{ meta = sigMetaOf(code); }catch(e1){ meta = null; }
            if(meta === null){ continue; }
            if(String(meta.ws).length === 0 && String(meta.name).length === 0
               && String(meta.prompt).length === 0){
                continue;
            }
            return {meta: meta, code: code, b: b, x: list[i].x, y: list[i].y};
        }
        return null;
    }

    // 签名里记着工作区 id、但磁盘上那份已经不在了（删了 / 换了台机器）：
    // 用它重建一个临时工作区，好让「改进上一版」还能拿到 prevCode。
    function wsFromSig(r, sig){
        if(sig === null || sig === undefined){ return null; }
        var meta = sig.meta;
        var nm = String(meta.name).length > 0 ? String(meta.name) : "签名挂载的上下文";
        var ws = wsNew(r, nm);
        if(String(meta.ws).length > 0){
            ws.id = String(meta.ws);
            wsCurrent = String(ws.id);
        }else{
            ws.id = "sig" + String(wsStamp()) + "_" + String(wsSeq);
            wsCurrent = String(ws.id);
        }
        ws.notes = String(meta.prompt || "");
        ws.sig = {job: String(meta.job || "")};
        var row = wsTouchProc(ws, sig.b, ws.notes);
        if(row !== null){ row.code = String(sig.code); }
        wsSave(ws);
        return ws;
    }

    function wsAttachCtx(ws, r, targets, desc){
        if(ws === null || ws === undefined){ return wsAttach(r, targets, desc); }
        var b = ws.bounds || {};
        if(b.x1 === undefined || b.x1 === null){
            ws.bounds = {x1: Number(r.x1), y1: Number(r.y1), x2: Number(r.x2), y2: Number(r.y2)};
        }else{
            ws.bounds = {x1: Math.min(Number(b.x1), Number(r.x1)),
                         y1: Math.min(Number(b.y1), Number(r.y1)),
                         x2: Math.max(Number(b.x2), Number(r.x2)),
                         y2: Math.max(Number(b.y2), Number(r.y2))};
        }
        for(var i = 0; i < (targets || []).length; i++){ wsTouchProc(ws, targets[i].b, desc); }
        wsSave(ws);
        wsCurrent = String(ws.id);
        return ws;
    }

    // zlib 解压（带 flag 字节的 base64url 已经剥掉）。
        // 单独一个函数：dn 一个函数里塞 try/catch + while 时 Rhino 的 codegen 会报
        // "invalid bytecode"，拆开就没事（真机 v160.7 上验过）。
        function sigInflate(raw){
            var inf = new Packages.java.util.zip.InflaterInputStream(
                new Packages.java.io.ByteArrayInputStream(raw, 1, raw.length - 1));
            var bos = new Packages.java.io.ByteArrayOutputStream();
            var buf = Packages.java.lang.reflect.Array.newInstance(Packages.java.lang.Byte.TYPE, 1024);
            var n = inf.read(buf);
            var guard = 0;
            while(n > 0 && guard < 4096){
                bos.write(buf, 0, n);
                n = inf.read(buf);
                guard++;
            }
            return String(bos.toString("UTF-8"));
        }

        // 从签名 token 里解 meta（只有 payload v2 有），解不出来就回 null。
        // 这里逐字符扫描而不用正则：字符类里带半角引号（jump 后面那个 "）会让真机
        // Rhino 的 codegen 报 "invalid bytecode"，v160.7 上实测必现。
        function sigMetaOf(code){
            var txt = String(code === null || code === undefined ? "" : code);
            var head = "aigenerated:";
            var at = txt.indexOf(head);
            if(at < 0){ return null; }
            var p = at + head.length;
            var author = "";
            while(p < txt.length && txt.charAt(p) !== ":" && txt.charAt(p) !== "\""){
                author += txt.charAt(p);
                p++;
            }
            if(p >= txt.length || txt.charAt(p) !== ":"){ return null; }
            p++;
            var hex = txt.substring(p, p + 12);
            if(hex.length < 12){ return null; }
            p += 12;
            if(p >= txt.length || txt.charAt(p) !== ":"){ return null; }
            p++;
            var enc = "";
            while(p < txt.length){
                var c = txt.charAt(p);
                var okChar = (c >= "A" && c <= "Z") || (c >= "a" && c <= "z")
                    || (c >= "0" && c <= "9") || c === "-" || c === "_";
                if(!okChar){ break; }
                enc += c;
                p++;
            }
            if(enc.length < 2){ return null; }
            var raw = null;
            try{ raw = Packages.java.util.Base64.getUrlDecoder().decode(enc); }
            catch(e0){ raw = null; }
            if(raw === null || raw.length < 2){ return null; }
            var flag = String.fromCharCode(Number(raw[0]) & 255);
            var body = "";
            if(flag === "z"){
                try{ body = sigInflate(raw); }
                catch(e1){ log("sig inflate failed: " + e1); return null; }
            }else{
                try{ body = String(new Packages.java.lang.String(raw, 1, raw.length - 1, "UTF-8")); }
                catch(e2){ body = ""; }
            }
            if(body.length === 0){ return null; }
            var doc = null;
            try{ doc = JSON.parse(body); }
            catch(e3){ doc = null; }
            if(doc === null || doc === undefined){ return null; }
            if(doc instanceof Array){ return null; }
            if(Number(doc.v) !== 2){ return null; }
            var mm = doc.m;
            if(mm === null || mm === undefined){ return null; }
            var meta = {ws: "", name: "", prompt: "", job: ""};
            if(mm.ws !== null && mm.ws !== undefined){ meta.ws = String(mm.ws); }
            if(mm.name !== null && mm.name !== undefined){ meta.name = String(mm.name); }
            if(mm.prompt !== null && mm.prompt !== undefined){ meta.prompt = String(mm.prompt); }
            if(mm.job !== null && mm.job !== undefined){ meta.job = String(mm.job); }
            return meta;
        }

        // 在蓝图库里按名字找一张图，并把它里面第一个「带签名的处理器代码」连同上下文取出来。
        function blueprintCtxOf(name){
            var out = null;
            try{
                if(Vars.schematics === null || Vars.schematics === undefined){ return null; }
                var all = Vars.schematics.all();
                if(all === null || all === undefined){ return null; }
                var n = 0;
                try{ n = seqSize(all); }catch(e0){ n = 0; }
                for(var i = 0; i < n; i++){
                    var sc = null;
                    try{ sc = all.get(i); }catch(e1){ sc = null; }
                    if(sc === null || sc === undefined){ continue; }
                    var nm = "";
                    try{ nm = String(sc.name()); }catch(e2){ nm = String(sc.name); }
                    if(nm !== String(name)){ continue; }
                    var code = "", meta = null, tn = 0;
                    try{ tn = seqSize(sc.tiles); }catch(e3){ tn = 0; }
                    for(var k = 0; k < tn; k++){
                        var tl = null;
                        try{ tl = sc.tiles.get(k); }catch(e4){ tl = null; }
                        if(tl === null || tl === undefined || tl.config === null || tl.config === undefined){ continue; }
                        var txt = String(tl.config);
                        if(txt.indexOf("aigenerated:") < 0){ continue; }
                        code = txt;
                        meta = sigMetaOf(txt);
                        break;
                    }
                    out = {name: nm, code: code, meta: meta, schematic: sc,
                           width: Number(sc.width || 0), height: Number(sc.height || 0)};
                    break;
                }
            }catch(e5){ log("blueprint ctx failed: " + e5); }
            return out;
        }

        function bpMetaLine(meta){
            if(meta === null){ return "这个蓝图里的处理器代码没有工作区上下文（v0.16.0 之前生成的代码没有）。"; }
            var s = "工作区：" + (String(meta.ws).length > 0 ? String(meta.ws) : "-")
                  + " · " + (String(meta.name).length > 0 ? String(meta.name) : "-");
            if(String(meta.prompt).length > 0){ s += "\n当时的描述：" + String(meta.prompt); }
            if(String(meta.job).length > 0){ s += "\n作业：" + String(meta.job); }
            return s;
        }

        function renderBlueprintChat(){
            var host = BP_CHAT.host;
            if(host === null || host === undefined){ return; }
            try{ host.clear(); }catch(e){ return; }
            host.add("— 上下文 —").row();
            host.add(bpMetaLine(BP_CHAT.meta)).row();
            var prev = String(BP_CHAT.code || "");
            var lines = prev.split("\n");
            host.add("— 上一版代码（" + String(lines.length) + " 行）—").row();
            for(var i = 0; i < lines.length && i < 20; i++){ host.add("    " + lines[i]).row(); }
            if(lines.length > 20){ host.add("    …").row(); }
            if(BP_CHAT.jobId !== null){
                host.add("— 最近一次：作业 " + String(BP_CHAT.jobId) + "（" + String(BP_CHAT.jobState) + "）—").row();
                if(String(BP_CHAT.lastWhy).length > 0){ host.add("为什么：" + String(BP_CHAT.lastWhy)).row(); }
                var nc = String(BP_CHAT.newCode || "");
                if(nc.length > 0){
                    var nl = nc.split("\n");
                    for(var j = 0; j < nl.length; j++){ host.add("    " + nl[j]).row(); }
                }
            }
        }

        function blueprintChatSubmit(desc){
            var text = trimText(String(desc === null || desc === undefined ? "" : desc));
            if(text.length === 0){ hint("先写一句要改什么"); return "empty"; }
            ensureSidecar(true);
            if(SIDE.state === "starting"){ hint("sidecar 正在启动（首次需几秒），稍后再投"); return "starting"; }
            if(llmState.kind !== "llm" && llmState.kind !== "fake"){
                if(llmUrl().length === 0 || llmModel().length === 0){
                    hint("未配置模型：先在「设置 → Mlog辅助 → 大模型」填地址、密钥、模型");
                    return "no model";
                }
                llmAutoSync();
            }
            var meta = BP_CHAT.meta;
            var ws = {
                id: (meta !== null && String(meta.ws).length > 0) ? String(meta.ws) : ("bp-" + String(BP_CHAT.name)),
                name: (meta !== null && String(meta.name).length > 0) ? String(meta.name) : String(BP_CHAT.name),
                notes: "蓝图续聊",
                prevCode: String(BP_CHAT.code),
                prevJob: (meta !== null) ? String(meta.job) : "",
                prevFindings: []
            };
            var body = {mode: "improve", desc: text, links: null, ipt: 2, origin: null,
                        snapshot: {}, workspace: ws};
            httpJson("POST", "/jobs", JSON.stringify(body), function(code, resp){
                var res = parse(resp);
                if(code !== 200 || res === null || res.ok !== true){
                    BP_CHAT.lastWhy = jobWhy(code, resp, res);
                    hint("投递失败：" + String(BP_CHAT.lastWhy));
                    renderBlueprintChat();
                    return;
                }
                BP_CHAT.jobId = String(res.job.id);
                BP_CHAT.jobState = String(res.job.state);
                BP_CHAT.newCode = "";
                BP_CHAT.lastWhy = "";
                BP_CHAT.pollAt = 0;
                renderBlueprintChat();
            });
            return "posted";
        }

        function blueprintChatAsk(){
            if(Vars.ui === null || Vars.ui === undefined){ return "no ui"; }
            if(String(BP_CHAT.code).length === 0){ hint("这个蓝图里没有可用的处理器代码，没法接着聊"); return "no code"; }
            Vars.ui.showTextInput("继续对话", "接着上一版要改什么？", 300, "", function(txt){
                blueprintChatSubmit(String(txt));
            });
            return "asked";
        }

        function blueprintChatPoll(){
            if(BP_CHAT.jobId === null || BP_CHAT.host === null){ return; }
            var now = Date.now();
            if(now < BP_CHAT.pollAt){ return; }
            BP_CHAT.pollAt = now + 900;
            jobGet(String(BP_CHAT.jobId), function(job, err){
                if(job === null){
                    BP_CHAT.lastWhy = String(err === null || err === undefined ? "" : err);
                    renderBlueprintChat();
                    return;
                }
                var st = String(job.state), nc = String(job.code === null || job.code === undefined ? "" : job.code);
                var changed = (st !== String(BP_CHAT.jobState)) || (nc !== String(BP_CHAT.newCode));
                BP_CHAT.jobState = st;
                BP_CHAT.newCode = nc;
                if(st === "failed"){ BP_CHAT.lastWhy = jobWhyLines(job, 2).join(" / "); }
                if(changed){ renderBlueprintChat(); }
            });
        }

        function blueprintWriteBack(){
            var code = String(BP_CHAT.newCode || "");
            if(code.length === 0){ hint("还没有可以写回的代码（先继续对话，等作业完成）"); return "no code"; }
            var ctx = blueprintCtxOf(BP_CHAT.name);
            if(ctx === null){ hint("找不到蓝图「" + String(BP_CHAT.name) + "」"); return "no blueprint"; }
            var sch = ctx.schematic;
            var file = null;
            try{ file = sch.file; }catch(e0){ file = null; }
            if(file === null || file === undefined){ hint("这个蓝图还没落盘，写不回去"); return "no file"; }
            var tn = 0, i, wrote = false;
            try{ tn = seqSize(sch.tiles); }catch(e1){ tn = 0; }
            for(i = 0; i < tn; i++){
                var tl = null;
                try{ tl = sch.tiles.get(i); }catch(e2){ tl = null; }
                if(tl === null || tl === undefined || tl.config === null || tl.config === undefined){ continue; }
                if(String(tl.config).indexOf("aigenerated:") < 0){ continue; }
                try{ tl.config = String(code); wrote = true; }
                catch(e3){ log("write back failed: " + e3); }
                break;
            }
            if(!wrote){ hint("蓝图里没找到可写回的处理器"); return "no logic"; }
            try{ Vars.schematics.saveChanges(sch); }
            catch(e4){ log("saveChanges failed: " + e4); return "save failed"; }
            var after = blueprintCtxOf(BP_CHAT.name);
            if(after === null || String(after.code) !== String(code)){
                hint("写回后回读对不上（原版可能没写盘）");
                return "verify failed";
            }
            BP_CHAT.code = String(code);
            BP_CHAT.meta = after.meta;
            hint("已写回蓝图「" + String(BP_CHAT.name) + "」");
            renderBlueprintChat();
            return "ok";
        }

        function openBlueprintChat(name){
            if(Vars.ui === null || Vars.ui === undefined){ return "no ui"; }
            var ctx = blueprintCtxOf(name);
            if(ctx === null){ hint("找不到蓝图「" + String(name) + "」"); return "no blueprint"; }
            var d = new Packages.arc.scene.ui.Dialog("Mlog辅助 · 蓝图上下文 · " + String(ctx.name));
            d.cont.defaults().pad(4).left();
            var host = new Packages.arc.scene.ui.layout.Table();
            try{ d.cont.pane(host).height(360).width(640).row(); }
            catch(e){ d.cont.add(host).row(); }
            BP_CHAT.name = String(ctx.name);
            BP_CHAT.code = String(ctx.code === null || ctx.code === undefined ? "" : ctx.code);
            BP_CHAT.meta = ctx.meta;
            BP_CHAT.jobId = null;
            BP_CHAT.jobState = "";
            BP_CHAT.newCode = "";
            BP_CHAT.lastWhy = "";
            BP_CHAT.pollAt = 0;
            BP_CHAT.host = host;
            BP_CHAT.dialog = d;
            renderBlueprintChat();
            d.buttons.button("继续对话", function(){ blueprintChatAsk(); }).width(120);
            d.buttons.button("写回蓝图", function(){ blueprintWriteBack(); }).width(120);
            d.buttons.button("复制上下文", function(){
                var t = bpMetaLine(BP_CHAT.meta);
                try{
                    Core.app.setClipboardText(t);
                    hint("上下文已复制");
                }catch(e){ log("clipboard failed: " + e); }
            }).width(130);
            d.buttons.button("关闭", function(){ d.hide(); BP_CHAT.host = null; BP_CHAT.dialog = null; }).width(100);
            d.show();
            return "opened";
        }

        function openBlueprintPicker(){
            if(Vars.ui === null || Vars.ui === undefined){ return "no ui"; }
            var d = new Packages.arc.scene.ui.Dialog("Mlog辅助 · 蓝图对话 v" + MOD_VERSION);
            d.cont.defaults().pad(4).left();
            var host = new Packages.arc.scene.ui.layout.Table();
            try{ d.cont.pane(host).height(340).width(620).row(); }
            catch(e){ d.cont.add(host).row(); }
            var rows = blueprintList(48), i;
            host.add("— 蓝图（" + String(rows.length) + " 个）—").row();
            if(rows.length === 0){ host.add("（这个环境读不到蓝图库）").row(); }
            for(i = 0; i < rows.length; i++){
                (function(nm){
                    var ctx = blueprintCtxOf(nm);
                    var tail = (ctx !== null && ctx.meta !== null) ? "  · 带上下文" : "";
                    host.button(String(nm) + tail, function(){
                        d.hide();
                        openBlueprintChat(nm);
                    }).width(560).row();
                })(rows[i].name);
            }
            d.buttons.button("关闭", function(){ d.hide(); }).width(110);
            d.show();
            return "opened";
        }

        // 蓝图页底栏加按钮：Vars.ui.schematics 建好之后挂一次就够（buttons 只建一次）。
        function blueprintPageHook(){
            if(BP_BTN_ADDED){ return; }
            if(Vars.ui === null || Vars.ui === undefined) { return; }
            if(Vars.ui.schematics === null || Vars.ui.schematics === undefined){ return; }
            var btns = null;
            try{ btns = Vars.ui.schematics.buttons; }catch(e){ btns = null; }
            if(btns === null || btns === undefined){ return; }
            try{
                // v0.18.3（用户 m11124）：「MAI 控制面板按钮是用来管理工作区的，替代以前的查看页面」。
                btns.button("MAI 控制面板", function(){ openWorkspaceDialog(); }).width(150);
                BP_BTN_ADDED = true;
            }catch(e2){ log("blueprint page button failed: " + e2); }
        }

        // 原版「详细」页是私有字段 SchematicsDialog.info 里的 SchematicInfoDialog；
        // 每次 show() 都会 cont.clear() + buttons.clearChildren()，所以按标题变化重挂。
        function schematicInfoDialog(){
            try{
                var cls = Packages.java.lang.Class.forName("mindustry.ui.dialogs.SchematicsDialog");
                var f = cls.getDeclaredField("info");
                f.setAccessible(true);
                return f.get(Vars.ui.schematics);
            }catch(e){ return null; }
        }

        function blueprintInfoHook(){
            if(Vars.ui === null || Vars.ui === undefined){ return; }
            if(Vars.ui.schematics === null || Vars.ui.schematics === undefined){ return; }
            var info = schematicInfoDialog();
            if(info === null || info === undefined){ return; }
            var shown = false;
            try{ shown = !!info.isShown(); }catch(e0){ shown = false; }
            if(!shown){ BP_TITLE = ""; return; }
            var txt = "";
            try{ txt = String(info.title.getText()); }catch(e1){ txt = ""; }
            // 标题没变也要查一次：原版每次 show() 都 buttons.clearChildren()，
            // 同一张图连开两次时标题一样，但按钮已经被清掉了。
            var kids = 0;
            try{ kids = Number(seqSize(info.buttons.getChildren())); }catch(e2){ kids = 0; }
            if(txt === BP_TITLE && kids > 0){ return; }
            BP_TITLE = txt;
            var nm = blueprintTitleName(txt);
            if(nm.length === 0){ return; }
            try{
                // v0.17.1（用户 m09822 / 3）：按钮就一个「MAI」，和返回 / 导出 / 编辑并排，
                // 点开是这个蓝图的上下文管理（看上下文、继续对话、写回、复制）。
                info.buttons.button("MAI", function(){ openBlueprintChat(nm); }).width(110);
            }catch(e3){ log("info page button failed: " + e3); }

        }

        function blueprintTick(){
            try{ blueprintPageHook(); }catch(e){ }
            try{ blueprintInfoHook(); }catch(e2){ }
            try{ blueprintChatPoll(); }catch(e3){ }
        }

        // 从蓝图建一个空工作区：范围按玩家脚下的方块摆（无头没玩家就摆在地图中心）。
        function wsFromBlueprintName(name){
            var rows = blueprintList(64), i, sch = null;
            for(i = 0; i < rows.length; i++){
                if(String(rows[i].name) === String(name)){ sch = rows[i]; break; }
            }
            if(sch === null){ return "找不到蓝图「" + String(name) + "」"; }
            var cx = 0, cy = 0;
            try{
                if(Vars.player !== null && Vars.player !== undefined && Vars.player.tileOn){
                    cx = Vars.player.tileOn().x; cy = Vars.player.tileOn().y;
                }else{
                    cx = Math.floor(Vars.world.width() / 2); cy = Math.floor(Vars.world.height() / 2);
                }
            }catch(e){ cx = 0; cy = 0; }
            var w = Math.max(1, Number(sch.width || 1)), h = Math.max(1, Number(sch.height || 1));
            var r = {x1: cx, y1: cy, x2: cx + w - 1, y2: cy + h - 1};
            var ws = wsNew(r, String(sch.name));
            if(ws === null){ return "创建工作区失败：无法写入磁盘"; }
            ws.notes = "来自蓝图「" + String(sch.name) + "」(" + String(w) + "×" + String(h) + ")";
            wsSave(ws);
            wsCurrent = String(ws.id);
            return "已从蓝图「" + String(sch.name) + "」创建工作区（" + String(w) + "×" + String(h) + "）";
        }

        function renderBlueprintPicker(host, d){
            managerSeq++;
            if(host === null || host === undefined){ return; }
            try{ host.clear(); }catch(e){ return; }
            var iw = Math.max(200, wsListPaneWidth() - 22);
            var rows = blueprintList(24);
            host.add("— 从蓝图创建工作区（" + String(rows.length) + " 个）—")
                .width(iw).left().ellipsis(true).row();
            if(rows.length === 0){
                host.add("（没有蓝图，或者这个环境里读不到蓝图库）").width(iw).left().ellipsis(true).row();
            }
            for(var i = 0; i < rows.length; i++){
                (function(nm){
                    host.button(String(nm), function(){
                        hint(wsFromBlueprintName(nm));
                        renderWorkspaces(host, d);
                    }).width(iw).height(30).padRight(4).ellipsis(true).row();
                })(rows[i].name);
            }
            host.button("返回列表", function(){ renderWorkspaces(host, d); }).height(32).padTop(4).ellipsis(true).row();
        }

        // v0.19.3（用户 m01237：按钮太宽、标题被挤到右边遮挡）：
        // Core.graphics.getWidth()/getHeight() 给的是**像素**，而 Arc 里 Table/Cell 的尺寸单位是
        // scene 单位 —— 官方到处都先除一次 UI 缩放再用：
        //   Mindustry-Source\core\src\mindustry\ui\dialogs\JoinDialog.java:647
        //     Math.min(Core.graphics.getWidth() / Scl.scl() * 0.9f, 550f)
        // 同类：ModsDialog.java:114/117、MapLocalesDialog.java:210、MapListDialog.java:116。
        // v0.19.2 直接把像素当格子宽度 ⇒ 对话框比窗口宽 Scl 倍 ⇒ 右栏被推出屏幕、按钮看着特别长。
        // 这里把「像素 → scene 单位」抽成纯函数，探针可以直接断言。
        // 一像素等于几个 scene 单位：优先用「场景宽度比」（main.js:3889 的 worldToScreen 就是这么算的），
        // 拿不到场景再退回 Scl.scl()，都没有就按 1:1 处理。
        function wsSclFactor(){
            try{
                var gw = Number(Core.graphics.getWidth()), sw = Number(Core.scene.getWidth());
                if(gw > 0 && sw > 0){ return gw / sw; }
            }catch(e){ }
            try{
                var f = Number(Packages.arc.scene.ui.layout.Scl.scl());
                if(f > 0){ return f; }
            }catch(e2){ }
            return 1;
        }
        function wsSceneUnits(px, scl){
            var v = Number(px), f = Number(scl);
            if(!(f > 0)){ f = 1; }
            if(!(v > 0)){ return 0; }
            return Math.round(v / f);
        }
        // 当前屏幕有多大（scene 单位）。Core.scene 的宽高本来就是 scene 单位，能拿到就直接用。
        function wsScreenSize(){
            var w = 0, h = 0;
            try{ w = Number(Core.scene.getWidth()); h = Number(Core.scene.getHeight()); }catch(e){ w = 0; h = 0; }
            var f = wsSclFactor();
            if(!(w > 0)){
                try{ w = wsSceneUnits(Number(Core.graphics.getWidth()), f); }catch(e2){ w = 0; }
            }
            if(!(h > 0)){
                try{ h = wsSceneUnits(Number(Core.graphics.getHeight()), f); }catch(e3){ h = 0; }
            }
            if(!(w > 0)){ w = 1280; }
            if(!(h > 0)){ h = 720; }
            return {w: Math.round(w), h: Math.round(h), scl: f};
        }

        // v0.19.2：把「按屏幕算尺寸」抽成纯函数，探针可以直接断言（无头环境也能跑）。
        // 入参是 **scene 单位**（不是像素，见上面 wsSclFactor 的注释）。
        function wsDialogPlan(sw, sh){
            var w = Number(sw), h = Number(sh);
            if(!(w > 0)){ w = 1280; }
            if(!(h > 0)){ h = 720; }
            // 左右留 28+28、上下留 100+90：对话框边框 + 标题栏 + 底部按钮栏都要吃掉余量。
            // v0.19.4（用户 m01690「标题居中导致按钮被拉伸」）：不再顶满屏幕 —— 上限 1080 时
            // 两栏是 356 / 710，详情栏的按钮有确定宽度（wsPrimaryWidth），不会被撑成半栏。
            var availW = Math.round(Math.max(520, Math.min(1080, w - 56)));
            var availH = Math.round(Math.max(320, Math.min(760, h - 190)));
            // v0.19.5（用户 m01884「MAI 的左栏仍然没变…按钮/输入框还是被拉得很宽」）：
            // 左栏是列表，不需要 1/3 那么宽 —— 收窄到 30%（1080 时 324），控件不再铺满整行。
            var listW = Math.round(Math.min(360, Math.max(300, availW * 0.30)));
            var detailW = Math.round(Math.max(300, availW - listW - 14));
            var onlyW = Math.round(Math.min(availW, 560));
            return {wide: w >= 900, availW: availW, availH: availH,
                    listW: listW, detailW: detailW, onlyW: onlyW};
        }

        function openWorkspaceDialog(){
            if(Vars.ui === null || Vars.ui === undefined){ return "no ui"; }
            var d = new Packages.arc.scene.ui.Dialog("Mlog辅助 · 工作区快照 v" + MOD_VERSION);
            d.cont.defaults().pad(3).left();
            try{ d.cont.left().top(); }catch(e00){ }
            // v0.19.4（用户 m01690「你把工作区的标题居中了」）：标题明确左对齐。
            // Arc 的 Dialog 标题行是 add(titleTable).growX()，Label 默认 left 但一旦被居中，
            // 整行会按标题宽度撑开，下面的按钮行（fillX）跟着变宽 —— 直接钉死左对齐 + 底部按钮靠左。
            try{ d.title.setAlignment(Packages.arc.util.Align.left); }catch(e0){ }
            // v0.19.6（用户 m02101「标题还是居中的」）：Dialog 的标题是 add(titleTable).growX()（Dialog.java:79），
            // titleTable 里又是 add(title).expandX().fillX()（Dialog.java:78）—— Label 被撑满整行时，
            // 只要样式对齐不是 left，文字就飘到中间；而且这个 cell 一旦被撑开，下面的行也跟着拉宽。
            // 这里把 titleTable 重建：cell 用自然宽度 + 左对齐，标题钉死在左上。
            try{
                d.titleTable.clearChildren();
                d.titleTable.left();
                d.titleTable.add(d.title).left().padLeft(2);
            }catch(e0b){ }
            try{ d.buttons.left(); }catch(e0c){ }
            // v0.19.0：宽屏双栏（左列表 / 右详情），窄屏单栏靠「← 返回列表」翻。
            // v0.19.1：尺寸按屏幕算，不再写死 300/440×430。
            // v0.19.2（用户截图：字重叠 / 宽度不够 / 按钮折行）：几乎顶满屏幕，算好的宽度存进 wsUI 供各栏排版。
            // v0.19.3（用户 m01237）：尺寸必须换算成 scene 单位（像素 / Scl.scl()），
            // 否则对话框比窗口宽一倍多，右栏直接被挤出屏幕。见 wsSclFactor 的注释。
            var wide = false;
            var scr = wsScreenSize();
            var plan = wsDialogPlan(scr.w, scr.h);
            wide = plan.wide;
            var availH = plan.availH;
            // v0.19.4：真机排版问题的取证 —— 写进 saves/last_log.txt（[mai] 前缀），不用再靠截图猜。
            try{
                log("ws dialog: screen=" + String(scr.w) + "x" + String(scr.h) + " scl=" + String(scr.scl)
                    + " plan=" + String(plan.availW) + "x" + String(plan.availH)
                    + " list=" + String(plan.listW) + " detail=" + String(plan.detailW) + " wide=" + String(wide));
            }catch(e1){ }
            var listT = wsT(), detailT = wsT();
            if(wide){
                try{
                    d.cont.pane(listT).width(plan.listW).height(availH).padRight(6);
                    d.cont.pane(detailT).width(plan.detailW).height(availH).row();
                }catch(e1){ log("ws two-pane failed: " + e1); d.cont.add(listT).row(); }
                wsUI.listW = plan.listW;
                wsUI.detailW = plan.detailW;
            }else{
                try{ d.cont.pane(listT).height(availH).width(plan.onlyW).row(); }catch(e2){ d.cont.add(listT).row(); }
                wsUI.listW = plan.onlyW;
                wsUI.detailW = plan.onlyW;
            }
            wsUI.open = true;
            wsUI.d = d;
            wsUI.single = !wide;
            wsUI.showDetail = false;
            wsUI.listHost = listT;
            wsUI.detailHost = wide ? detailT : listT;
            wsUI.ghosts = [];
            wsUI.needGeom = true;   // v0.19.5：等布局跑完再 dump 真实宽度（见 wsGeomDump）
            var listOf = function(){ return wsUI.listHost; };
            // 底部按钮不写死宽度：宽度跟着文字走，大字号下就不会折行。
            d.buttons.button("刷新", function(){ renderWorkspaces(listOf(), d); }).height(34).padLeft(4).ellipsis(true);
            // v0.18.3（用户 m11124）：「撤销功能移入 MAI 控制面板」。
            d.buttons.button("撤销上次写入", function(){
                var r = undo();
                if(Vars.ui !== null && Vars.ui !== undefined){ Vars.ui.showText("Mlog辅助 · 撤销", String(r)); }
                renderWorkspaces(listOf(), d);
            }).height(34).padLeft(4).ellipsis(true);
            d.buttons.button("撤销快照", function(){
                if(Vars.ui !== null && Vars.ui !== undefined){ Vars.ui.showText("Mlog辅助 · 撤销", String(undoInfo())); }
            }).height(34).padLeft(4).ellipsis(true);
            d.buttons.button("从蓝图创建", function(){ renderBlueprintPicker(listOf(), d); }).height(34).padLeft(4).ellipsis(true);
            d.buttons.button("关闭", function(){ d.hide(); }).height(34).padLeft(4).ellipsis(true);
            // 关窗必须清 open，否则世界层会一直画细框（drawWorkspaceGhosts 只认这个标志）。
            d.hidden(function(){ wsUI.open = false; wsUI.ghosts = []; wsUI.needGeom = false; });
            d.closeOnBack();
            d.show();
            renderWorkspaces(listT, d);
            return "workspace dialog";
        }

        // ---------------------------------------------------------- 区域面板（v0.6.0）
        // F3 松手之后：先落盘快照，再要 /region 的链接候选，最后开这个面板。
        // 面板里的「生成」按钮带着该处理器的引用直接开 AI 面板（showAiPanel(target)），
        // 省掉「先把鼠标移到那个处理器上」这一步。
        function buildAt(x, y){
            var t = Vars.world.tile(x, y);
            if(t === null || t === undefined){ return null; }
            var b = t.build;                    // 字段，不是方法（t.build() 会报错）
            if(b === null || b === undefined){ return null; }
            return b;
        }

        // v0.8.1：单格选中处理器 = 开 AI 面板（v0.9.0 起 F4 热键已删，只有这一条路）。
        function processorAt(x, y){
            var b = buildAt(x, y);
            return isProcessor(b) ? b : null;
        }

        function showRegionPanel(snap, head, candText){
            var d = new Packages.arc.scene.ui.Dialog("Mlog辅助 · 区域 v" + MOD_VERSION);
            d.cont.defaults().pad(4).left();
            // 同 showAiPanel：pane() 交回来的是 ScrollPane（没有 add），所以自己造 host 给它托管。
            var host = new Packages.arc.scene.ui.layout.Table();
            host.defaults().pad(2).left();
            buildRegionTable(host, snap, head, candText, function(x, y){
                var b = buildAt(x, y);
                if(b === null){ hint("@" + x + "," + y + " 上已经没有处理器了"); return; }
                try{ d.hide(); }catch(e){ }
                showAiPanel(b);
            });
            try{
                d.cont.pane(host).height(320).width(620);
            }catch(e){
                log("pane() overload unavailable in the region panel: " + e);
                d.cont.add(host).growX();
            }
            d.buttons.defaults().pad(4);
            d.buttons.button("复制快照", function(){
                try{ Core.app.setClipboardText(JSON.stringify(snap)); hint("快照已复制"); }
                catch(e){ hint("复制失败：" + e); }
            }).width(180);
            d.buttons.button("关闭", function(){ d.hide(); }).width(140);
            d.closeOnBack();
            d.show();
            return d;
        }

        // ---------------------------------------------------------- 处理器清单：扫描 + 面板（v0.7.0）
        // 只读：复用本地快检（quickApi）与 linkNames，不动任何方块。
        // Groups.build 里既有处理器也有普通建筑，用 isProcessor() 的鸭子类型筛。
        function scanProcessors(){
            var rows = [], api = quickApi(), build = gameBuild();
            var t0 = Packages.java.lang.System.currentTimeMillis();
            try{
                Groups.build.each(function(g){
                    try{
                        if(!isProcessor(g)){ return; }
                        var src = String(g.code);
                        var row = {x: g.tile.x, y: g.tile.y,
                                   lines: src.length === 0 ? 0 : src.split("\n").length,
                                   err: 0, warn: 0, links: 0, rules: ""};
                        try{ row.links = g.links.size; }catch(e){ }
                        if(api !== null){
                            var res = api.q.check(api.r, src, linkNames(g), {build: build});
                            var seen = {};
                            for(var i = 0; i < res.findings.length; i++){
                                var f = res.findings[i];
                                if(f.sev === "ERR"){ row.err++; } else { row.warn++; }
                                var k = String(f.rid || "?");
                                if(seen[k] !== true){ seen[k] = true; row.rules += (row.rules === "" ? "" : ",") + k; }
                            }
                        }
                        rows.push(row);
                    }catch(e){ }
                });
            }catch(e){ }
            rows.sort(function(a, b){
                if(b.err !== a.err){ return b.err - a.err; }
                if(b.warn !== a.warn){ return b.warn - a.warn; }
                if(a.y !== b.y){ return a.y - b.y; }
                return a.x - b.x;
            });
            return {rows: rows, ms: Packages.java.lang.System.currentTimeMillis() - t0,
                    api: api !== null};
        }

        function jumpTo(x, y){
            try{ Core.camera.position.set(x * 8 + 4, y * 8 + 4); }catch(e){ log("camera jump failed: " + e); }
        }

        function listText(){
            var res = scanProcessors();
            var out = [listHead(res)];
            for(var i = 0; i < res.rows.length && i < 40; i++){
                out.push("  " + listRowLabel(res.rows[i]));
            }
            if(res.rows.length > 40){ out.push("  …还有 " + (res.rows.length - 40) + " 个"); }
            return out.join("\n");
        }

        function showListPanel(res){
            var d = new Packages.arc.scene.ui.Dialog("Mlog辅助 · 处理器清单 v" + MOD_VERSION);
            d.cont.defaults().pad(4).left();
            // 同 showRegionPanel：pane() 交回来的是 ScrollPane（没有 add），自己造 host 给它托管。
            var host = new Packages.arc.scene.ui.layout.Table();
            host.defaults().pad(2).left();
            buildListTable(host, res, listHead(res), function(x, y){
                jumpTo(x, y);
                var b = buildAt(x, y);
                try{ d.hide(); }catch(e){ }
                if(b === null){ hint("@" + x + "," + y + " 上已经没有处理器了"); return; }
                showAiPanel(b);
            });
            try{
                d.cont.pane(host).height(340).width(560);
            }catch(e){
                log("pane() overload unavailable in the list panel: " + e);
                d.cont.add(host).growX();
            }
            d.buttons.defaults().pad(4);
            d.buttons.button("复制清单", function(){
                try{ Core.app.setClipboardText(listText()); hint("处理器清单已复制"); }
                catch(e){ hint("复制失败：" + e); }
            }).width(160);
            d.buttons.button("关闭", function(){ d.hide(); }).width(120);
            d.closeOnBack();
            d.show();
            return d;
        }

        function openListPanel(){
            var res = scanProcessors();
            try{ showListPanel(res); }
            catch(e){
                log("list panel failed, falling back to text: " + e);
                popup("Mlog辅助 · 处理器清单", listText());
            }
            return res;
        }

        // ---------------------------------------------------------- 动作

        function linkList(b){
            var out = [];
            if(b === null){ return out; }
            for(var i = 0; i < b.links.size; i++){
                var l = b.links.get(i);
                out.push({name: String(l.name), x: l.x, y: l.y});
            }
            return out;
        }

        // 这里原本有个 ping() 用 busy-wait「同步」等 /health —— 在真引擎里永远拿不到结果
        // （回调走 Core.app.post 投递回主线程，而调用方就在主线程上）。探活请用 probeSidecar()。

        // ---------------------------------------------------------- 本地快检（P2）
        // scripts/quickcheck.js 与 scripts/rules.generated.js 由上面的 require 取到。
        // 边界：这里只回答“单行 + 一张表”能定的事，不重写 sidecar 的 41 条规则。

        function gameBuild(){
            try{
                var v = Packages.mindustry.core.Version.build;
                return (v > 0) ? v : 0;      // 自定义构建是 -1，当作未知
            }catch(e){ return 0; }
        }

        function quickApi(){
            if(QUICK === null || RULES === null){ return null; }
            return {q: QUICK, r: RULES};
        }

        // 返回 null 表示「不知道这个处理器的链接表」，返回 [] 表示「确实没有链接」。
        // 这两个含义在 quickcheck 里分开用（stats.linkKnown）：把「不知道」当成「没有」
        // 会让每一行链接槽里的名字都报 link-name-not-bound（v0.4.6 的假 WARN）。
        // 返回 null 表示「不知道这个处理器的链接表」，返回 [] 表示「确实没有链接」。
        // jobqueue 靠这个区分门禁口径：[] 是权威空表（幻觉链接名必须拦），null 是不知道（不判）。
        function linkListOrNull(b){
            if(b === null || b === undefined){ return null; }
            try{
                var out = [];
                for(var i = 0; i < b.links.size; i++){
                    var l = b.links.get(i);
                    out.push({name: String(l.name), x: l.x, y: l.y});
                }
                return out;
            }catch(e){ return null; }
        }
        function linkNames(b){
            if(b === null || b === undefined){ return null; }
            var out = [];
            try{
                for(var i = 0; i < b.links.size; i++){ out.push(String(b.links.get(i).name)); }
            }catch(e){ return null; }
            return out;
        }

        function quickRun(code, b){
            var api = quickApi();
            if(api === null){ return {missing: true, findings: [], stats: null}; }
            var res = api.q.check(api.r, String(code || ""), linkNames(b), {build: gameBuild()});
            res.missing = false;
            return res;
        }

        function quickSummary(res){
            if(res.missing){ return "本地快检：不可用（quickcheck.js 未加载）"; }
            var err = 0, warn = 0, i;
            for(i = 0; i < res.findings.length; i++){
                if(res.findings[i].sev === "ERR"){ err++; } else { warn++; }
            }
            var st = res.stats;
            return "本地快检：" + (err ? ("ERR " + err) : "无 ERR")
                 + " / WARN " + warn
                 + "    语句 " + st.statements
                 + "    版本地板 build " + st.floor
                 + "    " + linkLine(res)
                 + (st.truncates ? "    ⚠ 超过 1000 条会被截断" : "")
                 + (st.linkKnown === false
                    ? "\n（链接表未知：读不到 LogicDialog.executor，本次跳过链接槽判据）"
                    : "");
        }

        function quickLines(res){
            var out = [];
            for(var i = 0; i < res.findings.length; i++){
                var f = res.findings[i];
                out.push("L" + f.line + " " + f.sev + " [" + (f.rid || "-") + "] " + f.msg);
            }
            return out;
        }

        // ---------------------------------------------------------- 区域快照（P3）
        // 只读扫描：交出「方块表 + 处理器表」，sidecar 用它算链接候选。
        // 终点仍是逻辑@config：候选 -> /plan 或 /compose -> bytes_b64 -> build.configure()，
        // 和游戏自己的配置通道是同一份载荷（LogicBlock.compress；联机时服务端应用，
        // LogicBlock.java:65-69），本地写入也是同一个调用，所以两条路不需要分开实现。
        // 引擎坑（v160.7 实测）：tile.block() 是方法，tile.build 是字段。
        var SNAP_MAX_CELLS = 4096;

        // LogicBlock 是「方块」类，真正的建筑是它的内部类 LogicBuild（LogicBlock$LogicBuild），
        // 所以 instanceof LogicBlock 永远为假（v160.7 实测）。跟 targetBuild() 一样用鸭子类型：
        // executor + code 这两个字段只有逻辑建筑才有。
        function isProcessor(bld){
            try{
                return bld !== null && bld !== undefined
                       && bld.executor !== undefined && bld.code !== undefined;
            }catch(e){ return false; }
        }

        function tileBlockName(tile, bld){
            try{
                if(bld !== null && bld !== undefined){ return String(bld.block.name); }
                return String(tile.block().name);
            }catch(e){ return "?"; }
        }

        // ------------------------------------------------------- v0.12.9 快照补全
        // 「这片区域有什么」不能只有方块名。引擎事实（v160.7）：库存挂在建筑上 ——
        // bld.items 是 ItemModule、bld.liquids 是 LiquidModule，没有这两样的建筑上它们是 null；
        // 一个 2x2 方块在四个格子上是同一个建筑（同一个 bld），所以库存/队伍必须按建筑身份去重，
        // 而 grid / blocks 是按格计数 —— 两者别混（container 2x2 = grid 4 格、stores 1 条）。
        var SNAP_MAX_STORES = 16;

        // 引擎坑（v160.7 实测，2026-10-31）：arc 的 Seq 既有 public int size 字段、也有 size()
        // 方法；普通 Seq 上 Rhino 取到的是字段（数字），但 EntityGroup 自己又声明了一遍
        // size()（mindustry/entities/EntityGroup.class），子类方法把继承来的字段挡住了 ——
        // 于是 Groups.unit.size 是个函数。写成 i < seq.size 会永远不成立（循环一次都不跑，
        // 单位数静默变 0），所以统一走这里：先当数字，不行再当函数调。
        function seqSize(seq){
            if(seq === null || seq === undefined){ return 0; }
            var v = 0;
            try{ v = seq.size; }catch(e){ v = 0; }
            if(typeof v === "number"){ return v; }
            try{ v = seq.size(); }catch(e){ v = 0; }
            return typeof v === "number" ? v : 0;
        }

        function stockOf(mod, seq){
            // mod: ItemModule / LiquidModule（可能 null）；seq: 内容表（Vars.content.items() 等）
            var out = {}, total = 0, i;
            if(mod === null || mod === undefined || seq === null || seq === undefined){
                return {v: out, n: 0};
            }
            var n = seqSize(seq);
            for(i = 0; i < n; i++){
                var c = seq.get(i), v = 0;
                try{ v = Number(mod.get(c)); }catch(e){ v = 0; }
                if(!(v > 0)){ continue; }
                out[String(c.name)] = Math.round(v * 100) / 100;
                total += v;
            }
            return {v: out, n: total};
        }

        function addInto(dst, src){
            for(var k in src){
                if(!Object.prototype.hasOwnProperty.call(src, k)){ continue; }
                dst[k] = (dst[k] || 0) + src[k];
            }
        }

        function countKeys(o){
            var n = 0;
            for(var k in o){ if(Object.prototype.hasOwnProperty.call(o, k)){ n++; } }
            return n;
        }

        function stockText(o, max){
            var parts = [], n = 0, k;
            for(k in o){
                if(!Object.prototype.hasOwnProperty.call(o, k)){ continue; }
                n++;
                if(n <= max){ parts.push(k + " " + o[k]); }
            }
            if(n > max){ parts.push("…还有 " + (n - max) + " 种"); }
            return n === 0 ? "无" : parts.join("、");
        }

        // 单位不在方块网格里，得走 Groups.unit 再按 tile 落点筛。坐标是世界像素，
        // 除以 Vars.tilesize（8）才是 tile 坐标。
        // 引擎坑（v160.7 实测，2026-10-31）：Groups.unit 是 EntityGroup，它把继承来的
        // Seq.get 挡住了 —— typeof Groups.unit.get 是 undefined（`Cannot find function get`），
        // 而 Groups.unit.each / rawSeq() 都在。所以先拿 rawSeq() 的底层 Seq 按索引走，
        // 拿不到再退回 each 回调；两条路都留，因为哪条能用取决于引擎版本。
        function unitStats(x1, y1, x2, y2, myTeam){
            var res = {total: 0, types: {}, teams: {}, enemies: 0, enemyTypes: {}};
            var seq = null;
            try{ seq = Packages.mindustry.gen.Groups.unit; }catch(e){ return res; }
            if(seq === null || seq === undefined){ return res; }
            var ts = 8;
            try{ ts = Number(Vars.tilesize); }catch(e){ ts = 8; }
            if(!(ts > 0)){ ts = 8; }

            function count1(u){
                if(u === null || u === undefined){ return; }
                var ux = 0, uy = 0, tn = "?", tm = "?";
                try{ ux = Math.floor(Number(u.x) / ts); uy = Math.floor(Number(u.y) / ts); }
                catch(e){ return; }
                if(ux < x1 || ux > x2 || uy < y1 || uy > y2){ return; }
                try{ tn = String(u.type.name); }catch(e){ }
                try{ tm = String(u.team.name); }catch(e){ }
                res.total += 1;
                res.types[tn] = (res.types[tn] || 0) + 1;
                res.teams[tm] = (res.teams[tm] || 0) + 1;
                if(myTeam !== null && tm !== myTeam){
                    res.enemies += 1;
                    res.enemyTypes[tn] = (res.enemyTypes[tn] || 0) + 1;
                }
            }

            var list = seq;
            try{
                if(typeof seq.rawSeq === "function"){
                    var raw = seq.rawSeq();
                    if(raw !== null && raw !== undefined){ list = raw; }
                }
            }catch(e){ list = seq; }

            var getter = null;
            try{ getter = list.get; }catch(e){ getter = null; }
            if(typeof getter === "function"){
                var n = seqSize(list);
                for(var i = 0; i < n; i++){
                    var u = null;
                    try{ u = list.get(i); }catch(e){ u = null; }
                    count1(u);
                }
                return res;
            }
            try{ list.each(function(u){ count1(u); }); }catch(e){ }
            return res;
        }

        function regionSnapshot(x1, y1, x2, y2){
            var W = Vars.world;
            var mapName = "?";
            try{ mapName = String(Vars.state.map.name()); }catch(e){ }
            x1 = Math.floor(x1); y1 = Math.floor(y1);
            x2 = Math.floor(x2); y2 = Math.floor(y2);
            if(x1 > x2){ var a = x1; x1 = x2; x2 = a; }
            if(y1 > y2){ var b0 = y1; y1 = y2; y2 = b0; }
            var maxX = W.width() - 1, maxY = W.height() - 1;
            if(x1 < 0){ x1 = 0; }
            if(y1 < 0){ y1 = 0; }
            if(x2 > maxX){ x2 = maxX; }
            if(y2 > maxY){ y2 = maxY; }
            var grid = [], procs = [], truncated = false, x, y;
            // v0.12.9：方块计数 / 库存 / 单位 / 敌方
            var blocks = {}, items = {}, liquids = {}, stores = [], seenIds = {}, seenProcs = {}, enemyBlocks = 0;
            var itemsSeq = null, liquidsSeq = null;
            try{ itemsSeq = Vars.content.items(); }catch(e){ itemsSeq = null; }
            try{ liquidsSeq = Vars.content.liquids(); }catch(e){ liquidsSeq = null; }
            var myTeam = null;
            try{
                var pl = localPlayer();
                if(pl !== null && pl !== undefined){ myTeam = String(pl.team().name); }
            }catch(e){ myTeam = null; }
            for(x = x1; x <= x2 && !truncated; x++){
                for(y = y1; y <= y2; y++){
                    var tile = W.tile(x, y);
                    if(tile === null || tile === undefined){ continue; }
                    var bld = tile.build;
                    var name = tileBlockName(tile, bld);
                    if(name === "air"){ continue; }
                    if(grid.length >= SNAP_MAX_CELLS){ truncated = true; break; }
                    grid.push({x: x, y: y, block: name});
                    blocks[name] = (blocks[name] || 0) + 1;
                    if(isProcessor(bld)){
                        // v0.18.3（用户 m11320）：逻辑处理器 2x2、超核处理器 3x3，
                        // 同一台建筑在它占的每个格子上都被枚举一次 ⇒ 完全框选时
                        // 同一份逻辑被下发 4/9 个作业。按建筑 id 去重（和库存同一手）。
                        var pid = null;
                        try{ pid = String(bld.id); }catch(eP){ pid = null; }
                        if(pid === null || pid === "undefined"){ pid = "t" + x + "," + y; }
                        if(seenProcs[pid] !== true){
                            seenProcs[pid] = true;
                            procs.push({x: x, y: y, block: name,
                                        code: String(bld.code === null ? "" : bld.code),
                                        links: linkList(bld)});
                        }
                    }
                    if(bld !== null && bld !== undefined){
                        // 同一个建筑在它占的每个格子上都出现一次：库存只算一遍。
                        var bid = null;
                        try{ bid = String(bld.id); }catch(e){ bid = null; }
                        if(bid === null || bid === "undefined"){ bid = "t" + x + "," + y; }
                        if(seenIds[bid] !== true){
                            seenIds[bid] = true;
                            var tname = "?";
                            try{ tname = String(bld.team.name); }catch(e){ }
                            if(myTeam !== null && tname !== myTeam){ enemyBlocks += 1; }
                            var si = stockOf(bld.items, itemsSeq);
                            var sl = stockOf(bld.liquids, liquidsSeq);
                            addInto(items, si.v);
                            addInto(liquids, sl.v);
                            if((countKeys(si.v) > 0 || countKeys(sl.v) > 0)
                               && stores.length < SNAP_MAX_STORES){
                                stores.push({x: x, y: y, block: name, team: tname,
                                             items: si.v, liquids: sl.v});
                            }
                        }
                    }
                }
            }
            var us = unitStats(x1, y1, x2, y2, myTeam);
            var enemies = {units: us.enemies, blocks: enemyBlocks, types: us.enemyTypes};
            if(myTeam === null){
                enemies.note = "no local player, so 'enemy' has no meaning here; both counts are 0";
            }
            return {schema: 1, mod: MOD_VERSION, build: gameBuild(), map: mapName,
                    world: {width: W.width(), height: W.height()},
                    bounds: {x1: x1, y1: y1, x2: x2, y2: y2},
                    cells: grid.length, truncated: truncated,
                    grid: grid, processors: procs,
                    blocks: blocks, items: items, liquids: liquids, stores: stores,
                    units: {total: us.total, types: us.types, teams: us.teams},
                    enemies: enemies};
        }

        // Last non-empty line of a diagnostic, trimmed: the user needs one line
        // of cause, not a Java stack (a refused socket used to be dumped whole).
        function lastLine(t){
            var parts = String(t === null || t === undefined ? "" : t).split("\n");
            var s = "", i;
            for(i = 0; i < parts.length; i++){
                var q = parts[i];
                while(q.length && (q.charAt(0) === " " || q.charAt(0) === "\t")){ q = q.substring(1); }
                if(q.length){ s = q; }
            }
            return s.length > 200 ? s.substring(0, 200) + "…" : s;
        }
        function httpWhy(code, body){
            var t = String(body === null || body === undefined ? "" : body);
            if(code === "" || code === "-1" || code === -1 || code === undefined || code === null){
                if(t.indexOf("Connection refused") >= 0){
                    return "sidecar 未运行（连接被拒绝，" + base + "）\n先启动 sidecar：python tools/logicd.py";
                }
                if(t.indexOf("timed out") >= 0 || t.indexOf("SocketTimeout") >= 0){
                    return "sidecar 没回应（超时，" + base + "）\n确认 python tools/logicd.py 仍在运行";
                }
                return "连不上 sidecar（" + base + "）\n先启动 sidecar：python tools/logicd.py";
            }
            return "sidecar 返回 " + code;
        }
        // 侧车进程比模组旧时，/jobs 会回 404 no such endpoint -- 玩家看到这句不知道要干什么，
        // 而唯一的解法就是重启那个 python 进程，所以直接把解法写进错误里。
        function sidecarStaleHint(t){
            var s = String(t === null || t === undefined ? "" : t);
            if(s.indexOf("no such endpoint") < 0){ return null; }
            return "sidecar 是旧版本，不认识这个接口（" + base + "）\n"
                 + "关掉旧的 sidecar 进程再重新启动：python tools/logicd.py --port 8765\n"
                 + "（改过 tools/logicd.py 后必须重启，模组连的是内存里的旧代码）";
        }
        function jobWhy(code, text, res){
            var hint = sidecarStaleHint(text) || sidecarStaleHint(res && res.error);
            if(hint !== null){ return hint; }
            return (res && res.error) ? String(res.error) : httpWhy(code, text);
        }
        function regionCandidatesText(raw){
            var s = String(raw === null || raw === undefined ? "" : raw);
            var nl = s.indexOf("\n");
            var code = nl < 0 ? "" : s.substring(0, nl);
            var body = nl < 0 ? s : s.substring(nl + 1);
            var res = parse(body);
            if(code !== "200" || res === null || res.ok !== true){
                var why = httpWhy(code, body);
                var diag = (code === "200" && res !== null && res.message)
                    ? String(res.message) : lastLine(body);
                return diag.length > 0 ? why + "\n\n" + diag : why;
            }
            var out = ["区域 " + res.bounds.x1 + "," + res.bounds.y1 + " - "
                       + res.bounds.x2 + "," + res.bounds.y2
                       + "   方块 " + res.cells
                       + (res.truncated ? "（已截断）" : "")];
            var ps = res.processors || [], i, j;
            if(ps.length === 0){ out.push("这个区域里没有处理器"); }
            for(i = 0; i < ps.length; i++){
                var p = ps[i];
                out.push("");
                out.push("@" + p.x + "," + p.y + " " + p.block + "   链接 "
                         + p.links.length + "：" + (p.links.length ? p.links.join(", ") : "无"));
                var ms = p.missing || [];
                for(j = 0; j < ms.length; j++){
                    out.push("  ⚠ " + ms[j].name + " 代码在用，但没绑上，也不在候选里"
                             + (ms[j].suggest ? "（同类可用 " + ms[j].suggest + "）"
                                              : "（本区域没有同类方块）"));
                }
                var cs = p.candidates || [];
                if(cs.length === 0){ out.push("  10 格内没有可连的方块"); }
                for(j = 0; j < cs.length && j < 12; j++){
                    out.push("  " + cs[j].name + "  ←  " + cs[j].block
                             + " @" + cs[j].x + "," + cs[j].y
                             + "   " + cs[j].dist + " 格"
                             + (cs[j].used ? "   [代码正在用]" : ""));
                }
                if(cs.length > 12){ out.push("  …还有 " + (cs.length - 12) + " 个"); }
            }
            out.push("");
            out.push("按住 F3 点一下处理器，用一句话生成，写入走 logic@config（configure）。");
            return out.join("\n");
        }
        function regionText(snap){
            var p = snap.processors, i;
            var out = ["区域 " + snap.bounds.x1 + "," + snap.bounds.y1 + " - "
                       + snap.bounds.x2 + "," + snap.bounds.y2
                       + "   方块 " + snap.cells + (snap.truncated ? "（已截断）" : "")
                       + "   处理器 " + p.length];
            // v0.12.9：区域里还有什么（库存 / 单位 / 敌人），一行说清
            var un = snap.units || {total: 0}, en = snap.enemies || {units: 0, blocks: 0};
            out.push("  方块 " + countKeys(snap.blocks || {}) + " 种"
                     + "   物品 " + stockText(snap.items || {}, 6)
                     + "   液体 " + stockText(snap.liquids || {}, 4)
                     + "   单位 " + un.total + "（敌方单位 " + en.units
                     + "，敌方建筑 " + en.blocks + "）");
            for(i = 0; i < p.length; i++){
                out.push("  @" + p[i].x + "," + p[i].y + " " + p[i].block
                         + "  链接 " + p[i].links.length);
            }
            return out.join("\n");
        }
        // ---------------------------------------------------------- 控制台接口
        var api = {
            // v0.15.0：面板上的失败说明与诊断（无头环境没有侧车也能自检文案）。
            // v0.16.0 B1：蓝图保存的守卫（无头环境 Vars.schematics 为 null）。
            // v0.18.0：无头环境没有 Vars.ui，只能验布线 + 标记读回。
        maiWelcomeProbe: function(){
            var ft = fakeTable();
            var why = "", cells = 0;
            try{ welcomeBuild(ft.table, function(){ return ft.table; }); cells = ft.count(); }
            catch(e){ why = String(e); }
            return (why.length === 0 ? "ok" : ("bad " + why))
                 + " seen=" + (welcomeSeen() ? "yes" : "no")
                 + " cells=" + cells + " side=" + SIDE.state;
        },

        // v0.18.1：主题色 / 强调色 / 提示换行的自检口（无头环境没有 Scene，也能看颜色与断行）。
        maiThemeProbe: function(){
            var sample = "长解释收进 infoCircle：只在鼠标悬浮时显示，常驻的只有短句；"
                       + "图标固定贴在每一行的最右侧，提示按 34 个字软换行，"
                       + "Core.settings 这类英文整块搬，不会被拆开。";
            var wrapped = softWrap(sample, 34);
            var ls = wrapped.split("\n"), i, mx = 0;
            for(i = 0; i < ls.length; i++){ if(ls[i].length > mx){ mx = ls[i].length; } }
            // v0.18.2：颜色现在是设置项，写入后必须立刻读得回来（再还原）。
            var orig = null, set = "none", back = "none";
            try{ orig = Core.settings.get(THEME_KEY, null); }catch(e10){ }
            try{
                Core.settings.put(THEME_KEY, "ff8800");
                set = String(colorHexOf(themeColor()));
            }catch(e11){
                set = "bad " + e11;
            }
            try{
                if(orig === null || orig === undefined){ Core.settings.remove(THEME_KEY); }
                else { Core.settings.put(THEME_KEY, String(orig)); }
                back = String(themeHex());
            }catch(e12){
                back = "bad " + e12;
            }
            return "MAITHEME:ok theme=" + String(themeColor()) + " emph=" + String(emphColor())
                 + " lines=" + ls.length + " maxline=" + mx + " first=" + ls[0]
                 + " set=" + set + " back=" + back + " dflt=" + THEME_DEFAULT;
        },

        // v0.18.1（用户 m10546）「设置控制更改的热键不生效」的回归口：
        // KeybindDialog.rebind 改完键落盘的是「只有 min/max、key 为 null」的 Axis
        // （KeybindDialog.java:133-140 → KeyBind.java:74-77），这里直接造出那个形状，
        // 看模组的读数有没有跟上；再验一遍单键形状，最后还原默认值。
        maiKeybindProbe: function(){
            var kb = selKeyBind();
            if(kb === null || kb === undefined){ return "MAIKEYBIND:no bind"; }
            var before = null;
            var out = "";
            try{
                before = kb.value;
                kb.value = new Packages.arc.input.KeyBind.Axis(
                    Packages.arc.input.KeyCode.g, Packages.arc.input.KeyCode.h);
                var c1 = selKeyCode();
                out = "MAIKEYBIND:ok axis=" + selKeyName()
                    + " code=" + (c1 === null || c1 === undefined ? "null" : String(c1.getName()));
                kb.value = new Packages.arc.input.KeyBind.Axis(Packages.arc.input.KeyCode.y);
                var c2 = selKeyCode();
                out = out + " one=" + selKeyName()
                    + " onecode=" + (c2 === null || c2 === undefined ? "null" : String(c2.getName()));
            }catch(e){
                out = "MAIKEYBIND:bad " + e;
            }
            try{ if(before !== null && before !== undefined){ kb.value = before; } }catch(e2){ }
            return out + " back=" + selKeyName();
        },

        maiBlueprintSaveProbe: function(rjson){
                var r = null;
                try{ r = JSON.parse(String(rjson)); }catch(e){ r = null; }
                var msg = blueprintSaveBox(r, "探针蓝图", null);
                return "probeblueprint:" + String(msg);
            },

            // v0.16.1 B2/B4：签名上下文解码、「详细」页标题解析、蓝图上下文读取。
            maiSigMetaProbe: function(code){
                var meta = sigMetaOf(String(code === null || code === undefined ? "" : code));
                if(meta === null){ return "MAISIGMETA:none"; }
                return "MAISIGMETA:ws=" + String(meta.ws) + " name=" + String(meta.name)
                     + " job=" + String(meta.job) + " prompt=" + String(meta.prompt).substring(0, 20);
            },
            maiBpTitleProbe: function(txt){
                return "MAIBPTITLE:[" + blueprintTitleName(txt) + "]";
            },
            maiBpChatProbe: function(name){
                var ctx = blueprintCtxOf(String(name));
                if(ctx === null){ return "MAIBPCHAT:none"; }
                return "MAIBPCHAT:code=" + String(String(ctx.code).length)
                     + " meta=" + (ctx.meta === null ? "no" : ("yes/" + String(ctx.meta.ws)));
            },
            maiJobDiagProbe: function(jobJson){
                var job = (typeof jobJson === "string") ? parse(jobJson) : jobJson;
                if(job === null || job === undefined){ return "MAIJOBPANELDIAG:no job"; }
                var why = jobWhyLines(job, 3), ev = jobEvidenceLines(job);
                return "MAIJOBPANELDIAG:ok why=" + why.length + " ev=" + ev.length
                    + " first=" + (why.length > 0 ? why[0] : "")
                    + " line=" + (ev.length > 0 ? ev[ev.length - 1] : "");
            },

            // v0.15.1（D2/D3）：最后一条提示 + 列表过滤，纯函数，无头环境可自检。
            maiHintProbe: function(txt){
                hint(String(txt === undefined || txt === null ? "" : txt));
                return "ok last=" + (String(LAST_HINT.text).length > 0 ? "yes" : "no")
                     + " label=" + hintLineLabel();
            },
            maiFilterProbe: function(listJson, text, onlyBad){
                var list = (typeof listJson === "string") ? parse(listJson) : (listJson || []);
                managerFilter.text = String(text === undefined || text === null ? "" : text);
                managerFilter.onlyBad = (onlyBad === true);
                var hit = 0, i;
                for(i = 0; i < (list || []).length; i++){ if(jobHit(list[i])){ hit++; } }
                var out = "ok hit=" + hit + " of " + (list || []).length
                        + " btn=" + managerFilterBtnText();
                managerFilter.text = "";
                managerFilter.onlyBad = false;
                return out;
            },
            maiVersion: function(){ return "mlog-auxiliary " + MOD_VERSION + "  sidecar=" + base; },
        // 调试/测试用：同步原生请求，返回 "<code>" + 换行 + 正文。会占住调用线程到超时，
        // 只对 localhost 用；正式路径仍然是 httpJson + Core.app.post。
        maiHttpRaw: function(method, url, body){ var r = httpRaw(method, String(url), body === undefined ? null : body); return r.code + "\n" + r.text; },
            maiPing:    function(){
                httpJson("GET", "/health", null, function(code, text){
                    var out = "sidecar " + base + " -> " + code + "\n" + String(text).substring(0, 300);
                    print("[mai] " + out);
                    if(Vars.ui !== null){ Vars.ui.showText("Mlog辅助 · 连接", out); }
                });
                return "已发出 health 请求（结果稍后弹窗）";
            },
            maiSel:     function(){ return describe(targetBuild()); },
            maiUndo:    function(){ return undo(); },
            maiCheck:   function(code){
                var b = targetBuild();
                var src = (code === undefined || code === null) ? (b === null ? "" : String(b.code)) : String(code);
                var res = quickRun(src, b);
                if(res.missing){ return "本地快检不可用"; }
                var out = [quickSummary(res)];
                var ls = quickLines(res);
                for(var i = 0; i < ls.length; i++){ out.push(ls[i]); }
                return out.join("\n");
            },
            maiScan:    function(){
                var api = quickApi();
                if(api === null){ return "本地快检不可用"; }
                var build = gameBuild();
                var t0 = Packages.java.lang.System.currentTimeMillis();
                var n = 0, bad = 0, errs = 0, warns = 0;
                var byRule = {};
                Groups.build.each(function(g){
                    try{
                        if(g.executor === undefined || g.code === undefined){ return; }
                        var res = api.q.check(api.r, String(g.code), linkNames(g), {build: build});
                        n++;
                        if(res.findings.length > 0){ bad++; }
                        for(var i = 0; i < res.findings.length; i++){
                            var f = res.findings[i];
                            if(f.sev === "ERR"){ errs++; } else { warns++; }
                            var k = f.rid || "(no-id)";
                            byRule[k] = (byRule[k] || 0) + 1;
                        }
                    }catch(e){ }
                });
                var ms = Packages.java.lang.System.currentTimeMillis() - t0;
                var out = ["本地快检扫描：" + n + " 个处理器，" + bad
                         + " 个有命中（ERR " + errs + " / WARN " + warns + "），" + ms + " ms"];
                for(var k in byRule){ out.push("  " + k + "  " + byRule[k]); }
                return out.join("\n");
            }
        };

        // 调试入口也挂到控制台（设置界面用不了时的退路）
        api.maiDiag = function(){ return diagText(); };
        api.maiSettings = function(){ return registerSettings() ? "registered" : "no ui"; };
        api.maiSelfTest = function(){ return selfTest(); };
        api.maiUndo = function(){ return undo(); };
        api.maiUndoInfo = function(){ return undoInfo(); };
        // 落盘路径的真引擎自检（ui smoke 用）：写一份假快照进 <dataDir>/mlogauxiliary/selftest，
        // 读回来比对 x/y/code，然后删掉文件和目录 —— 只在测试里跑，正常游戏不碰这个目录。
        api.maiUndoProbe = function(){
            var root = null, made = false, name = null, out = "no-dir";
            try{
                root = Vars.dataDirectory.child("mlogauxiliary").child("selftest");
                if(!root.exists()){ root.mkdirs(); made = true; }
                var b = {tile: {x: 3, y: 4}, code: "set n 0\nprint n\nend", links: {size: 0}};
                name = saveUndoTo(root, b, "selftest");
                var back = parse(root.child(name).readString("UTF-8"));
                var ok = (back !== null && back.x === 3 && back.y === 4 && back.code === b.code
                          && back.links.length === 0);
                root.child(name).delete();
                if(made){ root.deleteDirectory(); }
                out = ok ? ("ok " + name) : "bad";
            }catch(e){ out = "fail " + e; }
            return out;
        };
        // 模型档设置的自检（ui smoke 用）：存三件套 -> 看 llmText 是否显示模型、
        // 是否绝不显示密钥明文、是否把「还没提交」说出来 -> 原样恢复。
        api.maiLlmProbe = function(){
            try{
                var prev = {u: llmGet(LLM_URL_KEY, ""), m: llmGet(LLM_MODEL_KEY, ""),
                            k: llmGet(LLM_KEY_KEY, "")};
                var boot = llmText();
                if(boot.indexOf("未配置模型") < 0){ return "bad tier text: " + boot; }
                llmSave(LLM_URL_KEY, "https://example.invalid/v1/chat/completions");
                llmSave(LLM_MODEL_KEY, "m-probe");
                llmSave(LLM_KEY_KEY, "sk-probe-9999");
                var t = llmText();
                if(t.indexOf("m-probe") < 0){ return "model not shown: " + t; }
                if(t.indexOf("sk-probe") >= 0){ return "key leaked into the text: " + t; }
                if(t.indexOf("9999") >= 0){ return "key tail leaked into the text: " + t; }
                if(t.indexOf("未同步") < 0){ return "pending key not called out: " + t; }
                llmSave(LLM_URL_KEY, prev.u);
                llmSave(LLM_MODEL_KEY, prev.m);
                llmSave(LLM_KEY_KEY, prev.k);
                return "ok " + t.length;
            }catch(e){ return "fail " + e; }
        };
        api.maiAi = function(){ showAiPanel(); return "opened"; };
        api.maiReview = function(){ return reviewText(); };
        // 测试钩子（tests/mlogauxiliary/run_ui_smoke.py 用）：喂一个假 statements
        // （每个孩子代表一条语句）+ 一组 findings，返回 "行号:x,y:颜色" 清单。
        // 真机上没人调它，行内箭头走 refreshReview -> syncArrows。
        api.maiArrows = function(statements, findingsJson, drawable){
            var res = {findings: [], stats: null};
            if(findingsJson){
                var f = parse(String(findingsJson));
                if(f !== null && f !== undefined){ res.findings = f; }
            }
            var n = syncArrows(res, statements, drawable);
            var out = ["arrows " + n];
            for(var i = 0; i < arrowRows.length; i++){
                var r = arrowRows[i];
                out.push(r.line + ":" + Math.round(r.img.x) + "," + Math.round(r.img.y)
                         + ":" + sevNameOf(r.img) + ":" + r.items + ":" + (r.img.visible ? "v" : "h"));
            }
            return out.join(" ");
        };
        // 区域快照的三个出口：JSON（给 sidecar / 剪贴板）、纯文本（控制台读）、
        // 以及直接 POST /region 拿链接候选。F7 框选等 UI 之后再接，这里先给控制台。
        api.maiSnapshot = function(x1, y1, x2, y2){
            return JSON.stringify(regionSnapshot(x1, y1, x2, y2));
        };
        api.maiSnapshotText = function(x1, y1, x2, y2){
            return regionText(regionSnapshot(x1, y1, x2, y2));
        };
        // 区域快照一律异步：回调由 httpJson 用 Core.app.post 投递回主线程，而控制台与热键
        // 都跑在主线程 —— 在那里 busy-wait 只能等到超时（v0.4.2 的 F7 返回字面量 "pending"）。
        function regionAroundBox(x1, y1, x2, y2){
            httpJson("POST", "/region", JSON.stringify({snapshot: regionSnapshot(x1, y1, x2, y2)}),
                     function(code, text){
                         if(Vars.ui === null){ return; }
                         Vars.ui.showText("Mlog辅助 · 链接候选", regionCandidatesText(code + "\n" + text));
                     });
        }
        api.maiRegion = function(x1, y1, x2, y2){
            regionAroundBox(x1, y1, x2, y2);
            return "已发出区域快照（结果稍后弹窗）";
        };
        api.maiRegionAround = function(r){
            var b = targetBuild();
            if(b === null){ return "光标下没有处理器"; }
            var n = (r === undefined || r === null) ? 10 : Math.floor(r);
            return api.maiRegion(b.tile.x - n, b.tile.y - n, b.tile.x + n, b.tile.y + n);
        };

        // ---------------------------------------------------------- F3 拖拽框选（v0.4.8）
        // 规划里 F7 本来就是「框选矩形」（docs/Mlog辅助-规划.md:99），v0.4.2 只做了只读的一半：
        // 以鼠标下的处理器为中心扫固定半径。F8 在游戏里是控制台
        // （Mindustry-Source/core/src/mindustry/input/Binding.java:107 console = KeyCode.f8），
        // F9/F10 也被占，所以框选另开一个空着的键：F3。
        // 拖拽时的高亮画在世界坐标上（Events.run(Trigger.draw) + Lines.rect），不往场景树里塞元素：
        // v0.4.1 那次崩溃就是自造结果区引起的，能画的地方就不建 UI。
        // v0.5.0 起高亮照抄原版选框（InputHandler.drawSelection）：2px 双描边 + 尺寸文本，无填充。
        // 状态机（纯函数，控制台/自检直接驱动）：**F3 就是鼠标左键**。
        // 按住 F3 = 按下左键（起点 = 此刻鼠标格），移动鼠标改终点，松开 F3 = 抬起左键并出快照。
        // 没有「就绪」这个中间态（v0.4.9 是「按 F3 就绪 + 按左键拖」，用户 2026-10-30 要的就是按下即拉框）。
        var selHolding = false, selDragging = false;
        var selStartX = 0, selStartY = 0, selCurX = 0, selCurY = 0;

        // 屏幕 -> 格子。mouseWorldX/Y 已经过相机变换，tileWorld 再按 tilesize 取整。
        function mouseTile(){
            var t = Vars.world.tileWorld(Core.input.mouseWorldX(), Core.input.mouseWorldY());
            if(t === null || t === undefined){ return null; }
            return {x: t.x, y: t.y};
        }

        // 拖拽方向不重要：左上到右下、右下到左上必须给出同一个矩形。
        function selRect(ax, ay, bx, by){
            var x1 = Math.min(ax, bx), y1 = Math.min(ay, by);
            var x2 = Math.max(ax, bx), y2 = Math.max(ay, by);
            return {x1: x1, y1: y1, x2: x2, y2: y2, w: x2 - x1 + 1, h: y2 - y1 + 1};
        }

        // selPress = 按下左键，selRelease = 抬起左键。F3 的 keyDown/keyUp 边沿直接映射到这两个上。
        function selPress(x, y){
            if(selDragging){ return false; }
            selHolding = true;
            selDragging = true;
            selStartX = x; selStartY = y; selCurX = x; selCurY = y;
            return true;
        }
        function selMove(x, y){
            if(!selDragging){ return false; }
            selCurX = x; selCurY = y;
            return true;
        }
        function selBox(){ return selRect(selStartX, selStartY, selCurX, selCurY); }
        function selCancel(){ selHolding = false; selDragging = false; }
        function selRelease(){
            if(!selDragging){ return null; }
            var r = selBox();
            selCancel();
            return r;
        }

        // 高亮参数照原版选框：**左下角 + 宽高**（Lines.rect(x,y,w,h)），不是中心点。
        // 原版见 Mindustry-Source/core/src/mindustry/input/InputHandler.java:1758-1795 drawSelection()。
        // 还要再退半格：Tile.worldx() 返回 x*tilesize（Tile.java:166-172 —— 那是格子的**中心**），
        // 所以格子 (x1,y1) 的左下角是 (x1-0.5)*8。原版也是这么算的：Placement.java:380-390
        // drawResult.x = x*tilesize - block.size*scaling*tilesize/2（air：size 1、scaling 1 ⇒ 减 4）。
        // v0.6.0 漏了这个半格，框整体偏右上角半个格子（用户实测）。
        function selDrawArgs(r){
            return {x: (r.x1 - 0.5) * 8, y: (r.y1 - 0.5) * 8, w: r.w * 8, h: r.h * 8};
        }

        // 原版选框没有半透明填充：Lines.stroke(2) 两道描边，
        // 先 accentBack 画一条 y-1 的阴影，再 accent 画本体（InputHandler.java:1765-1770）。
        function drawSelection(){
            // v0.12.5: 拖拽时画实时框；松手之后 jobBox 还挂着就继续画它（直到这一批终态）。
            var live = selDragging, r = null;
            if(live){ r = selBox(); }
            else if(jobBox !== null && jobBox.r){ r = jobBox.r; }
            // v0.15.2：框没了（取消 / 右键关）也要摘控件 —— 以前这里直接 return，
            // HUD 上那张表就永远留在屏幕上，另外两个按钮看着像没关掉。
            if(r === null){ jobBadge = null; jobWidgetHide(); return; }
            var a = selDrawArgs(r);
            try{
                var D = Packages.arc.graphics.g2d.Draw;
                var L = Packages.arc.graphics.g2d.Lines;
                L.stroke(2);
                // v0.18.2（用户 m10938）：「逻辑选框应用主题色」——阴影仍取 accentBack（和原版同配方）。
                D.color(Pal.accentBack);
                L.rect(a.x, a.y - 1, a.w, a.h);
                D.color(themeColor());
                L.rect(a.x, a.y, a.w, a.h);
                D.color();
            }catch(e){ /* 画不出来不影响选择本身 */ }
            // v0.17.0（用户 m09440 / 4）：框里的逻辑处理器直接描出来，
            // 拖的时候就知道框住了哪几台（只画边、不填色，和原版选框同一套画法）。
            try{ drawLogicHighlight(r); }catch(e9){ }
            // v0.14.4：框角操作区优先用 HUD 上的原版控件；控件不可用（无 UI / 建表失败）时
            // 才退回即时绘制 —— 两条路都留，谁也挡不住谁。
            if(live){ drawSizeText(r); jobWidgetHide(); }
            else if(!jobButtonsSync()){ drawJobButtons(r, a); }
        }

        // v0.17.0：框里的逻辑处理器逐格描边。框太大就跳过多余的扫描，宁可少画也不掉帧。
        function drawLogicHighlight(r){
            var w = r.x2 - r.x1 + 1, h = r.y2 - r.y1 + 1;
            if(!(w > 0) || !(h > 0)){ return; }
            if(w * h > 1024){ return; }
            var D = Packages.arc.graphics.g2d.Draw;
            var L = Packages.arc.graphics.g2d.Lines;
            L.stroke(2);
            // v0.18.2（用户 m10938 / m10943）：高亮用强调色；位置要在格子正中 ——
            // Tile.worldx() 返回 x*tilesize 是格子中心（Tile.java:166-172），
            // 所以左下角要再退半格，少退这半格就整体偏右上角（v0.6.0 选框踩过同一个坑）。
            D.color(emphColor());
            // v0.18.3（用户 m11340）：高亮和选框同步缩放 —— 一台处理器画**一整块**外框，
            // 宽高 = block.size * tilesize（2x2 逻辑处理器 16、3x3 超核 24），不再逐格画 8px；
            // 同台建筑只画一次（bld.id 去重，多格建筑以前会被画 4/9 遍）。
            // Building 的 x/y 就是建筑几何中心（Entityc），尺寸取 block.size。
            var seenH = {}, hx, hy;
            for(hx = r.x1; hx <= r.x2; hx++){
                for(hy = r.y1; hy <= r.y2; hy++){
                    var t = null;
                    try{ t = Vars.world.tile(hx, hy); }catch(e0){ t = null; }
                    if(t === null || t === undefined){ continue; }
                    var b = null;
                    try{ b = t.build; }catch(e1){ b = null; }
                    if(b === null || b === undefined){ continue; }
                    if(!isLogicBuild(b)){ continue; }
                    var hid = null;
                    try{ hid = String(b.id); }catch(e2){ hid = null; }
                    if(hid === null || hid === "undefined"){ hid = "t" + hx + "," + hy; }
                    if(seenH[hid] === true){ continue; }
                    seenH[hid] = true;
                    var bsize = 1;
                    try{ bsize = Number(b.block.size); }catch(e3){ bsize = 1; }
                    if(!(bsize > 0)){ bsize = 1; }
                    var bx = Number(b.x), by = Number(b.y);
                    L.rect(bx - bsize * 4, by - bsize * 4, bsize * 8, bsize * 8);
                }
            }
            D.color();
        }

        // 原版还在光标旁写「宽x高 (面积)」（InputHandler.java:1772-1794）：
        // Fonts.outline + 1/camerascale 缩放 + Draw.z(Layer.endPixeled)，画完逐项还原。
        // 字是附加品：任何一步抛异常只丢字，方框照画。
        function drawSizeText(r){
            try{
                // Fonts 是 mindustry.ui.Fonts（不是 gen 包：out/_server160.jar 里只有
                // mindustry/ui/Fonts.class，写错包名会被 catch 吞掉、字永远画不出来）。
                var font = Packages.mindustry.ui.Fonts.outline;
                if(font === null || font === undefined){ return; }
                var D = Packages.arc.graphics.g2d.Draw;
                var scale = 1;
                try{ scale = Vars.renderer.camerascale; }catch(e0){ }
                if(!(scale > 0)){ scale = 1; }
                var z = D.z();
                D.z(Packages.mindustry.core.Layer.endPixeled);
                font.getData().setScale(1 / scale);
                font.setColor(Pal.accent);
                var off = 5 / scale * 8;
                font.draw(r.w + "x" + r.h + " (" + (r.w * r.h) + ")",
                          Core.input.mouseWorldX() + off, Core.input.mouseWorldY() + off);
                font.setColor(Packages.arc.graphics.Color.white);
                font.getData().setScale(1);
                D.z(z);
            }catch(e){ /* 字画不出来只丢字 */ }
        }

        function regionSummary(snap){
            var b = snap.bounds, out = [];
            out.push("区域 " + b.x1 + "," + b.y1 + " - " + b.x2 + "," + b.y2
                     + "   " + (b.x2 - b.x1 + 1) + "×" + (b.y2 - b.y1 + 1) + " 格");
            out.push("方块 " + snap.cells + (snap.truncated ? "（已截断）" : "")
                     + "   处理器 " + snap.processors.length + "   地图 " + snap.map);
            var ps = snap.processors || [], i;
            for(i = 0; i < ps.length; i++){
                out.push("  @" + ps[i].x + "," + ps[i].y + " " + ps[i].block
                         + "   链接 " + ps[i].links.length
                         + (ps[i].links.length ? "：" + ps[i].links.join(", ") : ""));
            }
            return out.join("\n");
        }

        function popup(title, text){
            if(Vars.ui === null || Vars.ui === undefined){ return; }
            try{ Vars.ui.showText(title, String(text)); }catch(e){ }
        }

        // 非阻塞提示。**框选绝不能用 showText**：Dialog 会把鼠标吃掉（Core.scene.hasDialog() 为真，
        // 世界收不到点击），于是「按 F3 弹出提示」= 拖不动 —— 用户 2026-10-30 报的就是这个。
        // showInfoFade 是 Touchable.disabled 的淡出标签（Mindustry-Source/core/src/mindustry/core/UI.java:373），不挡输入。
        // ---------- D2（v0.15.1）：最后一条提示 -------------------------------
        function hintLineLabel(){
            var t = String(LAST_HINT.text || "");
            if(t.length === 0){ return "（还没有提示）"; }
            if(t.length > 46){ t = t.substring(0, 46) + "…"; }
            return t;
        }

        function lastHintLblRefresh(){
            // arc 的 Table.button() 回的是 Cell，Cell.get() 才是那个 TextButton；无头模拟器里
            // 这个桩没有 get()，所以每一层都先问类型再调，缺一层就安静跳过（真机不受影响）。
            try{
                if(lastHintLbl === null || lastHintLbl === undefined){ return; }
                if(typeof lastHintLbl.get !== "function"){ return; }
                var box = lastHintLbl.get();
                if(box === null || box === undefined || typeof box.setText !== "function"){ return; }
                box.setText(hintLineLabel());
            }catch(e){ log("hint label refresh skipped: " + e); }
            // v0.18.1：日志那一段也要跟着换（设置页开着的时候新提示要立刻出现）。
            try{
                if(lastHintLog !== null && lastHintLog !== undefined
                   && typeof lastHintLog.setText === "function"){
                    lastHintLog.setText(hintLogText());
                }
            }catch(e2){ log("hint log refresh skipped: " + e2); }
        }

        function hintLineRefresh(){ lastHintLblRefresh(); }

        function hintCopy(){
            var t = String(LAST_HINT.text || "");
            if(t.length === 0){ hint("还没有提示可复制"); return "no hint"; }
            try{ Core.app.setClipboardText(t); }catch(e){ return "clipboard failed: " + e; }
            hint("已复制最近一条提示");
            return "copied:" + t.length;
        }

        function hintClear(){
            LAST_HINT.text = "";
            LAST_HINT.at = 0;
            return "hint cleared";
        }

        // ---------- D3（v0.15.1）：作业列表过滤 -------------------------------
        function managerFilterBtnText(){
            return managerFilter.onlyBad === true ? "只看失败：开" : "只看失败：关";
        }

        function jobHit(job){
            if(managerFilter.onlyBad === true){
                var st = String((job && job.state) || "");
                if(st !== "failed" && st !== "rejected" && st !== "cancelled"){ return false; }
            }
            var q = String(managerFilter.text || "").toLowerCase();
            if(q.length === 0){ return true; }
            var s = (String((job && job.id) || "") + " " + String((job && job.desc) || "") + " "
                     + String((job && job.state) || "") + " " + String((job && job.model) || "")).toLowerCase();
            return s.indexOf(q) >= 0;
        }

        function hint(text){
            LAST_HINT.text = String(text);        // v0.15.1：留最后一条，设置页能看能复制
            LAST_HINT.at = Packages.java.lang.System.currentTimeMillis();
            hintLogPush(LAST_HINT.at, String(text));   // v0.18.1：同时进「最近提示」文本日志
            lastHintLblRefresh();
            if(Vars.ui === null || Vars.ui === undefined){ return; }
            try{ Vars.ui.showInfoFade(String(text)); }catch(e){ }
        }

        // 松手之后走这里：先给区域摘要（不需要 sidecar），再把 JSON 交给 /snapshot 落盘。
        function dumpRegion(x1, y1, x2, y2){
            var snap = regionSnapshot(x1, y1, x2, y2);
            var head = regionSummary(snap);
            if(snap.processors.length === 0){
                popup("Mlog辅助 · 区域快照", head + "\n\n区域里没有处理器。");
                return head;
            }
            if(Vars.ui === null || Vars.ui === undefined){
                // 无头引擎（自检/测试）既没有界面也没有 sidecar：只把摘要交回去，不发请求。
                return head;
            }
            // 两次请求串起来（都是本机回环，几十毫秒）：先落盘，再要链接候选，最后开面板。
            // 顺序有意义：面板要同时显示「落盘到哪」和「候选链接」，缺一条就得等第二次回调。
            try{
                httpJson("POST", "/snapshot", JSON.stringify({snapshot: snap}), function(code, text){
                    var res = parse(text);
                    var line = (code === "200" && res !== null && res.ok === true)
                        ? "已落盘 " + res.path + "（" + res.bytes + " B）"
                        : "落盘失败：" + httpWhy(code, text);
                    httpJson("POST", "/region", JSON.stringify({snapshot: snap}), function(c2, t2){
                        var body = head + "\n" + line;
                        try{
                            showRegionPanel(snap, body, regionCandidatesText(c2 + "\n" + t2));
                        }catch(e){
                            log("region panel failed, falling back to text: " + e);
                            popup("Mlog辅助 · 区域快照", body + "\n\n" + regionCandidatesText(c2 + "\n" + t2));
                        }
                    });
                });
                return head + "\n（面板稍后弹出）";
            }catch(e){
                popup("Mlog辅助 · 区域快照", head + "\n\n落盘失败：" + e);
                return head + "\n（落盘失败：" + e + "）";
            }
        }

        function selFinish(){
            var r = selRelease();
            if(r === null){ return null; }
            // v0.12.5：松手只弹一个描述框（用户 2026-10-31：f3 后的窗口简化到只有一个描述框）。
            // 框住的处理器有几个就投几个作业；矩形留到这一批全部终态（drawSelection 负责继续画）。
            // 区域快照面板与 AI 面板还在，但只从控制台走（maiDump / maiAi），F3 不再弹它们。
            try{
                return openQuickJob(r);
            }catch(e){
                log("quick job dialog failed: " + e);
                hint("描述框打不开：" + e);
                return null;
            }
        }

        // F3 是按住式的（=鼠标左键），所以 maiSelect 不再“就绪”，只给提示。
        api.maiSelect = function(){
            hint("框选：按住 F3 拖出矩形，松开即出快照");
            return "按住 F3 拖出矩形";
        };
        api.maiSelState = function(){
            return JSON.stringify({holding: selHolding, dragging: selDragging,
                                   startX: selStartX, startY: selStartY,
                                   curX: selCurX, curY: selCurY});
        };
        api.maiSelPress = function(x, y){ return selPress(x, y); };
        api.maiSelMove = function(x, y){ return selMove(x, y); };
        api.maiSelRelease = function(){ var r = selRelease(); return r === null ? "" : JSON.stringify(r); };
        api.maiSelDraw = function(ax, ay, bx, by){ return JSON.stringify(selDrawArgs(selRect(ax, ay, bx, by))); };
        api.maiDump = function(x1, y1, x2, y2){ return dumpRegion(x1, y1, x2, y2); };
        api.maiSelRect = function(ax, ay, bx, by){ return JSON.stringify(selRect(ax, ay, bx, by)); };
        // v0.8.1：单格选中处理器 = 开 AI 面板；maiPick 把「这一格是不是处理器」暴露给无头测试。
        api.maiPick = function(x, y){ var b = processorAt(x, y); return b === null ? "" : (x + "," + y); };
        // v0.7.0 F2 处理器清单：maiList 返回文本摘要（无 UI 也能验），maiListRows 返回 JSON 行。
        api.maiList = function(){ return listText(); };
        api.maiListRows = function(){ return JSON.stringify(scanProcessors().rows); };
        api.maiListPanel = function(){ openListPanel(); return "opened"; };
        // v0.8.0 作业队列：投递 / 查 / 取消 / 同步跑完（同步版只给控制台与自检）
        api.maiSetBase = function(u){ base = String(u); return base; };
        api.maiJob = function(desc){
            var b = targetBuild();
            if(b === null){ return "光标下没有处理器"; }
            jobSubmit(b, String(desc), function(res, why){
                hint(res === null ? ("投递失败：" + why) : ("作业 " + res.id + " 已投递"));
            });
            return "已投递作业";
        };
        api.maiJobSync = function(desc, timeoutMs){
            var b = targetBuild();
            if(b === null){ return JSON.stringify({ok: false, text: "no processor"}); }
            return jobSync(b, String(desc), timeoutMs);
        };
        api.maiJobSyncAt = function(x, y, desc, timeoutMs){
            var b = buildAt(x, y);
            if(b === null){ return JSON.stringify({ok: false, text: "no processor at " + x + "," + y}); }
            return jobSync(b, String(desc), timeoutMs);
        };
        api.maiJobWrite = function(jobJson, x, y){
            var job = parse(String(jobJson));
            if(job === null){ return "job json parse failed"; }
            var b = buildAt(x, y);
            if(b === null){ return "no processor at " + x + "," + y; }
            var w = writeBuild(b, jobAsPlan(job));
            return w + " | " + String(b.code).replace(/\n/g, " / ");
        };
        // v0.10.1：写前冲突检测的无头探针。判定发生在 configure 之前，所以用假处理器
        // （只有 tile 与 code）就够，不会碰到真存档、也不会写撤销快照。
        api.maiConflictProbe = function(){
            var b = {tile: {x: 3, y: 4}, code: "set n 1\nend"};
            var k = procKey(b);
            JOB_ORIG[k] = "set n 0\nend";
            JOB_FORCE[k] = false;
            var r1 = conflictGate(b);
            var blocked = r1 !== null && String(r1).indexOf("已被改动") >= 0
                          && String(b.code) === "set n 1\nend";
            var r2 = conflictGate(b);
            var forced = r2 === null;                    // 第二次 = 确认覆盖
            var r3 = conflictGate(b);
            var once = r3 !== null;                      // 只放行一次，第三次又要问
            JOB_ORIG[k] = String(b.code);
            var quiet = conflictGate(b) === null;        // 代码没动过就不问
            forgetOrig(b);
            return (blocked && forced && once && quiet) ? "ok"
                 : ("bad blocked=" + blocked + " forced=" + forced
                    + " once=" + once + " quiet=" + quiet);
        };
        // v0.12.8：链接表冲突的无头探针。假处理器带一个假 links（size + get），
        // 覆盖四件事：没动过不问 / 拆掉一条要拦 / 拒绝一次后放行 / 同名换位置也算改。
        api.maiLinkConflictProbe = function(){
            function fakeLinks(list){
                return {size: list.length, get: function(i){ return list[i]; }};
            }
            var l1 = [{name: "conveyor1", x: 10, y: 4}, {name: "core1", x: 1, y: 4}];
            var b = {tile: {x: 3, y: 4}, code: "set n 1\nend", links: fakeLinks(l1)};
            rememberOrig(b);
            var quiet = conflictReason(b) === null;
            b.links = fakeLinks([{name: "conveyor1", x: 10, y: 4}]);      // 拆掉一条
            var r1 = conflictReason(b);
            var caught = r1 !== null && String(r1).indexOf("链接表") >= 0;
            var g1 = conflictGate(b);
            var g2 = conflictGate(b);
            var once = g1 !== null && g2 === null;                        // 拒绝一次、再点放行
            b.links = fakeLinks(l1);
            var again = conflictReason(b) === null;                       // 改回原样就不问
            b.links = fakeLinks([{name: "conveyor1", x: 11, y: 4}]);      // 同名不同位置也算改
            var moved = String(conflictReason(b)).indexOf("链接表") >= 0;
            b.code = "set n 2\nend";
            var bothWhy = String(conflictReason(b));
            var both = bothWhy.indexOf("代码") >= 0 && bothWhy.indexOf("链接表") >= 0;
            forgetOrig(b);
            var cleared = conflictReason(b) === null;
            return (quiet && caught && once && again && moved && both && cleared) ? "ok"
                 : ("bad quiet=" + quiet + " caught=" + caught + " once=" + once
                    + " again=" + again + " moved=" + moved + " both=" + both
                    + " cleared=" + cleared);
        };
        // v0.12.8：联机判定的无头探针。无头引擎里 Vars.net 是 null、Vars.player 也是 null，
        // 期望 net=false admin=false perm=null —— 单机路径一个闸都不该多。
        api.maiNetProbe = function(){
            var b = {tile: {x: 3, y: 4}, code: ""};
            var np = permWhy(b);
            return "net=" + netClient() + " admin=" + isAdmin()
                 + " player=" + (localPlayer() === null ? "null" : "some")
                 + " perm=" + String(np);
        };
        // v0.12.8：写后回读的队列探针（不碰 Vars.net）：塞一条必然对不上的，
        // 走三次 tick 之后应该自己出队（真机里这一步是弹「无权限」）。
        api.maiWriteCheckProbe = function(){
            var b = {tile: {x: 3, y: 4, build: null}, code: "set n 0\nend"};
            b.tile.build = b;
            if(!netClient()){
                writeCheckTick();
                return "offline queue=" + WRITE_CHECK.length;
            }
            armWriteCheck(b, "set n 1\nend", "probe");
            var n0 = WRITE_CHECK.length;
            WRITE_CHECK[WRITE_CHECK.length - 1].at = 0;      // 立刻到点
            for(var i = 0; i < WRITE_CHECK_TRIES + 1; i++){
                writeCheckTick();
                if(WRITE_CHECK.length > 0){ WRITE_CHECK[0].at = 0; }
            }
            return (n0 === 1 && WRITE_CHECK.length === 0) ? "ok" : ("bad queued=" + n0);
        };
        // v0.13.0 / v0.15.2：聊天播报的无头探针。只验行格式（不碰 Vars.ui / 聊天框）：
        // 尖括号必须在、:logic: 必须是原文、空信息不留冒号、状态在第二个尖括号里。
        api.maiChatProbe = function(){
            var a = chatLine("Generating", 12, 34, "给核心补铜（job 3f2a1b9c）");
            var b2 = chatLine("Done", 12, 34, "code · 12 条语句 · 240 字节 · 0 ERR");
            var c = chatLine("Failed", 12, 34, "模型没写出可用代码");
            var d = chatLine("Cancelled", 1, 2, "");
            var ok = a === "<MAI:logic:><Generating>(12,34):给核心补铜（job 3f2a1b9c）"
                  && b2 === "<MAI:logic:><Done>(12,34):code · 12 条语句 · 240 字节 · 0 ERR"
                  && c === "<MAI:logic:><Failed>(12,34):模型没写出可用代码"
                  && d === "<MAI:logic:><Cancelled>(1,2)";
            var pretty = chatPretty(a);
            return (ok ? "ok" : "bad")
                 + " pretty=" + (pretty.indexOf("<MAI") === 0 ? "yes" : "no")
                 + " start=" + a + " cancel=" + d;
        };

        // v0.13.1：自动拉起 sidecar 的无头探针。无头环境里 Vars.mods 有、exe 没有，
        // 所以只验同步的那部分：路径解析与端口解析不炸、找不到 exe 时 exe/dir 一起为 null。
        // 状态机本身是异步的（httpJson 的回调里才落 noexe），不在这里断言。
        api.maiSidecarProbe = function(){
            var exe = sidecarExe();
            var dir = sidecarDir();
            var port = sidecarPort();
            var before = SIDE.state;
            ensureSidecar(true);            // 找不到 exe 应安静地落到 noexe，不该抛
            var after = SIDE.state;
            var ok = before === "idle"
                  && (exe === null ? dir === null : dir !== null)
                  && typeof port === "number" && port > 0;
            return (ok ? "ok" : "bad") + " exe=" + (exe === null ? "none" : "yes")
                 + " dir=" + (dir === null ? "none" : "yes")
                 + " port=" + port + " state=" + before + "->" + after;
        };

        // v0.12.0：事务写入的无头探针。现放一台真处理器（结束时拆掉），一次整批写、
        // 一次整批撤销，再一次「第二台 configure 抛错」——三件事全对才返回 ok。
        api.maiWriteManyProbe = function(aJson, x, y){
            var b1 = buildAt(x, y);
            if(b1 === null){ return "no processor at " + x + "," + y; }
            var j1 = parse(String(aJson));
            if(j1 === null || j1.code === undefined){ return "job json parse failed"; }
            var r1 = jobAsPlan(j1);
            if(r1.ok !== true){ return "the job is not done"; }
            var W = Packages.mindustry.Vars.world, T = Packages.mindustry.game.Team.sharded;
            var x2 = x + 3, y2 = y + 3;
            if(x2 > W.width() - 2){ x2 = x - 3; }
            if(y2 > W.height() - 2){ y2 = y - 3; }
            var t2 = W.tile(x2, y2);
            t2.setBlock(Packages.mindustry.Vars.content.block("air"), T);
            t2.setBlock(Packages.mindustry.Vars.content.block("micro-processor"), T);
            var b2 = buildAt(x2, y2);
            if(b2 === null){ return "the second processor did not appear at " + x2 + "," + y2; }
            var before1 = String(b1.code), before2 = String(b2.code);
            var w = writeMany([{b: b1, res: r1}, {b: b2, res: r1}], "probe");
            var wrote = String(b1.code) === String(j1.code) && String(b2.code) === String(j1.code);
            var u = undo();
            var undone = String(b1.code) === before1 && String(b2.code) === before2;
            // 故意失败：第二台是假 build，configure 直接抛；第一台必须被回滚。
            try{ delete JOB_ORIG[x2 + "," + y2]; }catch(e8){ }
            var fake = {tile: {x: x2, y: y2}, code: "fake",
                        configure: function(){ throw "boom"; },
                        updateCode: function(c){ this.code = String(c); }};
            var keep = String(b1.code);
            var wf = writeMany([{b: b1, res: r1}, {b: fake, res: r1}], "probe-fail");
            var rolled = String(b1.code) === keep && String(wf).indexOf("已回滚 1 台") >= 0;
            try{ t2.setBlock(Packages.mindustry.Vars.content.block("air"), T); }catch(e0){ }
            var ok = wrote && undone && rolled && String(w).indexOf("已写入 2 台") >= 0;
            return ok ? ("ok " + w + " | " + u)
                      : ("bad wrote=" + wrote + " undone=" + undone + " rolled=" + rolled
                         + " w=" + w + " u=" + u + " wf=" + wf);
        };
        api.maiJobProgress = function(jobJson){ return jobProgressText(parse(String(jobJson))); };
        // v0.12.4：作业里带回的模型思考原文（控制台/自检用；面板显示的是它的开头）。
        api.maiJobReasoning = function(jobJson){ return jobReasoningText(parse(String(jobJson))); };
        // v0.13.2：作业视图新摊开的两段——模型的 assumptions 与 cost 报告
        // （cost 里带 UNREACHABLE 段落）。无头环境没有真 Table，所以探针只调
        // 取行函数本身：行数对、内容非空，就算这两段接上了。
        api.maiJobPanelProbe = function(jobJson){
            var job = parse(String(jobJson));
            var as = jobAssumptionLines(job, 4, 88), co = jobCostLines(job, 8);
            var ok = as.length === 2 && co.length === 3 && co[0].indexOf("UNREACHABLE") >= 0;
            return ok ? ("ok assumptions=" + as.length + " cost=" + co.length
                         + " first=" + as[0])
                      : ("bad assumptions=" + as.length + " cost=" + co.length);
        };
        // v0.18.4（用户 m13036）：思考全文的无头探针。Dialog 在无头环境开不了，
        // 所以只验「取行」这一层：全量文本长度、按 60 字切出来的行数与首行宽度。
        api.maiJobThinkProbe = function(jobJson){
            var job = parse(String(jobJson));
            var all = jobThinkAll(job), rows = jobReasoningLines(job, 100000, 60);
            var ok = all.length === 80 && rows.length === 2 && String(rows[0]).length === 60;
            return ok ? ("ok len=" + all.length + " rows=" + rows.length
                         + " w0=" + String(rows[0]).length)
                      : ("bad len=" + all.length + " rows=" + rows.length);
        };
        // v0.14.0：工作区数据层的无头探针。无头模拟器里 Vars.dataDirectory 可能是 null，
        // 所以把三个存储原语换成内存表，验的是「逻辑」而不是磁盘：
        // 落盘/读回、复用判定（中心在框内 + 地图一致）、归档后不复用、恢复、释放后文件真的没了。
        api.maiWorkspaceProbe = function(){
            var notes = [], savedMem = wsMem, savedCur = wsCurrent, savedCache = wsCache;
            wsMem = {};
            wsCache = {};
            try{
                var r = {x1: 40, y1: 40, x2: 46, y2: 46};
                var ws = wsNew(r, "probe");
                ws.notes = "probe-notes";
                var fake = {tile: {x: 41, y: 42}, code: "set n 1\nend", links: null};
                wsTouchProc(ws, fake, "探测用描述");
                if(wsSave(ws) === null){ notes.push("save"); }
                var back = wsLoad(ws.id);
                if(back === null || String(back.notes) !== "probe-notes"){ notes.push("roundtrip"); }
                var procs = (back === null ? [] : (back.procs || []));
                if(procs.length !== 1 || String(procs[0].desc) !== "探测用描述"){ notes.push("procs"); }
                if(wsFindActive(r) !== String(ws.id)){ notes.push("reuse"); }
                if(wsFindActive({x1: 400, y1: 400, x2: 402, y2: 402}) !== null){ notes.push("noreuse"); }
                wsRecordJob(ws, "job-1", "探测用描述");
                var jobs = ((wsLoad(ws.id) || {}).jobs) || [];
                if(jobs.length !== 1 || String(jobs[0].id) !== "job-1"){ notes.push("job"); }
                wsArchive(ws.id);
                if(wsFindActive(r) !== null){ notes.push("archived"); }
                if(String((wsLoad(ws.id) || {}).state) !== "archived"){ notes.push("state"); }
                wsRestore(ws.id);
                if(wsFindActive(r) !== String(ws.id)){ notes.push("restore"); }
                wsRelease(ws.id);
                if(wsLoad(ws.id) !== null){ notes.push("release-load"); }
                if(wsFileNames().length !== 0){ notes.push("release-file"); }
            }catch(e){
                notes.push("throw:" + e);
            }
            wsMem = savedMem;
            wsCache = savedCache;
            wsCurrent = savedCur;
            var dir = "no";
            try{ if(Vars.dataDirectory !== null && Vars.dataDirectory !== undefined){ dir = "yes"; } }catch(e2){ dir = "no"; }
            return (notes.length === 0 ? "ok" : ("bad " + notes.join(","))) + " dir=" + dir + " mem=yes";
        };
        api.maiJobCancel = function(){ return cancelJob(); };
        // v0.14.0：工作区数据层自检口（实现在工作区段里，这里转发）
        api.maiWorkspaceList = function(){ return JSON.stringify(wsList()); };
        api.maiWorkspaceArchive = function(id){ return wsArchive(String(id)); };
        api.maiWorkspaceRestore = function(id){ return wsRestore(String(id)); };
        api.maiWorkspaceRelease = function(id){ return wsRelease(String(id)); };
        api.maiWorkspaceCurrent = function(){ return wsCurrent === null ? "" : String(wsCurrent); };
        api.maiJobState = function(){
            return aiJob === null ? "" : JSON.stringify({id: aiJob.id, desc: aiJob.desc});
        };
        // v0.12.5：作业框 / 徽标 / 作业管理窗
        api.maiJobBox = function(){
            var s = jobBoxState();
            if(s === null){ return JSON.stringify({box: false}); }
            return JSON.stringify({box: true, r: jobBox.r, ws: jobBox.ws || null, ids: jobBox.ids,
                                   total: s.total, terminal: s.terminal,
                                   done: s.done, failed: s.failed,
                                   linger: jobBox.termAt > 0, written: (jobBox.written || 0),
                                   badge: jobBadgeText()});
        };
        api.maiJobBoxClear = function(){ jobBox = null; jobBadge = null; return "cleared"; };
        // v0.12.7：框只该在「写入」或「主动关」时消失 —— 这个口子就是「主动关」。
        api.maiJobBoxClose = function(){ return closeJobBox("closed"); };
        // 自检口：状态行 + 两个按钮的几何与框右下角的关系（无头引擎里 renderer 可能是 null，按 scale 1 算）。
        api.maiBadgeRect = function(){
            var a = selDrawArgs({x1: 1, y1: 1, x2: 6, y2: 6, w: 6, h: 6});
            var m = jobButtonLayout(a);
            if(m === null){ return JSON.stringify({rect: false}); }
            return JSON.stringify({rect: true, scale: m.scale, box: a, right: m.right, top: m.top,
                                   status: m.status, buttons: m.buttons, text: m.text,
                                   x: m.status.x, y: m.status.y, w: m.status.w, h: m.status.h});
        };
        // 自检口：不碰 sidecar 也能把「框留多久 / 徽标写什么」这套状态机跑一遍。
        api.maiJobBoxPin = function(idsJson, statesJson){
            var ids = parse(String(idsJson)) || [], states = parse(String(statesJson)) || [];
            var box = {r: {x1: 1, y1: 1, x2: 6, y2: 6, w: 6, h: 6}, ids: [], jobs: {}, pend: 0,
                       started: 0, termAt: 0, lastPoll: 0};
            for(var i = 0; i < ids.length; i++){
                var jid = String(ids[i]);
                box.ids.push(jid);
                box.jobs[jid] = {id: jid, state: String(states[i] || 'queued'), desc: 'x'};
            }
            jobBox = box;
            jobBoxFinishIfDone();
            return api.maiJobBox();
        };
        api.maiJobBadge = function(){ return jobBadgeText(); };
        api.maiJobManager = function(){ return openJobManager(); };
        api.maiWorkspaceDialog = function(){ return openWorkspaceDialog(); };
        api.maiWorkspaceExport = function(id){ return wsExportText(String(id)); };
        api.maiWorkspaceExportDisk = function(id){ return wsExportToDisk(String(id)); };
        api.maiWorkspaceImport = function(text){ return wsImportText(String(text)); };
        api.maiWorkspaceRestoreBox = function(id){ return wsRestoreBox(String(id)); };
        api.maiWorkspaceRename = function(id, name){ return wsRename(String(id), String(name)); };
        api.maiWorkspaceImprove = function(id, desc){ return wsImproveBatch(String(id), String(desc || "")); };
        api.maiWorkspaceImprovePayload = function(id, x, y){
            var ws = wsLoad(String(id));
            var b = buildAt(Number(x), Number(y));
            if(ws === null || b === null){ return ""; }
            var p = wsImprovePayload(ws, b);
            return p === null ? "" : JSON.stringify(p);
        };
        api.maiWorkspaceRowLabel = function(id){
            var ws = wsLoad(String(id));
            return ws === null ? "" : wsRowLabel(ws);
        };
        api.maiBlueprintList = function(){ return JSON.stringify(blueprintList(24)); };
        api.maiWorkspaceFromBlueprint = function(name){ return wsFromBlueprintName(String(name)); };
        // v0.14.1：工作区界面 / 继续改进 / 导入导出 / 蓝图入口的自检口（只跑纯函数，不建窗口）
        api.maiWorkspace2Probe = function(){
            var savedMem = wsMem, savedCache = wsCache, savedCur = wsCurrent;
            var notes = [];
            try{
                wsMem = {}; wsCache = {}; wsCurrent = null;
                var r = {x1: 10, y1: 10, x2: 16, y2: 14};
                var ws = wsNew(r, "探测工作区");
                var fake = {tile: {x: 12, y: 11}, code: "set n 1\njump 0 always\n",
                            links: {size: 0, get: function(i){ return null; }}};
                wsTouchProc(ws, fake, "把 n 设成 1");
                wsSave(ws);
                if(wsRowLabel(ws).indexOf("探测工作区") < 0){ notes.push("rowlabel"); }
                if(wsBoundsText(ws).indexOf("10") < 0){ notes.push("bounds"); }
                if(wsProcLines(ws, 4).length === 0){ notes.push("proclines"); }
                if(wsJobLines(ws, 3).length === 0){ notes.push("joblines"); }
                var ex = wsExportText(ws.id);
                if(ex.length === 0 || ex.indexOf("探测工作区") < 0){ notes.push("export"); }
                var id2 = wsImportText(ex);
                if(String(id2).length === 0){ notes.push("import"); }
                if(String(id2) === String(ws.id)){ notes.push("import-id"); }
                var ws2 = wsLoad(id2);
                if(ws2 === null || (ws2.procs || []).length !== 1){ notes.push("import-procs"); }
                if(wsRename(id2, "改名后").indexOf("改名后") < 0){ notes.push("rename"); }
                var payload = wsImprovePayload(ws2, fake);
                if(payload === null){ notes.push("payload"); }
                else{
                    if(String(payload.prevCode).indexOf("set n 1") < 0){ notes.push("payload-code"); }
                    if(String(payload.id) !== String(id2)){ notes.push("payload-id"); }
                }
                var msg = wsRestoreBox(ws.id);
                if(jobBox === null || String(jobBox.ws) !== String(ws.id)){ notes.push("restorebox"); }
                if((jobBox === null ? [] : jobBox.ids).length !== 0){ notes.push("restorebox-ids"); }
                if(msg.indexOf("已恢复") < 0){ notes.push("restorebox-msg"); }
                jobBox = null; jobBadge = null;
                var bp = blueprintList(4);
                if(bp === null || bp.length === undefined){ notes.push("blueprint"); }
                if(wsFromBlueprintName("绝对没有这个蓝图").indexOf("找不到") < 0){ notes.push("blueprint-miss"); }
                if(wsImproveBatch("没有这个工作区", "") !== "工作区不存在"){ notes.push("improve-miss"); }
            }catch(e){
                notes.push("throw:" + e);
            }
            jobBox = null; jobBadge = null;
            wsMem = savedMem; wsCache = savedCache; wsCurrent = savedCur;
            return notes.length === 0 ? "ok" : ("bad " + notes.join(","));
        };

        // v0.19.0：工作区界面（列表过滤/排序、详情模型、世界细框、释放确认）的自检口。
        // 只跑纯函数，不建窗口（无头环境 Vars.ui === null 也能过）。
        api.maiWorkspace3Probe = function(){
            var notes = [];
            var mk = function(name, map, state, nproc, njob){
                var procs = [], jobs = [], i;
                for(i = 0; i < nproc; i++){
                    procs.push({x: 10 + i, y: 10, block: "processor",
                                code: "set n 1\njump 0 always", desc: "把 n 设成 1", accepted: 1});
                }
                for(i = 0; i < njob; i++){
                    jobs.push({id: "j" + String(i), state: "done", desc: "把 n 设成 " + String(i), at: 1000 + i});
                }
                return {id: name, name: name, map: map, state: state, notes: "",
                        created: 1000, updated: 2000 + nproc,
                        bounds: {x1: 10, y1: 10, x2: 16, y2: 14},
                        procs: procs, jobs: jobs};
            };
            var counts = "";
            try{
                var a = mk("甲", "A", "active", 1, 1);
                var b = mk("乙", "A", "archived", 2, 0);
                var c = mk("丙", "B", "active", 3, 2);
                var rows = [a, b, c];
                if(wsStatusOf(a, "A") !== "active"){ notes.push("status-active"); }
                if(wsStatusOf(b, "A") !== "archived"){ notes.push("status-archived"); }
                if(wsStatusOf(c, "A") !== "mismatch"){ notes.push("status-mismatch"); }
                if(wsStatusOf(c, "?") !== "active"){ notes.push("status-unknown-map"); }
                if(wsStatusCn("mismatch") !== "地图不符"){ notes.push("status-cn"); }
                if(wsRowParts(a, "A").sub.indexOf("台") < 0){ notes.push("rowparts"); }
                var act = wsFilterList(rows, {q: "", filter: "active", map: "A"});
                if(act.length !== 1){ notes.push("filter-active"); }
                var mm = wsFilterList(rows, {q: "", filter: "mismatch", map: "A"});
                if(mm.length !== 1 || String(mm[0].id) !== "丙"){ notes.push("filter-mismatch"); }
                var hit = wsFilterList(rows, {q: "把 n 设成 1", filter: "all", map: "A"});
                if(hit.length < 1){ notes.push("filter-q"); }
                var srt = wsSortList(rows, "name");
                if(String(srt[0].name) !== "丙" && String(srt[0].name) !== "乙" && String(srt[0].name) !== "甲"){
                    notes.push("sort-name");
                }
                if((wsSortList(rows, "procs")[0].procs || []).length !== 3){ notes.push("sort-procs"); }
                if(String(wsSortList(rows, "updated")[0].id) !== String(c.id)){ notes.push("sort-updated"); }
                if(wsNextSort("updated") !== "name" || wsNextSort("procs") !== "updated"){
                    notes.push("next-sort");
                }
                var m = wsDetailModel(a, "A");
                if(m.size !== "7×5"){ notes.push("detail-size"); }
                if(m.procs.length !== 1 || m.jobs.length !== 1){ notes.push("detail-rows"); }
                if(m.boundsLine.indexOf("地图 A") < 0){ notes.push("detail-bounds"); }
                if(m.canLocate !== true){ notes.push("detail-locate"); }
                var m2 = wsDetailModel(c, "A");
                if(m2.canLocate !== false || m2.stateCn !== "地图不符"){ notes.push("detail-mismatch"); }
                var big = wsDetailModel(mk("大", "A", "active", 15, 9), "A");
                if(big.procs.length !== 12 || big.jobs.length !== 6){ notes.push("detail-cap"); }
                if(big.procTotal !== 15 || big.jobTotal !== 9){ notes.push("detail-total"); }
                var gh = wsGhostRects(rows, "A");
                if(gh.length !== 1 || String(gh[0].id) !== "甲"){ notes.push("ghosts"); }
                if(gh.length > 0 && (gh[0].x1 !== 10 || gh[0].y2 !== 14)){ notes.push("ghost-bounds"); }
                var rl = wsReleaseConfirmText(a);
                if(rl.indexOf("释放") < 0 || rl.indexOf("不能恢复") < 0){ notes.push("release-text"); }
                if(wsCanLocate(c, "A") !== false || wsCanLocate(a, "A") !== true){ notes.push("can-locate"); }
                if(String(wsLocate(null)) !== "没有这个工作区"){ notes.push("locate-null"); }
                // v0.19.2：排版尺寸（用户截图「窗口宽度不够」的那条线）也被断言，改宽度时不会静默退化。
                var plan = wsDialogPlan(1366, 768);
                if(plan.wide !== true){ notes.push("plan-wide"); }
                // v0.19.5：左栏收窄到 30%（1080 → 324），控件按文字宽度，不再铺满整行。
                if(plan.listW < 300 || plan.listW > 360){ notes.push("plan-min"); }
                if(plan.detailW < 300){ notes.push("plan-detail-min"); }
                if(plan.listW !== 324){ notes.push("plan-listw"); }
                if(plan.availW !== 1080){ notes.push("plan-avail"); }
                // v0.19.4：窗口宽度有上限，不再顶满屏幕（用户 m01690 的「按钮被拉伸」）。
                if(plan.availW > 1080){ notes.push("plan-cap"); }
                if(wsChipWidth(1200) !== 96){ notes.push("chip-cap"); }
                if(wsChipWidth(200) !== 56){ notes.push("chip-min"); }
                if(wsPrimaryWidth(1200) !== 168){ notes.push("primary-cap"); }
                if(wsPrimaryWidth(120) !== 100){ notes.push("primary-min"); }
                if(plan.listW + plan.detailW + 14 > plan.availW){ notes.push("plan-fit"); }
                if(wsDialogPlan(880, 600).wide !== false){ notes.push("plan-narrow"); }
                if(wsDialogPlan(900, 600).wide !== true){ notes.push("plan-wide-edge"); }
                if(wsDialogPlan(0, 0).availW !== 1080){ notes.push("plan-fallback"); }
                // v0.19.3：像素 → scene 单位的换算（用户 m01237 的「按钮太宽 / 右栏被挤出屏幕」）。
                if(wsSceneUnits(2049, 1.5) !== 1366){ notes.push("scene-units"); }
                if(wsSceneUnits(0, 1.5) !== 0){ notes.push("scene-units-zero"); }
                if(wsSclFactor() <= 0){ notes.push("scene-factor"); }
                var eq = wsDialogPlan(wsSceneUnits(2049, 1.5), wsSceneUnits(1152, 1.5));
                if(eq.availW !== plan.availW || eq.listW !== plan.listW){ notes.push("scene-equiv"); }
                if(wsFilterCn("all") !== "全部" || wsFilterCn("mismatch") !== "地图不符"){ notes.push("filter-cn"); }
                counts = "rows=" + String(rows.length) + " filt=" + String(act.length + mm.length)
                       + " sort=" + String(srt.length) + " detail=" + String(m.procs.length + m.jobs.length)
                       + " ghosts=" + String(gh.length) + " release=" + String(rl.length > 0 ? 1 : 0)
                       + " mismatch=" + String(m2.canLocate === false ? 1 : 0)
                       + " locate=" + String(wsCanLocate(a, "A") ? 1 : 0)
                       + " plan=" + String(plan.listW) + "x" + String(plan.detailW)
                       + " cap=" + String(wsChipWidth(1200)) + "/" + String(wsPrimaryWidth(1200))
                       + " scl=" + String(wsSclFactor() > 0 ? 1 : 0);
            }catch(e){
                notes.push("throw:" + e);
            }
            return notes.length === 0 ? ("ok " + counts) : ("bad " + notes.join(","));
        };


        // 挂到脚本作用域（控制台里能直接敲），并读回自检
        // v0.14.4（用户 2026-11-01）：「sidcar应该随游戏启动而启动」——不再等 F3 才拉；
        // 这里先试一次，ClientLoadEvent（进游戏）再补一次，两次都幂等。
        try{ ensureSidecar(true); }catch(e){ log("sidecar autostart skipped at load: " + e); }
        // 载入时就试着把设置里的模型档推给 sidecar（重启过的 sidecar 会忘）。
        try{ llmSync(); }catch(e){ log("llm sync skipped at load"); }
        var scope = Vars.mods.getScripts().scope;
        var mounted = 0;
        for(var k in api){
            Packages.rhino.ScriptableObject.putProperty(scope, k, api[k]);
            // 读回不能用 scope.get(k, scope)：某些加载路径下 scope 是 global 包装对象，
            // 没有可调用的 get 方法（真 v160.7 实测报 "Cannot find function get"）。
            try{
                if(Packages.rhino.ScriptableObject.getProperty(scope, k) !== undefined){ mounted++; }
            }catch(e){
                mounted++;   // putProperty 成功即可，读回是自检不是必需
            }
        }
        log("v" + MOD_VERSION + " ready, " + mounted + "/" + Object.keys(api).length
            + " console function(s), sidecar=" + base);

        // 脚本加载时 UI 可能还没建；ClientLoadEvent 再补一次。
        var settingsReady = registerSettings();
        try{
            Events.on(ET.ClientLoadEvent, function(){
                try{ if(!settingsReady){ settingsReady = registerSettings(); } }catch(e){ /* ignore */ }
                // v0.14.4：「随游戏启动」——进游戏再拉一次（脚本加载时可能在主菜单）。
                try{ ensureSidecar(true); }catch(e2){ log("sidecar autostart skipped: " + e2); }
            });
        }catch(e){ log("ClientLoadEvent hook failed: " + e); }

        // ---------------------------------------------------------- 热键
        {   // v0.9.0: the F4 hotkey is gone, so the update hook is unconditional
            Events.run(ET.Trigger.update, function(){
                try{
                    reviewTick();   // 原版逻辑编辑器开着就顺手刷新那条审查栏
                    jobTick();      // v0.8.0：有作业在跑就每秒查一次状态
                    jobBoxTick();   // v0.12.5：框选框里那一批作业的状态
                    writeCheckTick(); // v0.12.8：联机里回读写过的处理器，确认服务端真收下了
                    sidecarTick();  // v0.13.1：自己拉起来的 sidecar 要等它开口
                    sidecarPing();  // v0.15.0：心跳，sidecar 收不到 45 s 就自己退出
                    jobButtonsClick(); // v0.14.2：框右下角的「查看 / 取消」两个按钮
                    blueprintTick(); // v0.16.1：蓝图页入口、「详细」页续聊、蓝图对话轮询
                    // 「设置 → Mlog辅助」这一栏只在 Vars.ui 建好之后能注册。脚本加载时
                    // UI 往往还没建（真机日志里是在 ClientLoadEvent 前），所以这里补一次
                    // 有界重试：每 60 帧试一次、最多 600 次（≈10 s @60fps），成功即停。
                    if(!settingsDone && settingsTries < 600){
                        settingsTries++;
                        if(settingsTries % 60 === 0){ registerSettings(); }
                    }
                    if(Vars.state.isMenu()){ return; }
                    welcomeTick();
                    hudSliderTick();  // v0.18.1：滑条没有 changed 回调，每帧对一次读数
                    if(Vars.ui.chatfrag.shown()){ return; }
                    selKeyBind();
                    listKeyBind();                     // v0.17.1：确保框选键已注册进原版按键绑定
                    if(hotTap(KEY_REGION[0])){
                        if(!aiShown() && Core.scene !== null && Core.scene.hasDialog()){ return; }
                        var rb = targetBuild();
                        if(rb === null){
                            Vars.ui.showText("Mlog辅助 · 链接候选",
                                             "光标下没有处理器：先把光标移到处理器上再按 F7");
                        }else{
                            var rr = 10;
                            // 异步：回调由 httpJson 经 Core.app.post 投递回主线程，
                            // 在这里 busy-wait 只会等到超时（v0.4.2 就是这样弹「sidecar 不可用（）」）。
                            regionAroundBox(rb.tile.x - rr, rb.tile.y - rr,
                                            rb.tile.x + rr, rb.tile.y + rr);
                        }
                    }
                    if(listKeyTap()){
                        if(!aiShown() && Core.scene !== null && Core.scene.hasDialog()){ return; }
                        openListPanel();
                    }
                    // F3 = 鼠标左键：按住就拉框（起点=按下那一刻的鼠标格），松开就出快照。
                    // 没有「就绪」态、不需要点左键，也不弹任何窗（弹窗会抢鼠标）。
                    if(KEY_SELECT !== null){
                        var selKey = selKeyCode();
                        var selDown = selKeyDown();
                        if(selDown && !selHolding){
                            if(Core.scene === null || !Core.scene.hasDialog()){
                                var s0 = mouseTile();
                                if(s0 !== null){ selPress(s0.x, s0.y); }
                            }
                        }else if(selDown && selDragging){
                            var s1 = mouseTile();
                            if(s1 !== null){ selMove(s1.x, s1.y); }
                        }else if(!selDown && selHolding){
                            selFinish();
                        }
                    }
                }catch(e){ /* 每帧回调里绝不抛出去 */ }
            });
        }

        // 拖拽高亮：画在世界坐标系里，不碰场景树（画不出来也不影响选择本身）。
        try{
            Events.run(ET.Trigger.draw, function(){
                try{ drawSelection(); }catch(e){ /* 每帧回调里绝不抛出去 */ }
                // v0.19.0：工作区窗口开着时，世界层补画一层细框（只画进行中且地图一致的）。
                try{ drawWorkspaceGhosts(); }catch(e2){ /* 同上 */ }
                // v0.19.5：布局跑完之后 dump 一次真实宽度（只 dump 一次，见 wsGeomDump）。
                try{ wsGeomDump(); }catch(e3){ /* 同上 */ }
            });
        }catch(e){ log("Trigger.draw hook failed: " + e); }

    }catch(e){
        print("[mai] init failed: " + e);
    }
}

try{ __logicWriterMain(); }catch(e){ print("[mai] bootstrap failed: " + e); }
