
import io, json, os, subprocess, sys, tempfile
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

CASES = os.path.join(HERE, 'cases.json')
HARNESS = os.path.join(HERE, 'harness.js')

INNER = r'''
var F = Packages.java.nio.file.Files, P = Packages.java.nio.file.Paths, JS = Packages.java.lang.String;
function rd(p){ return new JS(F.readAllBytes(P.get(p)), 'UTF-8'); }
var cases = JSON.parse(rd('__CASES__'));
var out = {};
for(var i = 0; i < cases.length; i++){
    var c = cases[i];
    var code = c.code || "";
    if(c.repeat){ code = new Array(c.repeat[1] + 1).join(c.repeat[0] + "\n"); }
    // links === null means "the caller could not read the link table" (the real case:
    // LogicDialog.executor is package-private) -- keep it null so both sides agree with
    // harness.js, which passes null through the same way.
    var links = (c.links === undefined) ? [] : c.links;
    var r = MAI_QUICKCHECK.check(MAI_RULES, code, links, {build: c.build || 0});
    out[c.name] = {findings: r.findings.map(function(f){ return [f.sev, f.line, f.rid]; }), stats: r.stats};
}
out.__modules = {rulesVersion: MAI_RULES.v, instructions: Object.keys(MAI_RULES.instr).length,
                 hasCheck: typeof MAI_QUICKCHECK.check, hasStatements: typeof MAI_QUICKCHECK.statements};
print("MAIJSON:" + JSON.stringify(out));
'''

def node_side():
    r = subprocess.run(['node', HARNESS, CASES], capture_output=True, text=True, encoding='utf-8')
    if r.returncode != 0:
        print('node harness failed:', r.stderr.strip()[:500]); return None
    return json.loads(r.stdout)

def rhino_side():
    try:
        import mlogsim as M
    except Exception as e:
        print('SKIP no mlogsim:', e); return None
    jar = getattr(M, 'V160_JAR', None)
    if not jar or not os.path.exists(jar):
        print('SKIP no v160 jar'); return None
    java = M._default_java()
    if not java:
        print('SKIP no java'); return None
    srv = M.Server(java, jar)
    srv.prepare()
    M.run_mlog(srv, 'p', 'noop', steps=1, update='none')
    d = os.path.join(ROOT, 'out', '_lw_rhino')
    os.makedirs(d, exist_ok=True)
    inner_path = os.path.join(d, 'inner.js')
    io.open(inner_path, 'w', encoding='utf-8', newline='\n').write(INNER.replace('__CASES__', CASES.replace(chr(92), '/')))
    scripts = os.path.join(ROOT, 'MlogAuxiliary', 'scripts').replace(chr(92), '/')
    js = ' '.join([
        "var s = Packages.mindustry.Vars.mods.getScripts();",
        "var F = Packages.java.nio.file.Files, P = Packages.java.nio.file.Paths, JS = Packages.java.lang.String;",
        "function rd(p){ return new JS(F.readAllBytes(P.get(p)), 'UTF-8'); }",
        "s.runConsole(rd('%s/rules.generated.js'));" % scripts,
        "s.runConsole(rd('%s/quickcheck.js'));" % scripts,
        "s.runConsole(rd('%s'));" % inner_path.replace(chr(92), '/'),
        "void 0;",
    ])
    lines = srv.js(js)
    for ln in lines:
        ln = str(ln)
        if ln.startswith('MAIJSON:'):
            return json.loads(ln[len('MAIJSON:'):])
    print('rhino produced no LWJSON; console said:', ' | '.join(str(x) for x in lines)[:800])
    return None

def main():
    node = node_side()
    if node is None: return 1
    rh = rhino_side()
    if rh is None: return 3
    bad = []
    for k in sorted(node):
        if k not in rh:
            bad.append((k, 'missing in rhino')); continue
        if node[k] != rh[k]:
            bad.append((k, node[k], rh[k]))
    if bad:
        print('rhino parity: %d difference(s)' % len(bad))
        for b in bad[:6]: print('  DIFF', json.dumps(b, ensure_ascii=False)[:600])
        return 1
    print('rhino parity: %d case(s) identical in node and in the game engine' % (len(node) - 1))
    print('rhino OK')
    return 0

if __name__ == '__main__':
    sys.exit(main())
