#!/usr/bin/env python3
"""Every rule must fire on at least one fixture, and every fixture rid set is pinned.

Why this exists: tests/expected.json digests store (sev, line), but only three of the
39 rules are asserted with their rule id, so rewording a message silently re-classifies
a finding (rule_ids.py picks the longest matching msg_any substring) without any test
turning red. rule_capture.py lint only proves a rule does NOT fire on its own counter
example -- a rule that never fires anywhere passes that test too.

Inputs : rules/*.json (id, severity, state, msg_any) + tests/cases/*.mlog
Output : tests/rule-coverage.json  {rid: [case, ...]}   (--write)
Exit   : 0 all clean / 1 something to fix / 2 cannot run
"""
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / 'tools'
CASES = ROOT / 'tests' / 'cases'
RULES = ROOT / 'rules'
BASELINE = ROOT / 'tests' / 'rule-coverage.json'
sys.path.insert(0, str(TOOLS))


def load_rules():
    out = {}
    for p in sorted(RULES.glob('*.json')):
        try:
            d = json.loads(p.read_text('utf-8'))
        except Exception as e:
            print('!! unreadable rule %s: %s' % (p.name, e))
            continue
        out[d.get('id') or p.stem] = d
    return out


def scan():
    import mlogcheck as C
    hits, errors = {}, []
    for p in sorted(CASES.glob('*.mlog')):
        lines = p.read_text('utf-8').splitlines()
        # links=None means "unknown" and deliberately never enters the
        # link-name branch; links=[] is what check_corpus() actually calls
        # (a fragment with no declared links).  Both are real analyses, so a
        # rule may fire under either -- union them.
        found = []
        for kw in ({}, {'links': []}):
            try:
                found.extend(C.analyse(lines, **kw))
            except Exception as e:
                errors.append((p.name, repr(e)))
        for f in found:
            rid = getattr(f, 'rid', None) or '(no-id)'
            hits.setdefault(rid, set()).add(p.name)
    return {k: sorted(v) for k, v in hits.items()}, errors


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    write = '--write' in argv
    verbose = '-v' in argv or '--verbose' in argv

    rules = load_rules()
    if not rules:
        print('no rules under %s' % RULES)
        return 2
    hits, errors = scan()
    for name, err in errors:
        print('!! analyse raised on %s: %s' % (name, err))

    if write:
        BASELINE.write_text(json.dumps(hits, indent=2, ensure_ascii=False) + chr(10), 'utf-8')
        ncase = len(set(x for v in hits.values() for x in v))
        print('wrote %d rid(s) over %d case(s) to %s' % (len(hits), ncase, BASELINE.name))
        return 0

    problems = []
    # A rule with "surface": "blueprint" can only fire from analyse_blueprint()
    # (it needs the other processors' variable tables), so a .mlog fixture can
    # never cover it.  It is not dead -- it is out of reach of this check, and
    # the count is printed so the list cannot grow silently.
    blueprint = sorted(rid for rid, d in rules.items() if d.get('surface') == 'blueprint')
    active = [rid for rid, d in rules.items()
              if str(d.get('severity', '')).upper() != 'SUPPRESSED'
              and d.get('surface') != 'blueprint']
    for rid in sorted(active):
        if rid not in hits:
            problems.append('dead-rule: %s never fires on any tests/cases/*.mlog' % rid)
    for rid in sorted(set(hits) - set(rules)):
        problems.append('unknown-rid: %s is reported by a fixture but no rules/*.json has it' % rid)

    if BASELINE.exists():
        want = json.loads(BASELINE.read_text('utf-8'))
        for rid in sorted(set(want) | set(hits)):
            a, b = want.get(rid), hits.get(rid)
            if a != b:
                problems.append('drift: %s: baseline %s vs now %s' % (rid, a, b))
    elif not problems:
        problems.append('no baseline: run this script with --write once')

    if verbose or problems:
        for rid in sorted(hits):
            tail = ' ...' if len(hits[rid]) > 4 else ''
            print('  %-52s %d case(s): %s%s' % (rid, len(hits[rid]), ', '.join(hits[rid][:4]), tail))
    for msg in problems:
        print('PROBLEM ' + msg)

    ncase = len(set(x for v in hits.values() for x in v))
    print('  note: %d rule(s) only fire on blueprints, not covered here: %s'
          % (len(blueprint), ', '.join(blueprint) or 'none'))
    print('%d rule(s) (%d active), %d rid(s) fire on %d fixture(s), %d problem(s)'
          % (len(rules), len(active), len(hits), ncase, len(problems)))
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())