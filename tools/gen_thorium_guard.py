"""Generate the thorium-reactor anti-explosion blueprint.

Layout (tile coordinates, sizes from block_attrs/BLOCKS):
  micro-processor   1x1 at (0,0)
  thorium-reactor   3x3 centered at (2,1)  -> occupies (1,0)..(3,2)
  link reactor1@2,1 (relative to the processor)

`add_logic` only records the link; the target building itself must be placed,
otherwise `reactor1` resolves to nothing at paste time. That is the known
limitation of the exporter, so the reactor is placed here explicitly and
`link_report()` is used to prove the link lands on it.
"""
import pathlib
import sys

HERE = pathlib.Path("F:/OneDrive/桌面/AtomicStates")
sys.path.insert(0, str(HERE / "mindustry-blueprint" / "tools"))
import msch, mschgen  # noqa: E402

code = (HERE / "out" / "thorium-guard.mlog").read_text("utf-8")

bp = mschgen.Blueprint(
    name="钍反防爆 v1",
    description="[lightgray]热量到 [#ffb6c1]0.30[] 就关，掉到 [#ffb6c1]0.15[] 再开："
                "钍反 heat>=0.999 熔毁、>=0.5 被摧毁时才炸，压到 0.5 以下就炸不了。"
                "只认 [accent]@type[] 是 thorium-reactor 的链接。"
                "[]\n[lightgray]逻辑 by [#8abcd1]DeepSeek[]",
    labels=["[blue]DeepSeek"],
)
bp.add_logic(code, 0, 0, block="micro-processor",
             links=[mschgen.Link("reactor1", 2, 1)])
bp.add_block("thorium-reactor", 2, 1)

problems = bp.link_report()
for p in problems:
    print("!!", p)
if problems:
    raise SystemExit("link does not land on a placed block -- refusing to write")

out = HERE / "out" / "钍反防爆.msch"
bp.write(str(out))
print(f"wrote {out.name}: {len(bp.tiles)} tile(s), {out.stat().st_size} bytes")

# --- round-trip proof ------------------------------------------------------ #
sch = msch.parse_file(str(out))
print(f"parsed back: {len(sch.tiles)} tile(s), {len(sch.logic_tiles())} logic tile(s)")
for t in sch.tiles:
    print(f"  {t.block:<18} @({t.x},{t.y})")
for t in sch.logic_tiles():
    print(f"  logic {t.block} @({t.x},{t.y})  links: "
          + ", ".join(f"{l.name}@({l.x},{l.y})" for l in t.logic.links))
    same = t.logic.code.strip() == code.strip()
    print(f"  logic text identical to source: {same}")
    if not same:
        raise SystemExit("logic code changed in the round trip")
