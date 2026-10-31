#!/usr/bin/env python3
"""Pull every logic processor's source code out of a Mindustry .msav save.

A save is one zlib stream holding: "MSAV", an int version, then named regions,
each framed as `int length` + that many bytes (SaveFileReader.readChunk).

Region *order* is not stable: Save11 reads meta, content, patches, ... while
SaveVersion (>= 12) reads meta, patches, content, ... and some builds in the
wild disagree with both. So this reader trusts no order at all. It reads the
meta region for the map dimensions, then tries every remaining region as the map
region and keeps the one that parses *exactly* to its own end. Nothing else in a
save looks like a tile map that consumes its region perfectly, so the check is
self-validating rather than version-guessing.

Tile layout, version >= 11 (SaveVersion.readMap):

    width u16, height u16
    floor pass : (floor i16, ore i16, consecutives u8)*
    block pass : per tile  block i16, packed u8
                 bit0: an entity follows      bit2: 7 bytes of tile data follow
                 if bit2: data u8, floorData u8, overlayData u8, extra i32
                 if bit0: isCenter bool
                 if bit0 and isCenter: int length + that many bytes of payload
                 if not bit0 and not bit2: consecutives u8

Only a multiblock's center carries the building, and every payload is
length-prefixed -- so a building whose fields this reader does not understand
can still be skipped exactly. That is also why it never replays
`Building.readBase`: inside a processor's payload it searches for an inner
deflate blob that validates as the logic config format (`_scan_config`).
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

PROCESSORS = {
    "micro-processor",
    "logic-processor",
    "hyper-processor",
    "world-processor",
}

CT_BLOCK = 1  # ContentType.all ordinal for blocks


class Reader:
    """Big-endian reader over a bytes buffer, matching DataInputStream."""

    def __init__(self, data: bytes, pos: int = 0):
        self.d = data
        self.p = pos

    def need(self, n: int):
        if n < 0 or self.p + n > len(self.d):
            raise EOFError(f"want {n} bytes at {self.p}, have {len(self.d) - self.p}")

    def u8(self) -> int:
        self.need(1)
        v = self.d[self.p]
        self.p += 1
        return v

    def i16(self) -> int:
        self.need(2)
        v = struct.unpack_from(">h", self.d, self.p)[0]
        self.p += 2
        return v

    def u16(self) -> int:
        self.need(2)
        v = struct.unpack_from(">H", self.d, self.p)[0]
        self.p += 2
        return v

    def i32(self) -> int:
        self.need(4)
        v = struct.unpack_from(">i", self.d, self.p)[0]
        self.p += 4
        return v

    def raw(self, n: int) -> bytes:
        self.need(n)
        v = self.d[self.p:self.p + n]
        self.p += n
        return v

    def utf(self) -> str:
        return self.raw(self.u16()).decode("utf-8", "replace")

    def done(self) -> bool:
        return self.p >= len(self.d)


def _read_string_map(r: Reader) -> dict[str, str]:
    return {r.utf(): r.utf() for _ in range(r.u16())}


def _split_regions(data: bytes, start: int) -> list[bytes]:
    """Read framed regions until the buffer runs out or the framing stops making sense."""
    out: list[bytes] = []
    p = start
    while p + 4 <= len(data) and len(out) < 12:
        n = struct.unpack_from(">i", data, p)[0]
        if n < 0 or p + 4 + n > len(data):
            break
        p += 4
        out.append(data[p:p + n])
        p += n
    return out


def _scan_config(payload: bytes):
    """Find the logic config inside a processor's building payload.

    The payload starts with Building.readBase fields whose layout depends on the
    block's modules and the save's entity version; replaying that correctly would
    mean re-implementing the module system. Instead walk every offset and keep the
    one whose inner deflate blob parses as the config format
    (LogicBlock.readCompressed): version byte, code bytes, named links, and the
    stream must end exactly after the link table. A wrong offset cannot pass.
    """
    for off in range(0, max(0, len(payload) - 5)):
        n = int.from_bytes(payload[off:off + 4], "big")
        if n < 5 or off + 4 + n > len(payload):
            continue
        try:
            inner = zlib.decompress(payload[off + 4:off + 4 + n])
        except zlib.error:
            continue
        try:
            r = Reader(inner)
            ver = r.u8()
            if ver not in (0, 1):
                continue
            clen = r.i32()
            if clen < 0 or clen > 100 * 1024:
                continue
            code = r.raw(clen).decode("utf-8")
            total = r.i32()
            if total < 0 or total > 6000:
                continue
            links = []
            for _ in range(total):
                if ver == 0:
                    r.i32()
                else:
                    name = r.utf()
                    x, y = r.i16(), r.i16()
                    links.append((name, x, y))
            if not r.done():
                continue
        except Exception:
            continue
        return code, links
    return None


def _content_blocks(reg: bytes):
    """Return the block-name table of a content region (or None if it is not one)."""
    r = Reader(reg)
    mapped = r.u8()
    if mapped > 40:
        return None
    table = None
    for _ in range(mapped):
        tid = r.u8()
        total = r.u16()
        names = [r.utf() for _ in range(total)]
        if tid == CT_BLOCK:
            table = names
    return table


def _parse_map(reg: bytes, blocks: list[str]):
    """Parse a modern (version >= 11) map region. Raises if it is not one."""
    r = Reader(reg)
    w, h = r.u16(), r.u16()
    if not (1 <= w <= 4096 and 1 <= h <= 4096):
        raise ValueError(f"implausible map size {w}x{h}")
    cells = w * h

    i = 0
    while i < cells:
        r.i16()          # floor
        r.i16()          # ore
        i += 1 + r.u8()

    found = []
    i = 0
    while i < cells:
        if r.p >= len(r.d):
            raise ValueError(f"map region ended early at tile {i}/{cells}")
        bid = r.i16()
        packed = r.u8()
        had_entity = packed & 1
        had_data = packed & 4
        if had_data:
            r.raw(7)
        is_center = False
        if had_entity:
            is_center = r.u8() == 1
        name = blocks[bid] if 0 <= bid < len(blocks) else f"<id {bid}>"

        if had_entity and is_center:
            payload = r.raw(r.i32())
            if name in PROCESSORS:
                entry = {"x": i % w, "y": i // w, "block": name,
                         "code": None, "links": []}
                hit = _scan_config(payload)
                if hit:
                    code, links = hit
                    entry["code"] = code
                    entry["links"] = [{"name": n, "x": lx, "y": ly} for n, lx, ly in links]
                else:
                    entry["error"] = "no config blob found in the building payload"
                found.append(entry)
        elif not had_entity and not had_data:
            # SaveVersion.java:271 -- consecutives are written only when the tile
            # has no building at all; a non-center multiblock tile writes nothing
            i += r.u8()
        i += 1

    if r.p != len(r.d):
        raise ValueError(f"map region has {len(r.d) - r.p} byte(s) left over")
    return w, h, found


def _parse_legacy_map(reg: bytes, blocks: list[str]):
    """Parse a legacy (version <= 10) map region. Raises if it is not one."""
    r = Reader(reg)
    w, h = r.u16(), r.u16()
    if not (1 <= w <= 4096 and 1 <= h <= 4096):
        raise ValueError(f"implausible map size {w}x{h}")
    cells = w * h

    i = 0
    while i < cells:
        r.i16()          # floor
        r.i16()          # ore
        i += 1 + r.u8()

    found = []
    i = 0
    while i < cells:
        if r.p + 2 > len(r.d):
            raise ValueError(f"legacy map region ended early at tile {i}/{cells}")
        bid = r.i16()
        name = blocks[bid] if 0 <= bid < len(blocks) else f"<id {bid}>"
        if bid != 0:                       # Block.hasBuilding() == destructible || update
            payload = r.raw(r.u16())       # readLegacyShortChunk: u16 length + payload
            if name in PROCESSORS:
                entry = {"x": i % w, "y": i // w, "block": name,
                         "code": None, "links": []}
                try:
                    hit = _scan_config(payload)
                except Exception as e:
                    hit = None
                    entry["error"] = f"config scan failed: {type(e).__name__}: {e}"
                if hit:
                    code, links = hit
                    entry["code"] = code
                    entry["links"] = [{"name": n, "x": lx, "y": ly} for n, lx, ly in links]
                elif "error" not in entry:
                    entry["error"] = "no config blob found in the building payload"
                found.append(entry)
        i += 1

    if r.p != len(r.d):
        raise ValueError(f"legacy map region has {len(r.d) - r.p} byte(s) left over")
    return w, h, found


def _proc_entry(name: str, x: int, y: int, payload: bytes) -> dict:
    """Build one logic-processor record from its building payload."""
    entry = {"x": x, "y": y, "block": name, "code": None, "links": []}
    try:
        hit = _scan_config(payload)
    except Exception as e:
        hit = None
        entry["error"] = f"config scan failed: {type(e).__name__}: {e}"
    if hit:
        code, links = hit
        entry["code"] = code
        entry["links"] = [{"name": n, "x": lx, "y": ly} for n, lx, ly in links]
    elif "error" not in entry:
        entry["error"] = "no config blob found in the building payload"
    return entry


def _parse_shortchunk_map(reg: bytes, blocks: list[str]):
    """Parse a v7-v9 (ShortChunkSaveVersion) map region.

    The floor pass is the same as the modern layout. The block pass reads a
    ``block short`` plus a packed byte for EVERY tile:

    * bit0 = this tile has an entity
    * bit1 = old data format, one trailing byte (only when there is no entity)
    * bit2 = new data format, seven trailing bytes

    A ``u16``-length chunk follows for centre tiles that carry an entity,
    regardless of what the block is -- either a real building payload or a
    region to skip. That is why this parser needs no "hasBuilding" table:
    the packed byte already says whether a chunk is there.
    """
    r = Reader(reg)
    w, h = r.u16(), r.u16()
    if not (1 <= w <= 4096 and 1 <= h <= 4096):
        raise ValueError(f"implausible map size {w}x{h}")
    cells = w * h

    i = 0
    while i < cells:
        r.i16()          # floor
        r.i16()          # ore
        i += 1 + r.u8()

    found = []
    i = 0
    while i < cells:
        if r.p + 3 > len(r.d):
            raise ValueError(f"shortchunk map region ended early at tile {i}/{cells}")
        bid = r.i16()
        packed = r.u8()
        had_entity = packed & 1
        had_old = packed & 2
        had_new = packed & 4
        if had_new:
            r.raw(7)     # data, floorData, overlayData, extra int
        is_center = True
        if had_entity:
            is_center = r.u8() != 0

        name = blocks[bid] if 0 <= bid < len(blocks) else f"<id {bid}>"
        # the nesting matters: had_entity and hadDataOld are alternatives in the
        # source, not independent flags. A non-centre tile of a multi-tile
        # building carries an entity and therefore reads NO extra byte at all --
        # putting the else-if on the outer level desyncs every map with one.
        if had_entity:
            if is_center:
                payload = r.raw(r.u16())
                if name in PROCESSORS:
                    found.append(_proc_entry(name, i % w, i // w, payload))
        elif had_old or had_new:
            if had_old:
                r.raw(1)
        else:
            i += r.u8()  # consecutive tiles with the same block
        i += 1

    if r.p != len(r.d):
        raise ValueError(f"shortchunk map region has {len(r.d) - r.p} byte(s) left over")
    return w, h, found


def _unwrap_save(raw: bytes) -> bytes:
    """A .msav is either a zlib stream that contains `MSAV`, or a plain `MSAV` blob.

    Both exist in the wild: files written by `SaveIO` go through
    `new InflaterInputStream(...)` (so the whole file is one zlib stream), while
    some third-party uploads hand back the already-inflated bytes.
    """
    if raw[:4] == b"MSAV":
        return raw
    try:
        out = zlib.decompress(raw)
    except zlib.error as e:
        raise ValueError(f"not a save file: neither zlib nor MSAV ({e})") from e
    if out[:4] != b"MSAV":
        raise ValueError(f"not a save file: zlib payload starts {out[:8]!r}")
    return out


def read_save(path) -> dict:
    data = _unwrap_save(Path(path).read_bytes())
    r = Reader(data)
    header = r.raw(4)
    if header != b"MSAV":
        raise ValueError(f"not a save file (header {header!r})")
    version = r.i32()

    regions = _split_regions(data, r.p)
    if not regions:
        raise ValueError("no regions after the header")

    meta = _read_string_map(Reader(regions[0]))
    want = (int(meta.get("width", 0)), int(meta.get("height", 0)))

    for mi in range(1, len(regions)):
        blocks = None
        for ci in range(1, mi):
            try:
                blocks = _content_blocks(regions[ci])
            except Exception:
                blocks = None
            if blocks:
                break
        if not blocks:
            continue
        try:
            if version <= 6:
                w, h, found = _parse_legacy_map(regions[mi], blocks)
            elif version <= 9:
                w, h, found = _parse_shortchunk_map(regions[mi], blocks)
            else:
                w, h, found = _parse_map(regions[mi], blocks)
        except Exception:
            continue
        if want[0] and (w, h) != want:
            # dimensions must agree with the meta region; a wrong region cannot
            continue
        return {
            "file": str(path), "version": version, "width": w, "height": h,
            "block_types": len(blocks), "regions": len(regions),
            "logic": found,
        }

    raise ValueError(
        f"no region parses as a {want[0]}x{want[1] if False else want[1]} map "
        f"(version {version}, {len(regions)} region(s))"
        if False else
        f"no region parses as a {want[0]}x{want[1]} map (version {version}, "
        f"{len(regions)} region(s), "
        f"{'legacy' if version <= 6 else 'shortchunk' if version <= 9 else 'modern'} layout)"
    )


def main(argv) -> int:
    if len(argv) < 2:
        print("usage: map_logic.py <save.msav> [--code]", file=sys.stderr)
        return 2
    show = "--code" in argv
    paths = [a for a in argv[1:] if not a.startswith("--")]
    bad = 0
    for p in paths:
        try:
            info = read_save(p)
        except Exception as e:
            print(f"{p}: FAILED -- {type(e).__name__}: {e}")
            bad += 1
            continue
        print(f"{p}  v{info['version']}  {info['width']}x{info['height']}  "
              f"{info['block_types']} block types  {len(info['logic'])} logic processor(s)")
        for t in info["logic"]:
            code = t["code"]
            if code is None:
                print(f"  !! ({t['x']},{t['y']}) {t['block']}: {t['error']}")
                continue
            lines = len(code.splitlines())
            print(f"  ({t['x']},{t['y']}) {t['block']}  {lines} line(s), {len(t['links'])} link(s)")
            for l in t["links"]:
                print(f"       link {l['name']} -> ({l['x']},{l['y']})")
            if show:
                for n, line in enumerate(code.splitlines(), 1):
                    print(f"       {n:>3} {line}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
