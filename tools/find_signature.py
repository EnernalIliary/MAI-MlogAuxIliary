"""Find blueprints that carry this author's signature, and tag them ILI.

Evidence accepted (any one is enough):

* the schematic description,
* text stored in a message / world-message block,
* a string literal or variable name inside the logic (`print ...ILI`, `set ILI ...`,
  `print by:DeepSeek`-style name plates),
* a `print`/`message` line whose text is the signature.

The ASCII form is matched with letter boundaries so that `ability`, `facility`
and `utility` do not count; the Chinese form is matched as-is.
"""
import json
import pathlib
import re
import sys

sys.path.insert(0, "mindustry-blueprint/tools")
import msch  # noqa: E402
import mlogir  # noqa: E402

C = pathlib.Path.home() / "AppData" / "Roaming" / "Mindustry" / "schematics"

# [blue]Ili[#8abcd1]ary[#654321]艾铭[#6335F8] -- the pieces people keep
PATTERNS = [
    re.compile(r"(?<![A-Za-z])[Ii][Ll][Ii](?![A-Za-z])"),
    re.compile(r"(?<![A-Za-z])[Ii]liary(?![A-Za-z])", re.I),
    re.compile(r"艾铭"),
    re.compile(r"iliaryl?", re.I),
    # alt account -- 鱼破了冰 (full), 鱼冰 (short), 鱼梦醒时 (used as a variable name)
    re.compile(r"鱼破了冰"),
    re.compile(r"鱼冰"),
    re.compile(r"鱼梦醒时"),
]


def texts_of(sch) -> list[tuple[str, str]]:
    """(source, text) pairs to search."""
    out: list[tuple[str, str]] = []
    if sch.description:
        out.append(("description", sch.description))
    if sch.name:
        out.append(("name", sch.name))
    for t in sch.tiles:
        cfg = t.config
        if isinstance(cfg, str) and cfg.strip():
            out.append((f"{t.block}", cfg))
    for t in sch.logic_tiles():
        for line in t.logic.code.split("\n"):
            body = mlogir.strip_comment(line).strip()
            if body:
                out.append(("logic", body))
    return out


def main() -> None:
    apply = "--apply" in sys.argv
    hits: list[dict] = []
    for p in sorted(C.glob("*.msch")):
        try:
            sch = msch.parse_file(str(p))
        except Exception:
            continue
        found = []
        for src, text in texts_of(sch):
            for pat in PATTERNS:
                m = pat.search(text)
                if m:
                    found.append((src, m.group(0), text[:90]))
                    break
        if not found:
            continue
        hits.append({"file": p.name,
                     "labels": [l for l in (sch.labels or []) if l.strip()],
                     "evidence": found[:3]})

    print(f"命中 {len(hits)} 份")
    for h in hits:
        ev = "; ".join(f"{s}:{w}" for s, w, _ in h["evidence"])
        print(f"  {h['file']}  [{ev}]")
        for src, w, txt in h["evidence"][:1]:
            print(f"      {txt}")

    pathlib.Path("out/_ili_hits.json").write_text(
        json.dumps(hits, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
