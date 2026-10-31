"""Re-emit the thorium guard with its author label and install it into the game.

The label is `[blue]DeepSeek` -- the class the user added for my work. It is
written at generation time (tools/gen_thorium_guard.py) so every blueprint I
produce carries it from now on; this script just regenerates, checks the logic
survived, and copies the result into the schematic folder.
"""
import pathlib
import shutil
import sys

sys.path.insert(0, "mindustry-blueprint/tools")
sys.path.insert(0, "tools")
import msch  # noqa: E402
import mschgen  # noqa: E402
import gen_thorium_guard as GEN  # noqa: E402,F401  (importing regenerates the file)

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "钍反防爆.msch"
C = pathlib.Path.home() / "AppData" / "Roaming" / "Mindustry" / "schematics"


def main() -> None:
    sch = msch.parse_file(str(OUT))
    print(f"生成: {len(sch.tiles)} 方块, {len(sch.logic_tiles())} 逻辑块, "
          f"标签={sch.labels}")
    code = "\n".join(t.logic.code for t in sch.logic_tiles())
    assert "print by:DeepSeek" in code, "署名丢了"
    assert sch.labels == ["[blue]DeepSeek"], sch.labels

    dst = C / OUT.name
    if "--install" in sys.argv:
        shutil.copy2(OUT, dst)
        back = msch.parse_file(str(dst))
        assert [t.block for t in back.tiles] == [t.block for t in sch.tiles]
        assert "\n".join(t.logic.code for t in back.logic_tiles()) == code
        assert back.labels == ["[blue]DeepSeek"]
        print(f"已安装: {dst}")
    else:
        print(f"(未安装, 加 --install 可复制到 {dst})")


if __name__ == "__main__":
    main()
