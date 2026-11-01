"""mindustry.top map-site client: list / search / detail / download.

The site (https://www.mindustry.top/map) is a Nuxt 3 SPA; the data lives on a
separate API host. Finding the real endpoints took a detour worth recording:

* the SPA calls its fetch wrapper with paths like ``/api/maps/list``, but
  ``function xh(e){return e?.startsWith("/api/") ? Ci + e?.substring(5) : e}``
  with ``Ci = "https://api.mindustry.top/"`` strips the ``/api/`` prefix, so the
  wire URL is ``https://api.mindustry.top/maps/list`` -- NOT ``/api/maps/list``
  (every ``/api/...`` guess returns 404 with an empty body).
* the list endpoint takes just two query parameters, ``begin`` and ``search``
  (from the bundled chunk ``_nuxt/nWpsb5fo.js``), not page/limit.
* ``/maps/<id>.json`` is the detail, ``/maps/<id>.msav`` the blueprint download.

Be polite: the site is community-run and the user warned about rate limits, so
every request sleeps ``RATE`` seconds and failures retry with backoff.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import zlib
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.mindustry.top"
UA = "mlog-toolkit/1.0 (+https://github.com/EnernalIliary/mindustry-logic-lab)"
RATE = 1.1          # seconds between requests, globally
RETRIES = 3

_last = [0.0]


def _throttle() -> None:
    wait = RATE - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()


def _request(path: str, params: dict | None = None, raw: bool = False) -> bytes:
    url = BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read()
        except urllib.error.HTTPError as e:                # 4xx = real answer, not weather
            if 400 <= e.code < 500:
                raise RuntimeError(f"GET {url} -> HTTP {e.code}") from e
            last_err = e
            time.sleep(1.5 * (attempt + 1) ** 2)
        except (urllib.error.URLError, OSError) as e:      # SSL resets happen
            last_err = e
            time.sleep(1.5 * (attempt + 1) ** 2)
    raise RuntimeError(f"GET {url} failed after {RETRIES} tries: {last_err}")


def _json(path: str, params: dict | None = None):
    return json.loads(_request(path, params).decode("utf-8"))


def list_maps(search: str | None = None, begin: int | str | None = None) -> list[dict]:
    """One page of the map list. `begin` is the site's own cursor (it sent the
    previous page's last id in the SPA), `search` is free text."""
    data = _json("/maps/list", {"begin": begin, "search": search})
    return data if isinstance(data, list) else []


def detail(map_id: int | str) -> dict:
    return _json(f"/maps/{map_id}.json")


IPFS = "https://ipfs.mindustry.top"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                       # stop at the 302 and read Location


def _ipfs_url(map_id: int | str) -> str:
    """The api host stores blobs on ipfs and 302s to their gateway."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(f"{BASE}/maps/{map_id}.msav",
                                 headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with opener.open(req, timeout=30) as r:
            return r.geturl()
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") if e.headers else None
        if e.code in (301, 302, 303, 307, 308) and loc:
            return loc
        raise RuntimeError(f"GET /maps/{map_id}.msav -> HTTP {e.code}") from e


def _unwrap(body: bytes) -> bytes:
    """Some uploads are stored zlib/gzip-compressed behind the same `.msav` URL."""
    for wbits in (zlib.MAX_WBITS, zlib.MAX_WBITS | 32):
        try:
            out = zlib.decompress(body, wbits)
        except zlib.error:
            continue
        if out.startswith(b"MSAV"):
            return out
    return body


def download(map_id: int | str, dest: Path) -> Path:
    """Fetch one .msav. The slow part is the IPFS gateway, not the api host."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = _ipfs_url(map_id)
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read()
            if not (body.startswith(b"MSAV") or body[:1] == b"x"):
                # x\x9c = zlib-wrapped save; MSAV = already inflated
                raise RuntimeError(f"{url}: not a schematic (starts {body[:8]!r})")
            dest.write_bytes(body)
            return dest
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            time.sleep(2.0 * (attempt + 1) ** 2)
    raise RuntimeError(f"GET {url} failed after {RETRIES} tries: {last_err}")


def _fmt(m: dict) -> str:
    tags = " ".join(m.get("tags") or [])
    return (f"{m.get('id'):>6}  {m.get('mode','?'):<7} {m.get('width',0):>4}x{m.get('height',0):<4} "
            f"{m.get('name','')[:40]:<40} | {tags}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="map_site.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list/search maps")
    p.add_argument("--search", default=None)
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("detail", help="one map's metadata")
    p.add_argument("id")

    p = sub.add_parser("fetch", help="download .msav files")
    p.add_argument("ids", nargs="+")
    p.add_argument("--out", default="out/maps")

    args = ap.parse_args(argv)

    if args.cmd == "list":
        seen: list[dict] = []
        begin = None
        for page in range(max(1, args.pages)):
            batch = list_maps(args.search, begin)
            if not batch:
                break
            seen.extend(batch)
            begin = batch[-1].get("id")
            print(f"# page {page + 1}: {len(batch)} map(s), cursor -> {begin}", file=sys.stderr)
            if page + 1 < args.pages:
                time.sleep(RATE)
        if args.json:
            print(json.dumps(seen, ensure_ascii=False, indent=1))
        else:
            for m in seen:
                print(_fmt(m))
            print(f"\n{len(seen)} map(s)")
        return 0

    if args.cmd == "detail":
        d = detail(args.id)
        print(json.dumps(d, ensure_ascii=False, indent=2)[:4000])
        return 0

    out = Path(args.out)
    for mid in args.ids:
        mid = mid.strip()
        if not mid:
            continue
        dest = download(mid, out / f"mindustrytop-{mid}.msav")
        print(f"{dest}  {dest.stat().st_size} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
