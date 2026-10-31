"""Generate the landing-pad resource scheduler for the user's `tst` blueprint.

Design notes -- every claim below was checked against the v159 source:

* `ulocate` is NOT a free lookup: `LExecutor.java:232-237` returns immediately unless
  `@unit` is bound (and unless the map rule `logicUnitControl` is on). So the logic
  binds with `ubind @flare` before calling it.
* a linked core is cheaper and rule-free, so it is tried first: reading a slot off a
  link that does not exist yields null, while a real core always yields a number.
  `sensor probe core1 @copper` therefore doubles as an "is the core linked?" test.
* `control config <pad> <item>` goes through the object overload: `LExecutor.java:584`
  (`type.isObj && p1.isobj`) -> `BuildingComp.java:2163` -> `configured(null, item)`.
* `LandingPad.config` is the requested item; `LandingPad.java:334` only lands when
  `getImportRate(planet, config) > 0`, so asking for an unfamiliar item just idles.
* the last comparison omits `set mn n`: nothing reads `mn` afterwards, which is exactly
  the "value overwritten before any read" the checker flags.
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "mindustry-blueprint" / "tools"))

ITEMS = ["copper", "lead", "graphite", "silicon", "metaglass", "titanium", "thorium",
         "plastanium", "phase-fabric", "surge-alloy", "spore-pod", "blast-compound",
         "pyratite"]
PADS = ["pad1", "pad2", "pad3", "pad4", "pad5", "pad6"]


def build() -> str:
    L: list[str] = []
    L.append("# 着陆板资源调度:6 个着陆板都请求「核心最少的那个资源」")
    L.append("# 优先用链接 core1;没链接就用 ubind + ulocate 找核心")
    L.append("set by:DeepSeek 1")
    L.append("sensor probe core1 @copper")
    L.append("jump havecore notEqual probe null")
    L.append("ubind @flare")
    L.append("ulocate building core false @copper cx cy found core1")
    L.append("jump nocore equal found 0")
    L.append("havecore:")
    L.append(f"set best @{ITEMS[0]}")
    L.append(f"sensor mn core1 @{ITEMS[0]}")
    for i, item in enumerate(ITEMS[1:], start=1):
        last = i == len(ITEMS) - 1
        L.append(f"sensor n core1 @{item}")
        L.append(f"jump s{i} greaterThanEq n mn")
        if not last:
            L.append("set mn n")
        L.append(f"set best @{item}")
        L.append(f"s{i}:")
    for pad in PADS:
        L.append(f"sensor cur {pad} @config")
        L.append(f"jump w{pad} strictEqual cur best")
        L.append(f"control config {pad} best")
        L.append(f"w{pad}:")
    L.append("nocore:")
    L.append("end")
    return "\n".join(L) + "\n"


def main() -> None:
    code = build()
    out = HERE / "out" / "landing-scheduler.mlog"
    out.write_text(code, "utf-8")
    print(f"--- {len(code.splitlines())} lines -> {out}")
    print(code)


if __name__ == "__main__":
    main()
