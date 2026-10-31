"""游戏内容速查（数据由 out/_dump_game.py 从 v160.7 真执行器导出）。

    python tools/game.py item silicon
    python tools/game.py block silicon-smelter
    python tools/game.py unit flare
    python tools/game.py recipe silicon
    python tools/game.py search furnace
    python tools/game.py stats
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "game"


def load() -> dict:
    if not (DATA / "blocks.json").is_file():
        sys.exit(f"missing {DATA}; run: python out/_dump_game.py")
    out = {}
    for name in ("items", "liquids", "blocks", "units"):
        out[name] = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
    meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    ap = DATA / "aliases.json"
    out["aliases"] = json.loads(ap.read_text(encoding="utf-8")) if ap.is_file() else {}
    return {"meta": meta, **out}


def find(rows: list[dict], name: str, kind: str = "", aliases: dict | None = None) -> dict:
    key = name.lower().lstrip("@")
    for r in rows:
        if r["name"].lower() == key:
            return r
    norm = re.sub(r"[\s_]", "-", key)
    for r in rows:
        if r["name"].lower() == norm:
            return r
    if aliases:
        for r in rows:
            if key in [a.lower() for a in aliases.get(kind, {}).get(r["name"], [])]:
                return r
    hit = [r for r in rows if key in r["name"].lower()]
    if len(hit) == 1:
        return hit[0]
    if hit:
        sys.exit("ambiguous: " + ", ".join(r["name"] for r in hit[:8]))
    sys.exit(f"not found: {name}")


def fmt_stack(s: dict) -> str:
    n = s["amount"]
    return f"{s['name']}×{int(n) if n == int(n) else n}"


def _num(v: float) -> int | float:
    v = round(float(v), 2)
    return int(v) if v.is_integer() else v


def fmt_cons(cons: list[dict]) -> str:
    parts = []
    for c in cons:
        if c["kind"] == "power":
            parts.append(f"电 {_num(c['amount'])}/s")
        elif c["kind"] == "liquid":
            parts.append(f"{c['name']} {_num(c['amount'])}/s")
        else:
            parts.append(f"{c['name']}×{_num(c['amount'])}")
    return "、".join(parts) if parts else "—"


def produced_by(game: dict, item: str) -> list[dict]:
    return [b for b in game["blocks"] if any(o["name"] == item for o in b["outputs"])]


def consumed_by(game: dict, item: str) -> list[dict]:
    return [b for b in game["blocks"] if any(c["kind"] == "item" and c["name"] == item for c in b["consumers"])]


def required_by(game: dict, item: str) -> list[dict]:
    return [b for b in game["blocks"] if any(r["name"] == item for r in b["requirements"])]


def zh(game: dict, kind: str, name: str) -> str:
    al = game.get("aliases", {}).get(kind, {}).get(name, [])
    return "、".join(al)


def cmd_item(game: dict, name: str) -> None:
    it = find(game["items"], name, "items", game.get("aliases"))
    alias = zh(game, "items", it["name"])
    print(f"物品 {it['name']}  id={it['id']}  color=#{it['color']}" + (f"  中文：{alias}" if alias else ""))
    prod = produced_by(game, it["name"])
    print("\n产出：")
    print("  —" if not prod else "")
    for b in prod:
        print(f"  {b['name']:<26} {fmt_cons(b['consumers'])} → {', '.join(fmt_stack(o) for o in b['outputs'])}  ({b['size']}×{b['size']})")
    cons = consumed_by(game, it["name"])
    print("\n消耗：")
    print("  —" if not cons else "")
    for b in cons:
        outs = ", ".join(fmt_stack(o) for o in b["outputs"]) or "—"
        print(f"  {b['name']:<26} {fmt_cons(b['consumers'])} → {outs}")
    req = required_by(game, it["name"])
    if req:
        print(f"\n作为建材：{', '.join(b['name'] for b in req[:25])}" + (" …" if len(req) > 25 else ""))


def cmd_block(game: dict, name: str) -> None:
    b = find(game["blocks"], name, "blocks", game.get("aliases"))
    alias = zh(game, "blocks", b["name"])
    print(f"方块 {b['name']}  id={b['id']}  {b['size']}×{b['size']}  hp={b['health']}  类别={b['category']}"
          + (f"  中文：{alias}" if alias else ""))
    print(f"耗电：{fmt_cons([c for c in b['consumers'] if c['kind'] == 'power']) if b['hasPower'] else '—'}")
    if b["requirements"]:
        print("建材：" + "、".join(fmt_stack(r) for r in b["requirements"]))
    if b["consumers"]:
        print("输入：" + fmt_cons(b["consumers"]))
    if b["outputs"]:
        print("产出：" + "、".join(fmt_stack(o) for o in b["outputs"]))
    if b["craftTime"] and b["craftTime"] > 0:
        print(f"craftTime：{b['craftTime']} tick")


def cmd_unit(game: dict, name: str) -> None:
    u = find(game["units"], name, "units", game.get("aliases"))
    alias = zh(game, "units", u["name"])
    kind = "飞行" if u["flying"] else "地面"
    print(f"单位 {u['name']}  id={u['id']}  hp={u['health']}  护甲={u['armor']}  速度={u['speed']:.2f}  {kind}"
          + (f"  中文：{alias}" if alias else ""))
    print(f"体积={u['hitSize']}  最大射程={u['maxRange']:.1f}")
    for w in u["weapons"]:
        print(f"  武器 {w['name'] or '-'}：伤害={w['damage']} 弹速={w['bulletSpeed']} 射程={w['bulletRange']:.1f} 装填={w['reload']}")


def cmd_recipe(game: dict, name: str) -> None:
    item = find(game["items"], name, "items", game.get("aliases"))["name"]
    prod = produced_by(game, item)
    print(f"{item} 的配方（{len(prod)} 个产出方块）")
    for b in prod:
        print(f"  {b['name']:<26} {fmt_cons(b['consumers'])} → {', '.join(fmt_stack(o) for o in b['outputs'])}")


def cmd_search(game: dict, q: str) -> None:
    ql = q.lower()
    for kind in ("items", "liquids", "blocks", "units"):
        hits = [r["name"] for r in game[kind] if ql in r["name"].lower()]
        if hits:
            print(f"{kind}: " + ", ".join(hits[:30]))


def cmd_stats(game: dict) -> None:
    print(f"version {game['meta']['version']}  " + "  ".join(f"{k}={len(game[k])}" for k in ("items", "liquids", "blocks", "units")))


def main(argv: list[str]) -> int:
    game = load()
    if not argv:
        print(__doc__)
        return 0
    cmd, *rest = argv
    if cmd == "stats":
        cmd_stats(game)
    elif cmd == "search":
        cmd_search(game, rest[0])
    elif cmd == "item":
        cmd_item(game, rest[0])
    elif cmd == "block":
        cmd_block(game, rest[0])
    elif cmd == "unit":
        cmd_unit(game, rest[0])
    elif cmd == "recipe":
        cmd_recipe(game, rest[0])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv[1:]))
