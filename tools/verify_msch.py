"""Diagnose tag encoding + round-trip verify msch.py against real files."""
import sys, os, glob, json, zlib, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import msch
import os

SCHEM = os.environ.get("MLOG_SCHEM") or os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "Mindustry", "schematics")

# 1) look at raw tag bytes for a file with emoji-ish labels
p = os.path.join(SCHEM, "开关.msch")
data = open(p, "rb").read()
raw = zlib.decompress(data[5:])
r = msch.Reader(raw)
w, h = r.s(), r.s()
print("size", w, h)
for _ in range(r.ub()):
    k = r.utf(); v = r.utf()
    print("TAG raw-bytes:", )
print()

# re-read showing raw hex of each tag field
r = msch.Reader(raw); r.s(); r.s()
for _ in range(r.ub()):
    n = r.us(); kb = r.bytes(n)
    n2 = r.us(); vb = r.bytes(n2)
    print("KEY hex:", kb.hex(), "->", kb)
    print("VAL hex:", vb.hex())
    print("VAL utf8 :", vb.decode('utf-8', 'replace'))
    print("VAL cesu8:", vb.decode('utf-8', 'surrogatepass').encode('utf-16', 'surrogatepass').decode('utf-16', 'replace'))
    print()

# 2) round-trip test: parse -> rebuild -> reparse, compare logic code
print("=" * 60)
print("ROUND TRIP TEST")
ok = fail = 0
fails = []
files = sorted(glob.glob(os.path.join(SCHEM, "*.msch")))[:400]
for f in files:
    try:
        sch = msch.parse_file(f)
    except Exception as e:
        fails.append((os.path.basename(f), f"parse: {e}")); fail += 1; continue
    try:
        rebuilt = msch.build_schematic(sch.width, sch.height,
                                       {k: v for k, v in sch.tags.items()},
                                       sch.tiles)
        sch2 = msch.parse_bytes(rebuilt)
        a = [t.logic.code for t in sch.logic_tiles()]
        b = [t.logic.code for t in sch2.logic_tiles()]
        if a == b:
            ok += 1
        else:
            fails.append((os.path.basename(f), "logic mismatch")); fail += 1
    except Exception as e:
        fails.append((os.path.basename(f), f"rebuild: {type(e).__name__}: {e}")); fail += 1

print(f"ok={ok} fail={fail} (of {len(files)})")
for name, why in fails[:25]:
    print("  FAIL", name, "|", why)
