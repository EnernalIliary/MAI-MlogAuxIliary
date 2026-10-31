// node harness for the two MlogAuxiliary modules, loaded the SAME way the game loads
// them: through require().  Mods.java:809 runs only scripts/main.js, so the mod uses
// require("quickcheck") / require("rules.generated") (Scripts.java:30-32 installs the
// CommonJS provider).  Using require() here means the export contract is under test,
// not just the functions.
//
// usage: node tests/mlogauxiliary/harness.js [cases.json]  -> JSON on stdout

var fs = require("fs");
var path = require("path");

var here = __dirname;
var scripts = path.join(here, "..", "..", "MlogAuxiliary", "scripts");

var rules = require(path.join(scripts, "rules.generated.js"));
var quickcheck = require(path.join(scripts, "quickcheck.js"));

var casesFile = process.argv[2] || path.join(here, "cases.json");
var cases = JSON.parse(fs.readFileSync(casesFile, "utf8"));
var out = {};

cases.forEach(function(c){
    var code = c.code || "";
    if(c.repeat) code = new Array(c.repeat[1] + 1).join(c.repeat[0] + "\n");
    // links === null means "the caller could not read the link table" (the real case:
    // LogicDialog.executor is package-private, so Rhino cannot reach build.links).
    // quickcheck must then skip the link-slot rule instead of warning on every slot --
    // an absent "links" key keeps the old meaning of "this processor has no links".
    var links = (c.links === undefined) ? [] : c.links;
    var r = quickcheck.check(rules, code, links, {build: c.build || 0});
    out[c.name] = {
        findings: r.findings.map(function(f){ return [f.sev, f.line, f.rid]; }),
        stats: r.stats
    };
});

// the module contract itself: what require() hands back must be usable
out.__modules = {
    rulesVersion: rules.v,
    instructions: Object.keys(rules.instr).length,
    hasCheck: typeof quickcheck.check,
    hasStatements: typeof quickcheck.statements
};

process.stdout.write(JSON.stringify(out, null, 1) + "\n");