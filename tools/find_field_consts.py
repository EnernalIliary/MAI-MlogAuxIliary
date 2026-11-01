"""
Find every `<int constant> -> putfield/putstatic <name>` pair in a JVM class file.

Used to prove what MindustryX's patch sets maxInstructionsPerTick to, since the
mod replaces the vanilla LogicBlock default of 40.
"""
import struct
import sys
import zipfile

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dev"))  # classdump 已归到 tools/dev
from classdump import parse  # noqa: E402


def disasm(pool, names, code):
    mx, ml = struct.unpack(">HH", code[:4])
    blen = struct.unpack(">I", code[4:8])[0]
    b = code[8:8 + blen]
    pc, out, stack = 0, [], []

    def field_name(idx):
        ref = pool.get(idx)
        if not ref or ref[0] != "ref":
            return "?"
        nt = pool.get(ref[1])
        if not nt or nt[0] != "ref":
            return "?"
        # NameAndType stores (name_index, descriptor_index) as two u2s
        raw = None
        return names.get(nt[1], "?")

    while pc < len(b):
        op = b[pc]
        if op == 0x10:                                    # bipush
            stack.append(b[pc + 1] if b[pc + 1] < 128 else b[pc + 1] - 256); pc += 2
        elif op == 0x11:                                  # sipush
            stack.append(struct.unpack(">h", b[pc + 1:pc + 3])[0]); pc += 3
        elif op in (0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08):
            stack.append(op - 3); pc += 1                 # iconst_<n>
        elif op == 0x12:
            v = pool.get(b[pc + 1])
            stack.append(v[1] if v and v[0] == "int" else "?"); pc += 2
        elif op == 0x13:
            v = pool.get(struct.unpack(">H", b[pc + 1:pc + 3])[0])
            stack.append(v[1] if v and v[0] == "int" else "?"); pc += 3
        elif op in (0xb5, 0xb3):                          # putfield / putstatic
            idx = struct.unpack(">H", b[pc + 1:pc + 3])[0]
            val = stack[-1] if stack else "?"
            out.append((field_name(idx), val))
            pc += 3
        elif op == 0x84:                                  # iinc
            pc += 3
        elif 0x99 <= op <= 0xa8 or op in (0xc6, 0xc7):
            pc += 3
        elif op in (0xb2, 0xb4, 0xb6, 0xb7, 0xb8, 0xb9, 0xba,
                    0xbb, 0xbd, 0xc0, 0xc1, 0xbc):
            pc += 3
        elif op == 0xc5:
            pc += 4
        elif op in (0xaa, 0xab):
            pad = (4 - ((pc + 1) % 4)) % 4
            pc += 1 + pad
            if op == 0xaa:
                lo, hi = struct.unpack(">ii", b[pc + 4:pc + 12])
                pc += 12 + 4 * (hi - lo + 1)
            else:
                n = struct.unpack(">i", b[pc + 4:pc + 8])[0]
                pc += 8 + 8 * n
        elif op == 0xc4:
            pc += 6 if b[pc + 1] == 0x84 else 4
        else:
            pc += 1
    return out


def main(jar, *classes):
    zf = zipfile.ZipFile(jar)
    for cls in classes:
        try:
            data = zf.read(cls)
        except KeyError:
            print(f"missing {cls}")
            continue
        major, pool, methods = parse(data)
        names = {k: v[1] for k, v in pool.items() if v[0] == "utf8"}
        print(f"=== {cls}")
        for n, d, c in methods:
            if not c:
                continue
            pairs = [p for p in disasm(pool, names, c)
                     if isinstance(p[1], int) and p[1] > 1]
            if pairs:
                for fname, val in pairs:
                    print(f"   {n}{d}:  {fname} = {val}")


if __name__ == "__main__":
    main(sys.argv[1], *sys.argv[2:])
