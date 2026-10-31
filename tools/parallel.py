"""Run one job per file across the machine's cores, sized to what is actually free.

Why this is not simply ``cpu_count()`` workers:

* a corpus of 1421 saves runs from 20 KB to 4.5 MB, and a worker's peak RSS is
  set by whichever file it happens to be holding -- so the ceiling is memory,
  not cores;
* the memory that matters is *available right now* (the game and a browser are
  usually running), not the installed total;
* leaving a couple of cores alone keeps the machine usable while a 5 minute
  sweep runs in the background.

So the worker count is the smallest of: cores - ``cpu_reserve``,
``(available - reserve) / mem_per_worker_mb``, and the number of items.

Windows note: ``ProcessPoolExecutor`` starts children with *spawn*, so a script
that calls :func:`run` must keep its body under ``if __name__ == "__main__":``
-- otherwise every child re-executes the script and forks forever. Every sweep
in ``tools/`` does that; the workers are plain top-level functions so they can
be pickled by name.

Standalone use prints the machine's numbers and what they imply::

    python parallel.py
    python parallel.py --mem-per-worker 400 --items 1421
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Measured on 32 cores / 32 GB (2.2 GB free, so ~11 workers here): 8 workers
# took 155 s for the full analysis pass, 16 took 128 s -- the work does scale,
# memory is what stops it.  192 MB per *light* worker is what those runs
# actually used; the heavy batch (>= 1 MB) gets its own, much larger estimate
# below, and that split is what keeps a 4.5 MB save from being run 13 at once.
MEM_PER_WORKER_MB = 192.0
CPU_RESERVE = 2


def _meminfo_windows() -> tuple[int, int] | None:
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return int(st.ullTotalPhys // 2**20), int(st.ullAvailPhys // 2**20)
    except Exception:
        return None


def _meminfo_linux() -> tuple[int, int] | None:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = int(v.split()[0]) // 1024
        return info.get("MemTotal"), info.get("MemAvailable")
    except Exception:
        return None


def sysinfo() -> dict:
    """Cores plus memory in MB. Never raises -- unknown fields come back None."""
    total = avail = None
    pair = _meminfo_windows() if os.name == "nt" else _meminfo_linux()
    if pair:
        total, avail = pair
    return {
        "cpu": os.cpu_count() or 1,
        "total_mb": total,
        "avail_mb": avail,
    }


def pick_workers(
    n_items: int,
    mem_per_worker_mb: float = MEM_PER_WORKER_MB,
    cpu_reserve: int = CPU_RESERVE,
) -> tuple[int, str]:
    """How many workers to use, plus a one-line reason for that number."""
    si = sysinfo()
    cpu = si["cpu"]
    by_cpu = max(1, cpu - max(0, cpu_reserve))
    if si["avail_mb"] is None:
        by_mem = by_cpu
        mem_note = "memory unknown, assuming it fits"
    else:
        reserve = max(1024, int(si["total_mb"] * 0.15))
        usable = max(0, si["avail_mb"] - reserve)
        by_mem = max(1, int(usable // max(1.0, mem_per_worker_mb)))
        mem_note = (
            f"{si['avail_mb']} MB free - {reserve} MB reserve = {usable} MB "
            f"/ {mem_per_worker_mb:g} MB per worker"
        )
    workers = max(1, min(by_cpu, by_mem, max(1, n_items)))
    note = (
        f"{cpu} core(s), keep {max(0, cpu_reserve)}, so {by_cpu} by CPU; "
        f"{mem_note}, so {by_mem} by RAM; {n_items} item(s)"
    )
    return workers, note


def order_by_size(items: list) -> list:
    """Biggest first, so the slow tail starts early instead of finishing late."""
    def size(x):
        try:
            return os.path.getsize(x)
        except OSError:
            return 0

    return sorted(items, key=size, reverse=True)


def _size_mb(x) -> float:
    try:
        return os.path.getsize(x) / 2**20
    except OSError:
        return 0.0


def _pool(fn, items, workers: int, label: str, progress: bool, stall_s: float) -> list:
    """Run one batch; returns results in ``items`` order."""
    from concurrent.futures import ProcessPoolExecutor, as_completed

    t0 = time.time()
    out: list = [None] * len(items)
    step = max(1, len(items) // 20)
    last_report = t0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fn, x): i for i, x in enumerate(items)}
        done = 0
        for fut in as_completed(futures):
            out[futures[fut]] = fut.result()
            done += 1
            now = time.time()
            if progress and (done % step == 0 or done == len(items)):
                el = max(1e-6, now - t0)
                rate = done / el
                eta = (len(items) - done) / rate if rate else 0.0
                print(f"  {label} {done}/{len(items)}  {rate:.1f}/s  eta {eta:.0f}s", flush=True)
                last_report = now
            elif progress and now - last_report > stall_s:
                # Say what is still outstanding instead of looking hung: one
                # 4 MB save with 6600 processors can take longer than the other
                # 1419 files together.
                pending = [f for f in futures if not f.done()]
                pending.sort(key=lambda f: -_size_mb(items[futures[f]]))
                names = ", ".join(Path(items[futures[f]]).name for f in pending[:3])
                print(f"  {label} {done}/{len(items)}  still running: {names}", flush=True)
                last_report = now
    return out


def run(
    fn,
    items,
    workers: int | None = None,
    label: str = "",
    mem_per_worker_mb: float = MEM_PER_WORKER_MB,
    cpu_reserve: int = CPU_RESERVE,
    sort_by_size: bool = True,
    progress: bool = True,
    heavy_mb: float = 1.0,
    mem_per_heavy_mb: float | None = None,
    stall_s: float = 45.0,
) -> list:
    """``[fn(x) for x in items]``, spread over processes when that is faster.

    ``fn`` must be importable by name (a top-level function), take one argument
    and return something small -- results travel back over a pipe, so returning
    megabytes of source per file defeats the purpose.  Results come back in the
    same order as ``items``.

    Files above ``heavy_mb`` run in a second, deliberately narrower batch: a
    4 MB save holding 6600 processors needs hundreds of MB of live objects, and
    thirteen of those at once is how a "fast" sweep ends up swapping.  Sizing
    the heavy batch by ``mem_per_heavy_mb`` (default: 200 MB + 120 MB per MB of
    file) keeps the machine's memory as the binding constraint, as it should be.
    """
    items = list(items)
    if not items:
        return []
    if sort_by_size:
        items = order_by_size(items)
    name = label or getattr(fn, "__name__", "job")
    if workers is None:
        workers, note = pick_workers(len(items), mem_per_worker_mb, cpu_reserve)
    else:
        note = "forced by --workers"

    heavy = [x for x in items if _size_mb(x) >= heavy_mb]
    light = [x for x in items if _size_mb(x) < heavy_mb]

    if workers <= 1:
        if progress:
            print(f"[serial]   {name}: {len(items)} item(s) -- {note}", flush=True)
        return [fn(x) for x in items]

    if progress:
        print(f"[parallel] {name}: {len(items)} item(s) x {workers} worker(s) -- {note}", flush=True)
    t0 = time.time()
    results: list = []
    try:
        if light:
            if progress and heavy:
                print(f"  phase 1: {len(light)} light item(s) (< {heavy_mb:g} MB)", flush=True)
            results += _pool(fn, light, workers, name, progress, stall_s)
        if heavy:
            biggest = max(_size_mb(x) for x in heavy)
            per = mem_per_heavy_mb if mem_per_heavy_mb is not None else 200 + 120 * biggest
            heavy_workers = workers
            si = sysinfo()
            if si["avail_mb"] is not None:
                reserve = max(1024, int(si["total_mb"] * 0.15))
                heavy_workers = max(1, min(workers, int(max(0, si["avail_mb"] - reserve) // per)))
            if progress:
                print(f"  phase 2: {len(heavy)} heavy item(s) (biggest {biggest:.1f} MB, "
                      f"~{per:.0f} MB each) x {heavy_workers} worker(s)", flush=True)
            results += _pool(fn, heavy, heavy_workers, name + " [heavy]", progress, stall_s)
    except Exception as exc:  # spawn/pickle/permission problems: correctness over speed
        if progress:
            print(f"[serial fallback] {name}: pool failed ({type(exc).__name__}: {exc})", flush=True)
        return [fn(x) for x in items]

    if progress:
        el = time.time() - t0
        print(f"  {name}: {len(items)} item(s) in {el:.1f}s ({len(items) / max(el, 1e-6):.1f}/s)", flush=True)
    return results


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="what this machine can parallelise to")
    ap.add_argument("--items", type=int, default=0, help="how many files the job has")
    ap.add_argument("--mem-per-worker", type=float, default=MEM_PER_WORKER_MB)
    ap.add_argument("--cpu-reserve", type=int, default=CPU_RESERVE)
    a = ap.parse_args(argv)

    si = sysinfo()
    print(f"cpu        : {si['cpu']}")
    print(f"memory     : {si['total_mb']} MB total, {si['avail_mb']} MB available")
    print(f"per worker : {a.mem_per_worker:g} MB (peak RSS of the biggest file)")
    workers, note = pick_workers(a.items or si["cpu"] * 4, a.mem_per_worker, a.cpu_reserve)
    print(f"workers    : {workers}")
    print(f"because    : {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
