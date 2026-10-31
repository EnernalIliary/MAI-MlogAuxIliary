// Mlog辅助 · 本地快检（纯函数，无游戏依赖）
//
// 游戏里由 MlogAuxiliary/scripts/main.js 调用；测试里由 tests/mlogauxiliary/harness.js
// 在 node 下调用。数据表 MAI_RULES 由 tools/rules_export.py 生成（别手改）。
//
// 边界（见 docs/Mlog辅助-规划.md 2.2）：只做「单行 + 一张数据表」就能定的事实。
// 任何需要控制流/数据流推理的判据都属于 sidecar 的全量检查（tools/mlogcheck.py
// 41 条规则），这里不重写 —— 两份实现必然漂移，而漂移的检查器比没有检查器更糟。
//
// 因此本文件的五条判据是：未知指令 / 子命令拼错 / 版本门 / 链接名未绑定 / 体积。
// 未赋值变量的槽位分级、cost_report 的 CFG 最长路径、值域、跨处理器 pin 流：不做。

var MAI_QUICKCHECK = (function(){
    "use strict";

    // Rhino 是 ES5，这里只用最老的 API；反斜杠一律用 charCode 拼，省得被转义吃掉。
    var BS = String.fromCharCode(92);
    var NL = String.fromCharCode(10);
    var CR = String.fromCharCode(13);
    var TAB = String.fromCharCode(9);
    var SP = " ";

    // 不要用 String.prototype.split(正则)。真 v160.7 的 Rhino 里，带 + 量词的正则
    // （/\s+/、/[ ]+/、new RegExp("\s+")）在 split 里等价于空匹配，会把整行拆成
    // 单个字符 —— 每条语句的首 token 就变成第一个字母。见 tests/mlogauxiliary 的
    // rhino 回归（out/takeover-20261024/37-*.md）。这里全部手写扫描。
    function isWs(ch){ return ch === SP || ch === TAB || ch === CR; }

    function isNumber(s){
        if(!s) return false;
        if(s.charAt(0) === "-" || s.charAt(0) === "+") s = s.substring(1);
        if(!s) return false;
        var dots = 0;
        for(var i = 0; i < s.length; i++){
            var ch = s.charAt(i);
            if(ch === "."){ dots++; if(dots > 1) return false; continue; }
            if(ch < "0" || ch > "9") return false;
        }
        return true;
    }

    function isNameStart(ch){
        return (ch >= "A" && ch <= "Z") || (ch >= "a" && ch <= "z") || ch === "_";
    }

    function isNameRest(ch){
        return isNameStart(ch) || (ch >= "0" && ch <= "9");
    }

    function isIdent(s){
        if(!s || !isNameStart(s.charAt(0))) return false;
        for(var i = 1; i < s.length; i++){ if(!isNameRest(s.charAt(i))) return false; }
        return true;
    }

    // 裸标识符：不是 @常量、不是数字、不是 true/false/null
    function isName(s){
        if(!s) return false;
        if(s.charAt(0) === "@" || s.charAt(0) === "\"") return false;
        if(isNumber(s)) return false;
        if(s === "true" || s === "false" || s === "null") return false;
        return isIdent(s);
    }

    function trim(s){
        s = String(s);
        var a = 0, b = s.length;
        while(a < b && isWs(s.charAt(a))) a++;
        while(b > a && isWs(s.charAt(b - 1))) b--;
        return s.substring(a, b);
    }

    // 按空白切成 token（连续空白算一个分隔符），等价于 split(/\s+/) 的正常语义。
    function tokenize(s){
        var out = [], cur = "", i, ch;
        for(i = 0; i < s.length; i++){
            ch = s.charAt(i);
            if(isWs(ch)){
                if(cur){ out.push(cur); cur = ""; }
            }else{
                cur += ch;
            }
        }
        if(cur) out.push(cur);
        return out;
    }

    // 解析成语句表：空行、# 注释、`name:` 标签都不算语句（但占行号）。
    function statements(code){
        var lines = String(code || "").split(NL);
        var out = [];
        for(var i = 0; i < lines.length; i++){
            var s = trim(lines[i]);
            if(!s || s.charAt(0) === "#") continue;
            var t = tokenize(s);
            if(t.length === 1 && t[0].charAt(t[0].length - 1) === ":") continue;
            out.push({line: i + 1, t: t});
        }
        return out;
    }

    function check(rules, code, links, opts){
        opts = opts || {};
        var build = opts.build || 0;
        var findings = [];
        var stmts = statements(code);
        var i, j, v;

        // 第一遍：程序声明了哪些名字（defs 槽位，例如 set 的第一个操作数）。
        var declared = {};
        for(i = 0; i < stmts.length; i++){
            var meta = rules.instr[stmts[i].t[0].toLowerCase()];
            if(!meta || !meta.d) continue;
            for(j = 0; j < meta.d.length; j++){
                v = stmts[i].t[meta.d[j]];
                if(isName(v)) declared[v] = 1;
            }
        }
        // links === null 的意思是「调用方不知道这个处理器有哪些链接」，和「确实一个
        // 链接都没有」是两件事。分不清的后果是真的：v0.4.6 的审查栏因为读不到
        // LogicDialog.executor（包私有字段）而喂进来一个空数组，于是每一行链接槽里的
        // 名字都被判成 link-name-not-bound -- 有链接也报「没链接」。
        var linkKnown = (links !== null && links !== undefined);
        var linkSet = {};
        if(linkKnown){
            for(i = 0; i < links.length; i++) linkSet[String(links[i])] = 1;
        }

        var floor = 0, users = [];
        // 第二遍：五条单行判据。
        for(i = 0; i < stmts.length; i++){
            var st = stmts[i], toks = st.t, mn = toks[0].toLowerCase();
            var info = rules.instr[mn];
            if(!info){
                findings.push({sev: "WARN", line: st.line, rid: null,
                    msg: "`" + toks[0] + "` is not an instruction this quick check knows -- a mod instruction, or a typo the parser turns into noop"});
                continue;
            }
            if(info.sub && toks.length > 1 && info.sub.indexOf(toks[1]) < 0){
                findings.push({sev: "WARN", line: st.line, rid: null,
                    msg: "`" + toks[1] + "` is not a " + mn + " subcommand -- the parser turns the whole line into NoopI, so it silently does nothing"});
            }
            var mb = info.mb || 0;
            if(mn === "op" && toks.length > 1 && rules.ops[toks[1]] && rules.ops[toks[1]] > mb){
                mb = rules.ops[toks[1]];
            }
            if(mb > floor){ floor = mb; users = [mn]; }
            else if(mb === floor && mb > 0 && users.indexOf(mn) < 0) users.push(mn);
            if(build > 0 && mb > build){   // build -1/0 = custom or unknown build: no gate
                findings.push({sev: "WARN", line: st.line, rid: "version-gate",
                    msg: "version gate: uses " + mn + " -- needs build >= " + mb + ", you are on " + build + "; older builds replace the line with noop"});
            }
            var slots = rules.link[mn];
            if(slots && linkKnown){
                for(j = 0; j < slots.length; j++){
                    v = toks[slots[j]];
                    if(!isName(v) || linkSet[v] || declared[v]) continue;
                    findings.push({sev: "WARN", line: st.line, rid: "link-name-not-bound",
                        msg: "`" + v + "` is in a link slot but is not linked to this processor (and is never set) -- the instruction silently reads null"});
                }
            }
        }
        if(stmts.length > rules.maxInstr){
            findings.push({sev: "ERR", line: stmts[rules.maxInstr].line, rid: null,
                msg: stmts.length + " statements exceeds LExecutor.maxInstructions (" + rules.maxInstr + ") -- the tail is silently truncated on import"});
        }
        var ipt = rules.ipt || 40;
        return {
            findings: findings,
            stats: {
                statements: stmts.length,
                linearCost: stmts.length,
                ticksPerPass: Math.ceil(stmts.length / ipt),
                floor: floor,
                floorUsers: users,
                truncates: stmts.length > rules.maxInstr,
                linkKnown: linkKnown,           // false = 链接表未知，链接槽判据整段跳过
                linkCount: linkKnown ? links.length : -1
            }
        };
    }

    return {check: check, statements: statements, isName: isName};
})();

// scripts/main.js is the only file the game runs (Mods.java:809); it pulls this
// in with require(), so the module has to export itself the CommonJS way.
if(typeof module !== "undefined" && module.exports){ module.exports = MAI_QUICKCHECK; }
