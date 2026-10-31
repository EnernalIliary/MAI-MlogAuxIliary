"""Classify the signature hits into strong (an actual byline) and weak (a mention
inside prose), so the weak ones can be reviewed before they get tagged."""
import json
import pathlib
import re
import sys

sys.path.insert(0, "mindustry-blueprint/tools")
import msch  # noqa: E402
import mlogir  # noqa: E402

C = pathlib.Path.home() / "AppData" / "Roaming" / "Mindustry" / "schematics"
NAME = re.compile(r"(?<![A-Za-z])[Ii]li(ary)?(?![A-Za-z])|艾铭|鱼破了冰|鱼冰|鱼梦醒时")
# the alt account: `鱼破了冰` (full), `鱼冰` (short, also used in blueprint names),
# `鱼梦醒时` (used as a variable name in the 集火 logic)
BYLINE = re.compile(r"(?i)\b(by|author|made\s*by|制作|作者|署名)\b|by\s*[:：]?|\bby\b")


def texts(sch):
    out = []
    if sch.description:
        out.append(("desc", sch.description))
    for t in sch.tiles:
        if isinstance(t.config, str) and t.config.strip():
            out.append(("board", t.config))
    for t in sch.logic_tiles():
        for line in t.logic.code.split("\n"):
            body = mlogir.strip_comment(line).strip()
            if body:
                out.append(("logic", body))
    return out


def strong(src: str, text: str, m: re.Match) -> bool:
    if src == "desc":
        return True
    before = text[max(0, m.start() - 40):m.start()]
    after = text[m.end():m.end() + 20]
    # "by ILI", "by [sky]Il[..]ary", "制作：艾铭"
    if re.search(r"(?i)\bby\b|[：:]|制作|作者|署名", before):
        return True
    if src == "logic":
        body = text.strip()
        # a name plate: `print ILI`, `set ILI艾铭`, `print by:ILI`
        if body.split()[0].lower() in ("print", "message", "set") and len(body) < 60:
            return True
    # the matched token sits alone with nothing but markup around it
    if not re.sub(r"\[[^\]]*\]|\s|[-_*]", "", after)[:1].isalnum():
        return True
    return False


def main() -> None:
    rows = []
    for p in sorted(C.glob("*.msch")):
        try:
            sch = msch.parse_file(str(p))
        except Exception:
            continue
        have = [l for l in (sch.labels or []) if l.strip()]
        if len(have) >= 20:
            continue
        best = None
        for src, text in texts(sch):
            m = NAME.search(text)
            if not m:
                continue
            s = strong(src, text, m)
            if best is None or (s and not best[0]):
                best = (s, src, text[:100])
            if s:
                break
        if best:
            rows.append({"file": p.name, "strong": best[0], "src": best[1],
                         "text": best[2], "has_ili": any("ILI" in l.upper() for l in have)})

    s_rows = [r for r in rows if r["strong"]]
    w_rows = [r for r in rows if not r["strong"]]
    print(f"命中 {len(rows)} 份：强署名 {len(s_rows)}（其中缺 ILI 标签 "
          f"{sum(1 for r in s_rows if not r['has_ili'])}），弱提及 {len(w_rows)}"
          f"（缺标签 {sum(1 for r in w_rows if not r['has_ili'])}）")
    print("\n--- 弱提及（需人看）")
    for r in w_rows[:40]:
        print(f"  {r['file']}  [{r['src']}] {r['text'][:70]}")
    pathlib.Path("out/_ili_classified.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
