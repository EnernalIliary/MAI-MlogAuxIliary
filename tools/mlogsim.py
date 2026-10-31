"""mlogsim -- run real Mindustry mlog on the real LExecutor, headless.

No JDK, no compilation, no mods: starts the official server jar and uses its
console `js` command (Rhino) to place blocks, load code, step
`LExecutor.runOnce()` manually and read back every variable plus the print
buffer.

    python tools/mlogsim.py out/prog.mlog --steps 50
    python tools/mlogsim.py blueprints/x.msch --steps 200
    python tools/mlogsim.py scene.msav --ticks 60 --units flare:2

By default the world is a blank 64x64 (`--world N` to resize, 0 or `--host` to
host a random map). Blueprints are at most 64x64, so the default fits any
schematic. No host means no 6567 port and no random-map load: ~1s per run
faster; making the world smaller than 64 does not speed anything up, because
instruction execution does not touch the map and `tile` lookup is O(1).
`--host` is only needed for programs that read real terrain or `@mapw`.

For a schematic every tile is placed, every logic block is compiled with its
named links, and the logic blocks are stepped in schematic order (so a
two-processor cross-read behaves like the save it came from).

World simulation (`--ticks` only; `--steps` stays pure logic stepping):

    --update all    (default for --ticks) every building is updated each tick,
                    so factories, conveyors, drills, power and turrets run
    --update none   logic only; buildings stay frozen

`--update all` snapshots Groups.build, skips processors (the step loop drives
those), pins every consumer's `power.status` to 1.0 (`--power full`, default;
`--power none` pins 0.0), and sets `state` to playing for the duration of the
loop only -- otherwise `Building.update()`/`updateConsumption()` no-op, and
the server's own frame loop never gets a chance to advance the world.

Units spawned with `--units` are driven by their controller in the same mode:
under `--update all` every unit whose controller is a `LogicAI` gets one real
`Unit.update()` per tick, so `ucontrol move/approach/stop/boost` actually moves
it, `within`/`@x`/`@y` see the result, and `@unit` stays the same object.
Units that are not logic-controlled are not updated by default (wild AI is not
simulated; `--unit-ai all` turns it on), and a unit whose 10s control timeout
expires simply stops -- it does not revert to its faction AI. This is "B
level": real executor, real unit update, real `LogicAI`, but no wave AI, combat
targeting, power graphs or terrain collision detail in the world. Passing here
does not replace a real game run.
`--update none` keeps units frozen where they spawned. `--unit-pos X,Y` spawns
them at a tile coordinate instead of two tiles off the processor, which makes
motion demos reproducible (e.g. start a unit exactly on its orbit).
Power graphs are not simulated; `--power` is the hook reserved for that.

Scenes (`--save FILE`) load a real `.msav`: the map, teams, cores, buildings
and units come with it, so logic runs against the exact layout it will meet in
game. The processor is auto-placed on a free tile beside your core (`--base
X,Y` overrides) and the world size comes from the save, not `--world`. The
save's world clock is reset to 0 once, so `@tick`/`@time` start at 0 and runs
repeat; the absolute clock value inside a save is otherwise arbitrary.

Links (`--links auto`, default for a `.mlog`): a plain `.mlog` has no link
table, so `read cell1 0` normally compiles to a null constant. The simulator
finds names the program reads but never assigns and, when the name looks like
a link (`cell1`, `bank1`, `message1`, `dome1`), places the matching block on a
free tile next to the processor and wires it (10-tile link range, same team).
`--links off` restores the raw behaviour. A `.msch` keeps its own links.
`--unit-ai all` also updates units that no processor controls, i.e. the save's
own AI: turrets are buildings and already fire under `--update all`, while
this lets mobile defenders and attackers act. `CommandAI` units stay passive
unless the save gave them a command -- the stock server has no commander.

Weather (`--ticks` only) is the real state machine, not a stub: every tick each
`state.rules.weather` entry loses `Time.delta` from its cooldown, and an entry
whose cooldown expired (or that is `always`) creates its weather with a
duration/frequency randomized from a fixed seed, so a run repeats. Active
weathers fade/expire through `Groups.weather.update()`. `weathersense` and
`weatherset` are privileged: a program that uses them (or `setrule`, `message`,
`cutscene`, `spawn`, ...) is placed as a world processor automatically
(`--processor auto`), because a micro processor's parser silently replaces
those lines with invalid statements. `--weather NAME[:INTENSITY]` starts with
that weather already active (repeatable) instead of a clear sky.

Tick semantics: Mindustry adds `edelta()*ipt` to the accumulator *after* the
instruction loop, so the first update after placement executes nothing --
`--ticks N` therefore runs N-1 active ticks and `@tick` starts at 2. The print
buffer is capped by the game at `maxTextBuffer` = 400 chars; longer output is
truncated, not lost by the harness.

Preflight (default on; `--no-preflight` disables): the program must compile
(`executor.initialized()`), every `--units` entry must actually spawn, and at
tick 1 every spawned unit must still be alive. That last one matters: a unit
spawned on a solid tile is killed by its first `update()`, which otherwise
looks like "the unit never moved" only after the whole run has finished.
Failures abort with the cause instead of a silent wrong result.

Combat (`--ticks` only) mirrors `Logic.updateEntities()`: before the logic
step, `Groups.bullet.updatePhysics()` and `Groups.unit.updatePhysics()` run;
after it, `Groups.bullet.update()` and `.collide()`. A unit's `ucontrol target`
shots then really travel, hit buildings, and drain health. `--combat off`
leaves bullets frozen: useful to isolate aiming, but nothing takes damage.
Not a battle model: waves, enemy AI, death/despawn edge cases and block
destruction consequences are still the game's job.

Trace (`--ticks` only): `--trace FILE` writes one CSV row per tick per unit
whose controller is a `LogicAI` (a unit a processor took over):

    unit,tick,ux,uy,uhp,range,tx,ty,dist,cx,cy,chp,bullets,shots

`ux/uy` are tiles; `range` is `@range` (tiles); `tx/ty/dist` are the current
`ucontrol target` point (blank unless aim mode is target); `cx/cy/chp` are the
nearest enemy core (blank if none); `bullets` is `Groups.bullet.size()`;
`shots` sums each weapon mount's `totalShots`. Works with `--update none` too.

Assertions run on trace rows: `--assert EXPR` on every row (invariant, e.g.
`dist >= range - 1.5`), `--assert-last EXPR` on the final row only (outcome,
e.g. `chp < 1100`). Column names are variables, `eval` has no builtins, and
any failure exits 3. `ASSERT PASS` prints the row/expr counts.

Default runtime is the newest local pair, overridable with env vars:
    MLOGSIM_JAR   (default: out/_server160.jar, then out/_server159.jar, then out/_server146.jar)
    MLOGSIM_JAVA  (default: out/jre21/.../java.exe, then the Steam jre)

Saves made in build 160 (the current Mindustry format) need the v160 server:
v159.3 loads build-160 saves whose map uses the old tile encoding, and fails
with "Error reading region map" on the newer one. Keep both jars around; the
default is v160.7, and `MLOGSIM_JAR=.../_server159.jar` switches back.

Java 21+ is required (the Steam jlink image lacks java.logging).
v146 is kept for comparison only: it lacks `LogicBlock.readable`, so
cross-processor `read` by name always yields null there.

Child lifetime: the Java child goes into a Windows job object with
KILL_ON_JOB_CLOSE, so however this process ends -- a gate timeout, Ctrl-C, a
crash -- the kernel takes the server down with it. It used to survive (61 of
them once starved the machine until every probe failed with "insufficient
memory"). `--sweep-orphans` kills leftovers from older runs (only our jars,
only when the parent is gone) and exits.
Note: micro-processor runs 2 instructions/tick, logic-processor 8,
hyper-processor 25. `--steps` counts *instructions*, not ticks.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "mindustry-blueprint", "tools")

# Statements whose LStatement.privileged() is true: a micro processor silently
# turns them into InvalidStatement (LParser.read()), i.e. the line vanishes.
# Only the first word of a line is checked; operands are not.
# clientdata and localeprint are privileged too: ClientDataStatement
# (LStatements.java:2182-2184) and LocalePrintStatement (:2622-2625) both return
# true from privileged(), so a micro processor drops them -- the run then shows
# an empty instruction list, which reads like a version gap until you look.
PRIVILEGED_RE = re.compile(
    r"^\s*(weathersense|weatherset|setrule|message|cutscene|effect|fetch|sync|"
    r"getblock|setblock|spawn|status|clientdata|localeprint)\b", re.M)
STEAM_JAVA = r"E:\Program Files (x86)\Steam\steamapps\common\Mindustry\jre\bin\java.exe"
V160_JAR = os.path.join(ROOT, "out", "_server160.jar")
V159_JAR = os.path.join(ROOT, "out", "_server159.jar")
V159_JAVA = os.path.join(ROOT, "out", "jre21", "jdk-21.0.5+11-jre", "bin", "java.exe")
V146_JAR = os.path.join(ROOT, "out", "_server146.jar")


def _default_jar() -> str:
    if os.environ.get("MLOGSIM_JAR"):
        return os.environ["MLOGSIM_JAR"]
    for jar in (V160_JAR, V159_JAR, V146_JAR):
        if os.path.exists(jar):
            return jar
    return V146_JAR


def _default_java() -> str:
    env = os.environ.get("MLOGSIM_JAVA")
    if env:
        return env
    if os.path.exists(V159_JAVA):
        return V159_JAVA
    return STEAM_JAVA


def _abs_path(p: str) -> str:
    """Resolve a jar/java path against the caller's cwd; keep bare commands as-is.

    Server() starts the Java child with cwd=<workdir under out/_sim> (Java must
    not write to %TEMP%), so a relative path would be resolved *there* and fail
    with "Unable to access jarfile". A bare name (e.g. --java java) must still
    go through PATH, so only paths with a directory part or an existing file
    are rewritten.
    """
    if os.path.dirname(p) or os.path.exists(p):
        return os.path.abspath(p)
    return p


# ------------------------------------------------------------- child lifetime
# The Java child used to outlive its Python parent: when the parent was killed
# (a gate timeout, Ctrl-C, a crashed probe) the JVM kept the pipe and stayed
# resident. 61 of them once piled up and starved the box so badly that the whole
# probes tier failed with "There is insufficient memory for the Java Runtime
# Environment to continue". On Windows the fix is a job object with
# KILL_ON_JOB_CLOSE: the job handle belongs to *this* process, so whenever this
# process ends for any reason the kernel kills every child inside it. atexit is
# the polite path; the job object is the one that survives a hard kill.
_LIVE_SERVERS = []


def _win_kill_with_parent(proc):
    """Put a Windows child in a job that dies with this process; None elsewhere."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class _BASIC_LIMITS(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _EXTENDED_LIMITS(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC_LIMITS),
                        ("IoInfo", _IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _EXTENDED_LIMITS()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return None
        if not k32.AssignProcessToJobObject(job, ctypes.c_void_p(int(proc._handle))):
            k32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _close_live_servers():
    for srv in list(_LIVE_SERVERS):
        try:
            srv.close()
        except Exception:
            pass


atexit.register(_close_live_servers)


# Any java.exe still running one of our jars whose parent is gone is a leftover
# from a killed run. Only our jars count: the game itself also runs a java.exe.
_JAR_MARKERS = ("_server160.jar", "_server159.jar", "_server146.jar", "jre21")


def _ps_json(script):
    exe = shutil.which("powershell") or "powershell"
    out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=180)
    text = (out.stdout or "").strip()
    return json.loads(text) if text else None


def sweep_orphans(dry_run=False):
    """Kill simulator JVMs whose Python parent no longer exists; return their pids."""
    if os.name != "nt":
        return []
    try:
        procs = _ps_json("Get-CimInstance Win32_Process | "
                         "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
                         "ConvertTo-Json -Compress -Depth 3")
    except Exception:
        return []
    if not procs:
        return []
    if isinstance(procs, dict):
        procs = [procs]
    live = set()
    for item in procs:
        try:
            live.add(int(item.get("ProcessId") or 0))
        except (TypeError, ValueError):
            pass
    killed = []
    for item in procs:
        try:
            pid = int(item.get("ProcessId") or 0)
            ppid = int(item.get("ParentProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or str(item.get("Name") or "").lower() != "java.exe":
            continue
        cmd = str(item.get("CommandLine") or "")
        if not any(mark in cmd for mark in _JAR_MARKERS):
            continue
        if ppid in live:
            continue          # parent still alive: not an orphan (pid reuse reads as alive)
        if dry_run:
            killed.append(pid)
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=30)
            killed.append(pid)
        except Exception:
            pass
    return killed


class SimError(RuntimeError):
    pass


class Server:
    def __init__(self, java: str, jar: str, world_size: int | None = 64,
                 save: str | None = None):
        # Java runs with cwd=<workdir> below, so relative jar/java paths must be
        # resolved against the *caller's* cwd first (same meaning as the CLI).
        java = _abs_path(java)
        jar = _abs_path(jar)
        # writes must stay inside the workspace: Java child processes are not
        # allowed to write under %TEMP% (sandbox denies with "access denied")
        base = os.path.join(ROOT, "out", "_sim")
        os.makedirs(base, exist_ok=True)
        self.workdir = tempfile.mkdtemp(prefix="run-", dir=base)
        self._save = save
        self._base0 = None
        self._init_src = None
        self._core_spec = None
        self._unit_spec = None
        self._unit_pos = None
        self._weather_specs = []
        os.makedirs(os.path.join(self.workdir, "config", "logs"), exist_ok=True)
        self.proc = subprocess.Popen(
            [java, "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8", "-jar", jar],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.workdir,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._job = _win_kill_with_parent(self.proc)
        self._closed = False
        _LIVE_SERVERS.append(self)
        self.lines: queue.Queue = queue.Queue()
        self.buffer: list[str] = []
        self.all: list[str] = []
        self.exit_code = None
        threading.Thread(target=self._pump, daemon=True).start()
        try:
            self.wait_for("Server loaded.", 60)
            if save:
                # a save brings its own map, teams, cores, buildings and units;
                # `load` takes the slot name without the .msav suffix
                saves = os.path.join(self.workdir, "config", "saves")
                os.makedirs(saves, exist_ok=True)
                shutil.copyfile(save, os.path.join(saves, "scene.msav"))
                self.send("load scene")
                self.wait_for("Save loaded.", 120)
                self.send("pause on")
                time.sleep(0.3)
                out = self.js(PICK_BASE_JS)
                base_line = next((ln for ln in out if ln.startswith("BASE\t")), None)
                if base_line is None:
                    raise SimError("could not find an empty tile for the processor")
                _, bx, by = base_line.split("\t")
                self.base = (int(bx), int(by))
                world_line = next((ln for ln in out if ln.startswith("WORLD\t")), "")
                dims = world_line.split("\t", 1)[1].split("x") if "\t" in world_line else []
                self.world_size = int(dims[0]) if dims else None
            elif world_size:
                # no host: Vars.init already built state/world, loadGenerator makes
                # a blank map. Saves the ~0.5s random-map host and all port binding.
                self.world_size = world_size
                self.base = (0, 0)
                self.js(LOAD_JS % {"n": world_size})
            else:
                self.world_size = None
                self.base = (80, 80)
                self.send("host")
                self.wait_for("Opened a server", 120)
                self.send("pause on")
                time.sleep(0.4)
        except Exception:
            self.close()
            raise

    def _pump(self):
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\r\n"))
        self.exit_code = self.proc.wait()
        self.lines.put(None)

    def send(self, cmd: str):
        assert self.proc.stdin is not None
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def wait_for(self, needle: str, timeout: float = 60.0) -> list[str]:
        deadline = time.time() + timeout
        out: list[str] = []
        while time.time() < deadline:
            try:
                line = self.lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                raise SimError(f"server exited (code={self.exit_code}):\n" + "\n".join(self.all[-20:]))
            self.buffer.append(line)
            self.all.append(line)
            out.append(line)
            if needle in line:
                return out
        raise SimError(f"timeout waiting for {needle!r}; tail:\n" + "\n".join(self.all[-20:]))

    def js(self, script: str, timeout: float = 60.0) -> list[str]:
        """Run one `js` command; return the lines it printed."""
        self.buffer.clear()
        # the console reads one line per command: multi-line scripts would be
        # parsed as separate console commands
        self.send("js " + " ".join(script.splitlines()))
        lines = self.wait_for("undefined", timeout)
        out = [ln.split("[none/base.js]:", 1)[1].strip() for ln in lines if "[none/base.js]:" in ln]
        err = [ln for ln in lines if "Exception" in ln or "Error:" in ln]
        if err:
            raise SimError("\n".join(err))
        return out

    def fill_cores(self, spec: str) -> list[dict]:
        """Stock our team's cores; if there is none, drop a core-shard next to base.

        SPEC is `item:amount,item:amount` (item ids as in `sensor ... @copper`).
        """
        items: dict[str, int] = {}
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            name, _, amount = part.partition(":")
            name = name.strip().lstrip("@")
            if not name:
                raise SimError(f"bad --core-items entry: {part!r}")
            items[name] = int(amount) if amount else 1
        if not items:
            raise SimError("--core-items is empty")
        # auto-placed core: put it in a far corner.  A 3x3 core-shard dropped
        # next to base can be clipped by the auto-link ring (a 3x3 furnace
        # overlapping any of its tiles deletes the whole core).
        limit = self.world_size if self.world_size else 64
        cx = min(max(limit - 5, 2), limit - 3)
        cy = min(max(limit - 5, 2), limit - 3)
        out = self.js(CORE_JS % {"spec": json.dumps(items), "cx": cx, "cy": cy})
        cores = []
        for ln in out:
            if not ln.startswith("CORE\t"):
                continue
            _, x, y, name, total = ln.split("\t")
            cores.append({"x": int(x), "y": int(y), "block": name, "total": int(total)})
        if not cores:
            raise SimError("--core-items: no core found and the auto-placed one was not created")
        self.cores = cores
        return cores

    def reset_world(self):
        """Put the world back to its initial state so one JVM can run many jobs."""
        if getattr(self, "_save", None):
            self.send("load scene")
            self.wait_for("Save loaded.", 120)
            self.send("pause on")
            time.sleep(0.2)
            out = self.js(PICK_BASE_JS)
            base_line = next((ln for ln in out if ln.startswith("BASE\t")), None)
            if base_line is None:
                raise SimError("reset: could not find an empty tile for the processor")
            _, bx, by = base_line.split("\t")
            self.base = (int(bx), int(by))
        elif self.world_size:
            self.js(LOAD_JS % {"n": self.world_size})
        else:
            self.send("stop")
            time.sleep(0.2)
            self.send("host")
            self.wait_for("Opened a server", 120)
            self.send("pause on")
            time.sleep(0.3)
        if self._base0 is not None:
            self.base = self._base0

    def prepare(self):
        """Re-apply world setup that reset_world() wipes (init-js, cores, units)."""
        if self._init_src:
            self.js(self._init_src)
        if self._core_spec:
            self.fill_cores(self._core_spec)
        if self._unit_spec:
            wx, wy = self._unit_pos or (self.base[0] * 8 + 4, self.base[1] * 8 + 4)
            self.js(UNIT_JS % {"spec": json.dumps(self._unit_spec), "wx": wx, "wy": wy})
        if self._weather_specs:
            for wname, wint in self._weather_specs:
                self.js(weather_js(wname, wint))

    def close(self):
        """Shut the child down; safe to call twice and from atexit."""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            _LIVE_SERVERS.remove(self)
        except ValueError:
            pass
        proc = self.proc
        try:
            if proc.poll() is None:
                self.send("exit")
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except Exception:
                    pass
            job = getattr(self, "_job", None)
            if job:
                try:
                    import ctypes
                    ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(job))
                except Exception:
                    pass
                self._job = None


LOAD_JS = r'''
Vars.world.loadGenerator(%(n)d,%(n)d,function(tiles){
    tiles.fill();
    var floor = Vars.content.block('stone').asFloor();
    for(var x = 0; x < %(n)d; x++){
        for(var y = 0; y < %(n)d; y++){ tiles.get(x,y).setFloor(floor); }
    }
});
print("WORLD\t" + Vars.world.width() + "x" + Vars.world.height());
'''


PICK_BASE_JS = r'''
print("WORLD\t" + Vars.world.width() + "x" + Vars.world.height());
/* a save carries its own world clock; scene runs start from zero so @tick/@time
   are readable and repeatable (the absolute value is meaningless between saves) */
Vars.state.tick = 0; Packages.arc.util.Time.setInternalTime(0); Vars.logicVars.update();
var W = Vars.world, air = Vars.content.block("air");
var own = null, GB = Packages.mindustry.gen.Groups.build;
for(var i = 0; i < GB.size(); i++){
    var b = GB.index(i);
    if(b.block instanceof Packages.mindustry.world.blocks.storage.CoreBlock
       && b.team == Packages.mindustry.game.Team.sharded){ own = b; break; }
}
var cx = own == null ? 2 : Math.round(own.x / 8), cy = own == null ? 2 : Math.round(own.y / 8);
var found = null;
/* spiral outward: an air tile with a non-solid floor is safe to put a processor on */
for(var r = 3; r < 40 && found == null; r++){
    for(var dx = -r; dx <= r && found == null; dx++){
        for(var dy = -r; dy <= r && found == null; dy++){
            if(Math.max(Math.abs(dx), Math.abs(dy)) != r) continue;
            var x = cx + dx, y = cy + dy;
            if(x < 1 || y < 1 || x >= W.width() - 1 || y >= W.height() - 1) continue;
            var t = W.tile(x, y);
            if(t.block().name != "air" || t.floor().solid || t.floor().isLiquid) continue;
            found = [x, y];
        }
    }
}
if(found == null){ found = [1, 1]; }
print("BASE\t" + found[0] + "\t" + found[1]);
'''


CORE_JS = r'''
var spec = %(spec)s;
var team = Packages.mindustry.game.Team.sharded;
var GB = Packages.mindustry.gen.Groups.build;
var found = 0;
for(var i = 0; i < GB.size(); i++){
    var b = GB.index(i);
    if(!(b.block instanceof Packages.mindustry.world.blocks.storage.CoreBlock)) continue;
    if(b.team != team) continue;
    if(b.items == null) continue;
    found++;
    for(var k in spec){
        var it = Vars.content.item(k);
        if(it == null) throw new Error("unknown item: " + k);
        b.items.add(it, spec[k]);
    }
    print("CORE\t" + Math.round(b.x / 8) + "\t" + Math.round(b.y / 8) + "\t"
          + b.block.name + "\t" + b.items.total());
}
if(found == 0){
    var x = %(cx)d, y = %(cy)d;
    Vars.world.tile(x, y).setBlock(Vars.content.block("core-shard"), team);
    /* Tile.setBlock does NOT update BlockIndexer (Tile.java:208); without this
       ulocate building core finds nothing, and the team's building list is empty */
    Vars.indexer.addIndex(Vars.world.tile(x, y));
    var nb = Vars.world.build(x, y);
    for(var k2 in spec){
        var it2 = Vars.content.item(k2);
        if(it2 == null) throw new Error("unknown item: " + k2);
        nb.items.add(it2, spec[k2]);
    }
    print("CORE\t" + x + "\t" + y + "\tcore-shard\t" + nb.items.total());
}
'''


def place_js(x: int, y: int, block: str = "micro-processor") -> str:
    return ("var T=Packages.mindustry.game.Team.sharded;"
            "Vars.world.tile(%d,%d).setBlock(Vars.content.block('air'), T);"
            "Vars.world.tile(%d,%d).setBlock(Vars.content.block('%s'), T);"
            "print('PLACED');" % (x, y, x, y, block))

STEP_JS = r'''
var p = "%(path)s";
var s = Packages.java.nio.file.Files.readString(
    Packages.java.nio.file.Paths.get(p),
    Packages.java.nio.charset.StandardCharsets.UTF_8);
var b = Vars.world.build(%(x)d,%(y)d);
b.code = s;
/* a .mlog file has no link table; place a matching block next to the processor
   and wire it, so `read cell1 0` / `print message1` behave like the real build.
   Link names follow the game's defaults: memory-cell -> cell1, memory-bank -> bank1. */
var links = %(links)s;
var LL = Packages.mindustry.world.blocks.logic.LogicBlock.LogicLink;
var W = Vars.world, team = Packages.mindustry.game.Team.sharded;
var air = Vars.content.block("air");
var bx = %(x)d, by = %(y)d;
var placed = [];
function freeSpot(size){
    for(var r = 2; r <= 24; r++){
        for(var dx = -r; dx <= r; dx++){
            for(var dy = -r; dy <= r; dy++){
                if(Math.max(Math.abs(dx), Math.abs(dy)) != r) continue;
                var x = bx + dx, y = by + dy;
                if(x < 0 || y < 0 || x + size > W.width() || y + size > W.height()) continue;
                var ok = true;
                for(var i = 0; i < size && ok; i++){
                    for(var j = 0; j < size; j++){
                        var t = W.tile(x + i, y + j);
                        if(t.block() != air || t.build != null){ ok = false; break; }
                    }
                }
                /* a multi-block's non-origin tiles still read as air, so also
                   reject anything overlapping a rectangle placed during this run */
                if(ok){
                    for(var q = 0; q < placed.length; q++){
                        var p = placed[q];
                        if(x < p[0] + p[2] && x + size > p[0]
                           && y < p[1] + p[2] && y + size > p[1]){ ok = false; break; }
                    }
                }
                if(ok) return [x, y];
            }
        }
    }
    return null;
}
for(var k = 0; k < links.length; k++){
    var spec = links[k];
    var blk = Vars.content.block(spec.block);
    if(blk == null){ print("LINKWARN\t" + spec.name + "\tunknown block " + spec.block); continue; }
    var spot = freeSpot(blk.size);
    if(spot == null){ print("LINKWARN\t" + spec.name + "\tno space"); continue; }
    W.tile(spot[0], spot[1]).setBlock(blk, team);
    placed.push([spot[0], spot[1], blk.size]);
    b.links.add(new LL(spot[0], spot[1], spec.name, true));
    print("LINK\t" + spec.name + "\t" + spot[0] + "\t" + spot[1] + "\t" + blk.name);
}
b.updateCode(s);
/* compile errors (undefined label, malformed statement) leave no instructions;
   fail here in ~3 s instead of after a full scene run */
if(!b.executor.initialized()){
    throw new Error("PREFLIGHT: program did not compile (run mlog.py check; "
                    + "a jump target out of range/undefined label compiles to nothing)");
}
/* a link that is out of range/silently unbound becomes a null constant; catch it
   now instead of debugging "why is cell1 null" after a long run */
for(var k = 0; k < b.links.size; k++){
    var lk = b.links.get(k);
    if(!lk.valid){
        throw new Error("PREFLIGHT: link '" + lk.name + "' did not bind (missing target "
                        + "or farther than 10 tiles)");
    }
}
/* updateTile() copies the team in; paused world never runs it */
b.executor.team = Packages.mindustry.game.Team.sharded;
/* a block placed via setBlock keeps efficiency 0 until updateEfficiency runs */
b.efficiency = 1;
/* probe hook: runs after the program compiled and the units exist but before the
   first instruction executes, so a probe can put the world in a state (kill the
   bound unit, hand it to another processor) and then let the program see it.
   `b` is this processor's building; empty when the caller passes nothing. */
%(setup)s
%(loop)s
print("@@BEGIN");
var vs = b.executor.vars;
for(var i = 0; i < vs.length; i++){
    var v = vs[i], val;
    if(v.isobj){ val = String(v.objval); } else { val = "" + v.numval; }
    print("VAR\t" + v.name + "\t" + val);
}
print("COUNTER\t" + b.executor.counter.numval);
print("IPT\t" + b.executor.ipt.numval);
print("ACC\t" + b.accumulator);
print("TEXT\t" + String(b.executor.textBuffer.toString()).replace(/\n/g, "\\n"));
print("@@END");
'''

SCENE_JS = r'''
var content = Vars.content;
var team = Packages.mindustry.game.Team.sharded;
var LL = Packages.mindustry.world.blocks.logic.LogicBlock.LogicLink;
var tiles = %(tiles)s;
for(var i = 0; i < tiles.length; i++){
    var t = tiles[i];
    var blk = content.block(t.block);
    if(blk == null){ print("SKIP\t" + t.block); continue; }
    Vars.world.tile(t.x, t.y).setBlock(blk, team);
}
var procs = [];
for(var i = 0; i < tiles.length; i++){
    var t = tiles[i];
    if(!t.logic){ continue; }
    var b = Vars.world.build(t.x, t.y);
    if(b == null){ print("NOPROC\t" + t.x + "," + t.y); continue; }
    b.code = t.logic;
    b.links.clear();
    for(var j = 0; j < t.links.length; j++){
        var l = t.links[j];
        b.links.add(new LL(l.x, l.y, l.name, true));
    }
    b.updateCode(b.code);
    if(!b.executor.initialized()){
        throw new Error("PREFLIGHT: processor at " + t.x + "," + t.y + " did not compile");
    }
    for(var k = 0; k < b.links.size; k++){
        var lk = b.links.get(k);
        if(!lk.valid){
            throw new Error("PREFLIGHT: processor at " + t.x + "," + t.y + " link '"
                            + lk.name + "' did not bind (missing target or farther than 10 tiles)");
        }
    }
    /* updateTile() copies the team in; paused world never runs it */
    b.executor.team = team;
    b.efficiency = 1;
    procs.push(b);
}
print("PROCS\t" + procs.length);
%(loop)s
for(var i = 0; i < procs.length; i++){
    var b = procs[i];
    print("@@BEGIN\t" + b.tileX() + "," + b.tileY());
    var vs = b.executor.vars;
    for(var j = 0; j < vs.length; j++){
        var v = vs[j], val;
        if(v.isobj){ val = String(v.objval); } else { val = "" + v.numval; }
        print("VAR\t" + v.name + "\t" + val);
    }
    print("COUNTER\t" + b.executor.counter.numval);
    print("IPT\t" + b.executor.ipt.numval);
    print("ACC\t" + b.accumulator);
    print("TEXT\t" + String(b.executor.textBuffer.toString()).replace(/\n/g, "\\n"));
    print("@@END");
}
'''

UNIT_JS = r'''
var spec = %(spec)s;            /* [[name, count], ...] */
var team = Packages.mindustry.game.Team.sharded;
var wantx = %(wx)f, wanty = %(wy)f;
Vars.state.rules.logicUnitControl = true;
/* Two distinct failure modes, do not conflate them:
 * (a) BLANK WORLD HAS NO CORES -> Units.getCap() == 0 -> every unit is removed
 *     inside add() before any update() runs. This is what "spawned and died
 *     instantly" looked like historically; raising the cap is what makes the
 *     spawn possible at all.
 * (b) SPAWNING ON A SOLID TILE was believed to kill the unit on its first
 *     update(). NOT reproduced on v160.7: a dagger placed on stone-wall or on a
 *     micro-processor tile stayed alive for 5 updated ticks
 *     (out/takeover-20261024/07-assert-runs.md, assertion L2-07 overturned).
 * freeSpot() is therefore kept only to get a clean landing spot: the processor
 * is placed at the base tile, and a +16px offset can land inside a multi-tile
 * core. It skips the base tile itself and spawns at the tile center. */
function freeSpot(cx, cy){
    var bw = Math.floor(cx / 8), bh = Math.floor(cy / 8);
    for(var r = 1; r < 16; r++){
        for(var dx = -r; dx <= r; dx++){
            for(var dy = -r; dy <= r; dy++){
                if(Math.max(Math.abs(dx), Math.abs(dy)) != r) continue;
                var x = bw + dx, y = bh + dy;
                if(x < 1 || y < 1 || x >= Vars.world.width() - 1 || y >= Vars.world.height() - 1) continue;
                var t = Vars.world.tile(x, y);
                if(t.block().name != "air" || t.floor().solid || t.floor().isLiquid) continue;
                return [x * 8 + 4, y * 8 + 4];
            }
        }
    }
    return [cx, cy];
}
var made = 0;
var SPAWNED = [];
var total = 0;
for(var s = 0; s < spec.length; s++){
    var name = spec[s][0], n = spec[s][1];
    total += n;
    var ut = null;
    for(var i = 0; i < Vars.content.units().size; i++){
        var t = Vars.content.units().get(i);
        if(t.name == name){ ut = t; break; }
    }
    if(ut == null){ print("NOUNIT\t" + name); continue; }
    for(var k = 0; k < n; k++){
        var sp = freeSpot(wantx + (k %% 5) * 16, wanty + ((k / 5) | 0) * 16);
        SPAWNED.push(ut.spawn(team, sp[0], sp[1]));
        made++;
    }
}
if(made < total){
    throw new Error("PREFLIGHT: requested " + total + " unit(s), spawned " + made
                    + " (unknown unit name? see --units)");
}
/* paused world never runs Teams.updateTeamStats, so ubind would see no units */
Vars.state.teams.updateTeamStats();
print("UNITS\t" + made);
'''


WORLD_JS = r'''
var GS = Packages.mindustry.core.GameState; var ST = GS.State;
var GB = Packages.mindustry.gen.Groups.build;
/* Snapshot first: update() may add/remove buildings and shift the group. */
function worldTick(){
    var arr = [];
    for(var k = 0; k < GB.size(); k++){ arr.push(GB.index(k)); }
    for(var k = 0; k < arr.length; k++){
        var w = arr[k];
        if(w.block.name.indexOf("processor") >= 0) continue;
        if(w.power != null) w.power.status = %(power)s;
        w.update();
    }
}
'''

# Weather is a real state machine in Logic.updateWeather(): every entry has a
# cooldown; when it fires, an event is created with a random duration and the
# cooldown becomes duration + random frequency. WeatherState.update() counts
# life down (Time.delta per tick), fades opacity, applies its status effect to
# units, and removes itself at life < 0 -- that is exactly what `weathersense`
# / `weatherset` observe and drive.
WEATHER_JS = r'''
var GW = Packages.mindustry.gen.Groups.weather;
var WR = Vars.state.rules.weather;
var WSEED = 123456789;
function wrnd(){ WSEED = (WSEED * 16807) % 2147483647; return WSEED / 2147483647.0; }
function weatherTick(){
    for(var i = 0; i < WR.size; i++){
        var e = WR.get(i);
        if(e.weather == null) continue;
        e.cooldown -= Time.delta;
        if((e.cooldown < 0 || e.always) && !e.weather.isActive()){
            var dur = e.always ? 1e30 : e.minDuration + wrnd() * (e.maxDuration - e.minDuration);
            e.cooldown = dur + e.minFrequency + wrnd() * (e.maxFrequency - e.minFrequency);
            e.weather.create(e.intensity, dur);
        }
    }
    GW.update();
}
'''


def weather_js(name: str, intensity: float = 1.0) -> str:
    return ("var W = Vars.content.weather('%s');"
            "if(W == null){ throw new Error('unknown weather: %s'); }"
            "W.create(%f, 1e30);"
            "print('WEATHER\\t' + W.name);" % (name, name, intensity))



UNITS_JS = r'''
var UG = Packages.mindustry.gen.Groups.unit;
var UNIT_ALL = %(all)s;
/* Snapshot first: update() may kill/despawn units and shift the group. */
function unitTick(){
    var arr = [];
    for(var k = 0; k < UG.size(); k++){ arr.push(UG.index(k)); }
    for(var k = 0; k < arr.length; k++){
        var u = arr[k];
        if(!u.isValid()) continue;
        /* logic-controlled units always; --unit-ai all adds the world's own AI */
        if(UNIT_ALL || u.controller() instanceof Packages.mindustry.ai.types.LogicAI) u.update();
    }
}
'''

# Cheap first-tick checks: a unit spawned on a solid tile is killed by its first
# update() (900 HP gone in one frame) and the run then looks like "the unit did
# not move". Fail at tick 1 with the real cause instead of after 1200 ticks.
PREFLIGHT_JS = r'''
function preflightTick(){
    if(typeof SPAWNED === "undefined") return;
    for(var i = 0; i < SPAWNED.length; i++){
        var u = SPAWNED[i];
        if(u == null || !u.isValid()){
            throw new Error("PREFLIGHT: a spawned unit died on tick 1 (solid spawn tile? "
                            + "use --unit-pos; pass --no-preflight to ignore)");
        }
    }
}
'''


COMBAT_JS = r'''
var GBU = Packages.mindustry.gen.Groups.bullet;
var GUU = Packages.mindustry.gen.Groups.unit;
/* Logic.updateEntities() runs bullet/unit physics first, then the updates,
   then bullet.update()/collide(). Mirror that order, otherwise real bullets
   never move and never deal damage. */
function combatPre(){ GBU.updatePhysics(); GUU.updatePhysics(); }
function combatPost(){ GBU.update(); GBU.collide(); }
'''

# Per-tick trace of the unit a processor is steering. Coordinates are tiles.
# Columns: unit,tick,ux,uy,uhp,range,tx,ty,dist,cx,cy,chp,bullets,shots
TRACE_JS = r'''
var TRACE = [];
var GBT = Packages.mindustry.gen.Groups.build;
var GUT = Packages.mindustry.gen.Groups.unit;
var GBL = Packages.mindustry.gen.Groups.bullet;
function traceCore(u){
    var best = null, bd = 1e30;
    for(var i = 0; i < GBT.size(); i++){
        var w = GBT.index(i);
        if(w == null || !w.isValid()) continue;
        if(!(w.block instanceof Packages.mindustry.world.blocks.storage.CoreBlock)) continue;
        if(w.team == u.team) continue;
        var dx = w.x - u.x, dy = w.y - u.y;
        var d = Math.sqrt(dx*dx + dy*dy);
        if(d < bd){ bd = d; best = w; }
    }
    return best;
}
function traceTick(){
    for(var i = 0; i < GUT.size(); i++){
        var u = GUT.index(i);
        if(u == null || !u.isValid()) continue;
        var ctl = u.controller();
        if(!(ctl instanceof Packages.mindustry.ai.types.LogicAI)) continue;
        var tx = "", ty = "", dist = "";
        if(("" + ctl.aimControl) == "target"){
            tx = ctl.posTarget.x / 8.0; ty = ctl.posTarget.y / 8.0;
            var ax = ctl.posTarget.x - u.x, ay = ctl.posTarget.y - u.y;
            dist = Math.sqrt(ax*ax + ay*ay) / 8.0;
        }
        var c = traceCore(u);
        var cx = "", cy = "", chp = "";
        if(c != null){ cx = c.x / 8.0; cy = c.y / 8.0; chp = c.health; }
        var shots = 0;
        try { for(var m = 0; m < u.mounts.length; m++){ shots += u.mounts[m].totalShots; } } catch(e){}
        TRACE.push(["TRACE", u.type.name, Vars.state.tick, (u.x/8.0), (u.y/8.0), u.health,
                    (u.type.maxRange/8.0), tx, ty, dist, cx, cy, chp, GBL.size(), shots].join("\t"));
    }
}
'''


def parse_trace(lines: list[str]) -> list[list[str]]:
    rows = []
    for ln in lines:
        if ln.startswith("TRACE\t"):
            parts = ln.split("\t")
            if len(parts) >= 15:
                rows.append(parts[1:])
    return rows


def _world_js(power: float, unit_ai: str = "logic") -> str:
    return WORLD_JS % {"power": power} + UNITS_JS % {"all": "true" if unit_ai == "all" else "false"}



ITRACE_JS = """
var ITRACE = [];
var ITR_CODE = __CODE__;
var ITR_VARS = __VARS__;
function itr_val(ex, name){
  try{
    var v = ex.optionalVar(name);
    if(v == null) return "?";
    return v.isobj ? "<obj>" : String(v.numval);
  }catch(e){ return "?"; }
}
function itr_exec(obj, t){
  var ex = obj.executor;
  if(!obj.enabled || !ex.initialized()) return;
  obj.accumulator += obj.block.instructionsPerTick;
  var guard = 0;
  while(obj.accumulator >= 1){
    var pc = ex.counter.numval|0;
    var src = (pc >= 0 && pc < ITR_CODE.length) ? ITR_CODE[pc] : "";
    var extra = "";
    for(var q = 0; q < ITR_VARS.length; q++){ extra += ITR_VARS[q] + "=" + itr_val(ex, ITR_VARS[q]) + ";"; }
    ITRACE.push("ITRACE\t" + t + "\t" + pc + "\t" + extra + "\t" + src);
    ex.runOnce();
    obj.accumulator -= 1;
    if(++guard > 2000) break;
    if(ex.yield){ ex.yield = false; obj.accumulator = 0; break; }
  }
}
""".strip()

def _loop_js(obj: str, steps: int, ticks: bool, update: str = "none", power: float = 1.0,
             combat: bool = False, trace: bool = False, unit_ai: str = "logic",
             preflight: bool = True, itrace: dict | None = None) -> str:
    """Instruction mode steps the executor directly; tick mode drives updateTile.

    In tick mode Time.delta must be 1.0: edelta() = delta * timeScale feeds the
    accumulator (ipt per tick) and WaitI adds Time.delta/60 per execution, so
    1.0 is the value that makes 60 ticks equal one second.

    itrace={"code": [...], "vars": [...]} switches tick mode from updateTile()
    to a hand-driven loop that records (tick, pc, vars, source line) per
    executed instruction. That is the only way to see which line ran: reading
    @counter after updateTile only tells you where the tick ended.
    """
    if ticks:
        world = _world_js(power, unit_ai) + "worldTick(); unitTick();" if update == "all" else ""
        setup = PREFLIGHT_JS + WEATHER_JS
        pre = post = ""
        if combat:
            setup += COMBAT_JS
            pre = "combatPre(); "
            post = " combatPost();"
        if trace:
            setup += TRACE_JS
        if itrace is not None:
            setup += (ITRACE_JS.replace("__CODE__", json.dumps(itrace.get("code", []), ensure_ascii=False))
                      .replace("__VARS__", json.dumps(itrace.get("vars", []), ensure_ascii=False)))
        trace_tick = " traceTick();" if trace else ""
        if itrace is not None:
            body = pre + world + " itr_exec(%s, i);" % obj + post + trace_tick
        else:
            body = pre + world + " %s.updateTile();" % obj + post + trace_tick
        check = " if(i == 0) preflightTick();" if preflight else ""
        dump = ""
        if trace:
            dump += " for(var q = 0; q < TRACE.length; q++){ print(TRACE[q]); }"
        if itrace is not None:
            dump += " for(var q = 0; q < ITRACE.length; q++){ print(ITRACE[q]); }"
        return ("var GS = Packages.mindustry.core.GameState; var ST = GS.State;"
                " Vars.state.set(ST.playing); Time.delta = 1.0; %s"
                " try{ for(var i = 0; i < %d; i++){"
                " Vars.state.tick++; Time.update(); Vars.logicVars.update(); weatherTick(); %s%s }"
                " } finally { Vars.state.set(ST.paused); }%s" % (setup, steps, body, check, dump))
    return "for(var i = 0; i < %d; i++){ %s.executor.runOnce(); }" % (steps, obj)


def _scene_loop_js(steps: int, ticks: bool, update: str = "none", power: float = 1.0,
                   combat: bool = False, trace: bool = False, unit_ai: str = "logic",
                   preflight: bool = True) -> str:
    """Same, but tick every processor together so cross-reads see one world.

    A scene has N processors, so the tick loop must nest: outer ticks, inner
    processors. Iterating each processor for `steps` ticks in isolation would
    let one processor run far ahead of the other.
    """
    if ticks:
        world = _world_js(power, unit_ai) + "worldTick(); unitTick();" if update == "all" else ""
        setup = PREFLIGHT_JS + WEATHER_JS
        pre = post = ""
        if combat:
            setup += COMBAT_JS
            pre = "combatPre(); "
            post = " combatPost();"
        if trace:
            setup += TRACE_JS
        dump = (" for(var q = 0; q < TRACE.length; q++){ print(TRACE[q]); }" if trace else "")
        check = " if(t == 0) preflightTick();" if preflight else ""
        return ("var GS = Packages.mindustry.core.GameState; var ST = GS.State;"
                " Vars.state.set(ST.playing); Time.delta = 1.0; %s"
                " try{ for(var t = 0; t < %d; t++){"
                " Vars.state.tick++; Time.update(); Vars.logicVars.update(); weatherTick(); %s"
                " for(var j = 0; j < procs.length; j++){ procs[j].updateTile(); }"
                " %s%s%s }"
                " } finally { Vars.state.set(ST.paused); }%s"
                % (setup, steps, pre + world, post, " traceTick();" if trace else "", check, dump))
    return ("for(var i = 0; i < procs.length; i++){"
            " for(var k = 0; k < %d; k++){ procs[i].executor.runOnce(); } }" % steps)


def parse_results(lines: list[str]) -> list[dict]:
    """Turn @@BEGIN blocks into records."""
    recs: list[dict] = []
    cur = None
    for ln in lines:
        if ln.startswith("@@BEGIN"):
            cur = {"label": ln.split("\t", 1)[1] if "\t" in ln else "", "vars": {}, "counter": None,
                   "ipt": None, "accumulator": None, "text": "", "skipped": []}
            recs.append(cur)
        elif ln.startswith("@@END"):
            cur = None
        elif ln.startswith("SKIP\t") and recs:
            recs[-1]["skipped"].append(ln.split("\t", 1)[1])
        elif cur is not None:
            if ln.startswith("VAR\t"):
                _, k, v = ln.split("\t", 2)
                cur["vars"][k] = v
            elif ln.startswith("COUNTER\t"):
                cur["counter"] = float(ln.split("\t", 1)[1])
            elif ln.startswith("IPT\t"):
                cur["ipt"] = float(ln.split("\t", 1)[1])
            elif ln.startswith("ACC\t"):
                cur["accumulator"] = float(ln.split("\t", 1)[1])
            elif ln.startswith("TEXT\t"):
                cur["text"] = ln.split("\t", 1)[1].replace("\\n", "\n")
    return recs


LINK_ALIASES = {
    "cell": "memory-cell",
    "bank": "memory-bank",
    "message": "message",
    "switch": "switch",
    "sorter": "sorter",
    "unloader": "unloader",
    "dome": "overdrive-dome",
    "projector": "force-projector",
    "mender": "mender",
    "router": "router",
    "junction": "junction",
    "spectre": "spectre",
    "furnace": "silicon-arc-furnace",
    "pad": "landing-pad",
    "container": "container",
    "turret": "duo",
}


def infer_links(code: str) -> list[dict]:
    """Link names a `.mlog` uses but never assigns (`cell1`, `message2`, ...).

    A plain `.mlog` file has no link table: `read cell1 0` normally compiles to a
    null constant. With `--links auto` the simulator places a matching block next
    to the processor and wires it under that name. Names follow the game defaults
    (`memory-cell` -> `cell1`, `memory-bank` -> `bank1`, `overdrive-dome` -> `dome1`).
    """
    sys.path.insert(0, TOOLS)
    import mlogir  # noqa: E402

    prog = mlogir.parse(code)
    defined, used = set(), set()
    for st in prog.stmts:
        defined.update(st.defs)
        used.update(st.uses)
    out = []
    for name in sorted(used - defined):
        if name.startswith("@") or not re.fullmatch(r"[a-z][a-z0-9-]*\d+", name):
            continue
        base = re.sub(r"\d+$", "", name)
        if base not in LINK_ALIASES:
            continue
        out.append({"name": name, "block": LINK_ALIASES[base]})
    return out


def run_mlog(srv: Server, name: str, code: str, steps: int, ticks: bool = False,
             update: str = "none", power: float = 1.0, processor: str = "auto",
             combat: bool = False, trace: bool = False, unit_ai: str = "logic",
             preflight: bool = True, links: str = "auto",
             itrace: str | None = None, itrace_vars: tuple = (),
             setup_js: str | None = None) -> dict:
    # privileged statements exist only in world processors; selecting the block
    # that makes them compile is better than silently dropping whole lines.
    world_proc = processor == "world" or (processor == "auto" and PRIVILEGED_RE.search(code) is not None)
    link_specs = infer_links(code) if links == "auto" else []
    # a fresh processor keeps textBuffer/vars from leaking between programs
    srv.js(place_js(*srv.base, "world-processor" if world_proc else "micro-processor"))
    fd, path = tempfile.mkstemp(suffix=".mlog", dir=srv.workdir, text=True)
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(code)
        itr = None
        if itrace:
            itr = {"code": code.splitlines(), "vars": list(itrace_vars)}
        js = STEP_JS % {"path": path.replace("\\", "/"), "x": srv.base[0], "y": srv.base[1],
                        "links": json.dumps(link_specs, ensure_ascii=False),
                        "setup": setup_js or "",
                        "loop": _loop_js("b", steps, ticks, update, power, combat, trace, unit_ai,
                                         preflight, itr)}
        lines = srv.js(js)
        if preflight:
            warns = [ln.split("\t") for ln in lines if ln.startswith("LINKWARN\t")]
            if warns:
                detail = "; ".join(f"{w[1]}: {w[2]}" for w in warns if len(w) > 2)
                raise SimError(f"link preflight: {detail} (use --links off to allow null links)")
        recs = parse_results(lines)
        rec = recs[0] if recs else {"vars": {}, "counter": None, "text": ""}
        rec["trace"] = parse_trace(lines) if trace else []
        if itrace:
            rows = [ln for ln in lines if ln.startswith("ITRACE\t")]
            with open(itrace, "w", encoding="utf-8", newline="\n") as f:
                f.write("tick\tpc\tvars\tsource\n")
                for ln in rows:
                    f.write(ln[len("ITRACE\t"):] + "\n")
            rec["itrace"] = itrace
        rec["name"] = name
        rec["steps"] = steps
        rec["mode"] = "tick" if ticks else "instruction"
        rec["update"] = update
        rec["processor"] = "world" if world_proc else "micro"
        rec["links"] = link_specs
        return rec
    finally:
        os.unlink(path)


def run_msch(srv: Server, name: str, path: str, steps: int, ticks: bool = False,
             update: str = "none", power: float = 1.0,
             combat: bool = False, trace: bool = False, unit_ai: str = "logic",
             preflight: bool = True) -> list[dict]:
    sys.path.insert(0, TOOLS)
    import msch  # noqa: E402

    sch = msch.parse_file(path)
    minx = min(t.x for t in sch.tiles)
    miny = min(t.y for t in sch.tiles)
    w = max(t.x for t in sch.tiles) - minx + 1
    h = max(t.y for t in sch.tiles) - miny + 1
    if srv.world_size and (srv.base[0] + w > srv.world_size or srv.base[1] + h > srv.world_size):
        raise SimError("schematic is %dx%d, does not fit the %dx%d world (use --world %d or --host)"
                       % (w, h, srv.world_size, srv.world_size, max(w, h)))
    tiles = []
    for t in sch.tiles:
        x, y = srv.base[0] + (t.x - minx), srv.base[1] + (t.y - miny)
        entry = {"block": t.block, "x": x, "y": y, "logic": None, "links": []}
        if t.logic is not None:
            entry["logic"] = t.logic.code
            entry["links"] = [
                {"name": l.name, "x": srv.base[0] + (l.x - minx), "y": srv.base[1] + (l.y - miny)}
                for l in t.logic.links
            ]
        tiles.append(entry)
    js = SCENE_JS % {"tiles": json.dumps(tiles),
                     "loop": _scene_loop_js(steps, ticks, update, power, combat, trace, unit_ai,
                                            preflight)}
    lines = srv.js(js, timeout=180)
    recs = parse_results(lines)
    rows = parse_trace(lines) if trace else []
    for r in recs:
        r["name"] = name
        r["steps"] = steps
        r["mode"] = "tick" if ticks else "instruction"
        r["update"] = update
        r["trace"] = rows
    return recs


TRACE_COLUMNS = ["unit", "tick", "ux", "uy", "uhp", "range", "tx", "ty", "dist",
                 "cx", "cy", "chp", "bullets", "shots"]


def check_asserts(rows: list[list[str]], exprs: list[str]) -> int:
    """Evaluate EXPR per trace row; return the number of failed evaluations."""
    if not rows:
        print("ASSERT: no trace rows (are you using --ticks?)", file=sys.stderr)
        return 1
    fails = []
    for expr in exprs:
        for row in rows:
            env = {}
            for key, val in zip(TRACE_COLUMNS, row):
                if key == "unit":
                    env[key] = val
                else:
                    try:
                        env[key] = float(val)
                    except ValueError:
                        env[key] = None
            try:
                ok = bool(eval(expr, {"__builtins__": {}}, env))  # noqa: S307 - local, no builtins
            except Exception as e:  # noqa: BLE001 - report, do not crash
                ok = False
                env["_error"] = str(e)
            if not ok:
                fails.append((expr, env))
    if not fails:
        print(f"ASSERT PASS  {len(rows)} row(s) x {len(exprs)} expr(s)")
        return 0
    for expr, env in fails[:5]:
        detail = " ".join(f"{k}={env[k]}" for k in
                          ("tick", "unit", "dist", "range", "chp", "ux", "uy") if k in env)
        err = f"  ({env['_error']})" if "_error" in env else ""
        print(f"ASSERT FAIL  {expr}\n              {detail}{err}")
    print(f"ASSERT FAILED  {len(fails)}/{len(rows) * len(exprs)} evaluation(s)")
    return len(fails)


def _write_trace(path: str, rows: list[list[str]]):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(",".join(TRACE_COLUMNS) + "\n")
        for row in rows:
            fh.write(",".join(row) + "\n")


def configure_server(srv: Server, args):
    """Attach per-run world setup so prepare() can replay it after each reset."""
    if args.base:
        srv.base = tuple(int(v) for v in args.base.split(","))
        srv._base0 = srv.base
    if args.init_js:
        if os.path.exists(args.init_js):
            with open(args.init_js, encoding="utf-8-sig") as fh:
                srv._init_src = fh.read()
        else:
            srv._init_src = args.init_js
    if args.core_items:
        srv._core_spec = args.core_items
    if args.units:
        spec = []
        for part in args.units.split(","):
            part = part.strip()
            if not part:
                continue
            name, _, cnt = part.partition(":")
            spec.append([name.strip(), int(cnt) if cnt else 1])
        srv._unit_spec = spec
        if args.unit_pos:
            ux, uy = (float(v) for v in args.unit_pos.split(","))
            srv._unit_pos = (ux * 8 + 4, uy * 8 + 4)
    if args.weather:
        for wspec in args.weather:
            wname, _, wint = wspec.partition(":")
            srv._weather_specs.append((wname.strip(), float(wint) if wint else 1.0))


DAEMON_FILE = os.path.join(ROOT, "out", "_mlogsimd.json")


def serve_daemon(args, steps: int, ticks: bool, update: str, power: float, combat: bool,
                 preflight: bool):
    """Boot once, then answer JSON-line jobs over localhost (prewarm for a session)."""
    import socket
    world_size = None if args.host or args.world == 0 else args.world
    srv = Server(args.java, args.jar, world_size, args.save)
    configure_server(srv, args)
    srv.prepare()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port))
    sock.listen(4)
    port = sock.getsockname()[1]
    with open(DAEMON_FILE, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "port": port, "world": world_size, "save": args.save}, fh)
    print("DAEMON\t%d" % port, flush=True)
    try:
        while True:
            conn, _ = sock.accept()
            try:
                with conn:
                    line = conn.makefile("r", encoding="utf-8").readline()
                    req = json.loads(line)
                    srv.reset_world()
                    srv.prepare()
                    f = req["file"]
                    n = int(req.get("ticks") or 0)
                    t = n > 0
                    st = n if t else int(req.get("steps") or steps)
                    if f.lower().endswith(".msch"):
                        recs = run_msch(srv, os.path.basename(f), f, st, t, update, power,
                                        combat, False, args.unit_ai, preflight)
                    else:
                        with open(f, encoding="utf-8-sig") as fh:
                            code = fh.read()
                        recs = [run_mlog(srv, os.path.basename(f), code, st, t, update, power,
                                         args.processor, combat, False, args.unit_ai,
                                         preflight, args.links)]
                    conn.sendall((json.dumps({"ok": True, "results": recs},
                                             ensure_ascii=False) + "\n").encode("utf-8"))
            except Exception as e:  # noqa: BLE001 - report to the client, keep serving
                try:
                    conn.sendall((json.dumps({"ok": False, "error": str(e)},
                                             ensure_ascii=False) + "\n").encode("utf-8"))
                except OSError:
                    pass
    finally:
        try:
            os.unlink(DAEMON_FILE)
        except OSError:
            pass
        srv.close()


def remote_client(args) -> int:
    """Send files to a running --daemon and print its results."""
    import socket
    if not os.path.exists(DAEMON_FILE):
        sys.exit("no daemon: start `python tools/mlogsim.py --daemon` first")
    with open(DAEMON_FILE, encoding="utf-8") as fh:
        cfg = json.load(fh)
    out = []
    for f in args.files:
        req = {"file": os.path.abspath(f)}
        if args.ticks:
            req["ticks"] = args.ticks
        elif args.steps:
            req["steps"] = args.steps
        try:
            with socket.create_connection(("127.0.0.1", cfg["port"]), timeout=300) as s:
                s.sendall((json.dumps(req) + "\n").encode("utf-8"))
                line = s.makefile("r", encoding="utf-8").readline()
        except OSError:
            sys.exit(f"daemon not running (stale {DAEMON_FILE}?); start --daemon again")
        resp = json.loads(line)
        if not resp.get("ok"):
            sys.exit(f"sim failed: {resp.get('error')}")
        out.extend(resp["results"])
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else
          "\n".join(f"== {r['name']} ==" for r in out))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="run mlog on the real Mindustry LExecutor")
    ap.add_argument("files", nargs="*", help=".mlog files (or .msch schematics)")
    ap.add_argument("--daemon", action="store_true",
                    help="boot once and serve JSON jobs on 127.0.0.1 (warm start for a session)")
    ap.add_argument("--remote", action="store_true",
                    help="send the files to the running --daemon instead of booting a server")
    ap.add_argument("--port", type=int, default=0,
                    help="--daemon port (0 = pick a free one and write out/_mlogsimd.json)")
    ap.add_argument("--steps", type=int, default=None, help="instructions to execute (default 200)")
    ap.add_argument("--ticks", type=int, default=None,
                    help="simulated ticks: drive updateTile() with Time.delta=1 (accumulator/ipt/wait)")
    ap.add_argument("--world", type=int, default=64, metavar="N",
                    help="blank NxN world loaded with loadGenerator (default 64, 0 = host a random map)")
    ap.add_argument("--host", action="store_true",
                    help="host a random map instead of the blank world (needed for terrain/@mapw)")
    ap.add_argument("--jar", default=_default_jar())
    ap.add_argument("--java", default=_default_java())
    ap.add_argument("--init-js", metavar="FILE|CODE", default=None,
                    help="JS run once after the world exists: set rules, seed blocks")
    ap.add_argument("--units", metavar="NAME[:N],...", default=None,
                    help="spawn units near the processor as team sharded, e.g. flare:3,poly:1")
    ap.add_argument("--unit-pos", metavar="X,Y", default=None,
                    help="spawn units at this tile coordinate instead of 2 tiles off the processor, e.g. 6,4")
    ap.add_argument("--update", choices=["none", "all"], default=None,
                    help="world update per tick: all = every building (default in --ticks mode), none = logic only")
    ap.add_argument("--power", choices=["full", "none"], default="full",
                    help="for --update all: pin every consumer's power.status (default full = 100 percent power)")
    ap.add_argument("--combat", choices=["on", "off"], default=None,
                    help="run bullet physics + damage each tick (default on with --ticks). "
                         "Without it a unit fires but bullets never move or hit")
    ap.add_argument("--itrace", metavar="FILE", default=None,
                    help="write per-instruction execution trace (tick, pc, vars, source)")
    ap.add_argument("--itrace-var", dest="itrace_vars", action="append", default=[],
                    metavar="NAME", help="variable to include in each --itrace row (repeatable)")
    ap.add_argument("--trace", metavar="FILE", default=None,
                    help="write per-tick CSV of logic-controlled units (tiles): "
                         "unit,tick,ux,uy,uhp,range,tx,ty,dist,cx,cy,chp,bullets,shots")
    ap.add_argument("--assert", dest="asserts", action="append", default=None, metavar="EXPR",
                    help="evaluate EXPR on every trace row (e.g. 'dist >= range - 1.5'); "
                         "exit 3 on any failure")
    ap.add_argument("--assert-last", dest="asserts_last", action="append", default=None, metavar="EXPR",
                    help="evaluate EXPR on the final trace row only (e.g. 'chp < 1100' = "
                         "the target took damage); exit 3 on failure")
    ap.add_argument("--processor", choices=["auto", "micro", "world"], default="auto",
                    help="block for a .mlog program; auto = world-processor when it uses privileged "
                         "statements (weathersense/weatherset/...), which a micro processor silently drops")
    ap.add_argument("--weather", metavar="NAME[:INTENSITY]", action="append", default=None,
                    help="force a weather active from tick 0, e.g. rain or sandstorm:0.5; repeatable. "
                         "Without it the world starts clear (a --host map can still start its own weather)")
    ap.add_argument("--unit-ai", choices=["logic", "all"], default="logic",
                    help="which units get Unit.update() per tick: logic = only units a "
                         "processor controls; all = also the world's own AI (enemy defenders shoot back)")
    ap.add_argument("--save", metavar="FILE", default=None,
                    help="load a .msav scene instead of a blank world: its map, teams, "
                         "cores, buildings and units all come with it (slot 'scene')")
    ap.add_argument("--core-items", metavar="ITEM:N,...", default=None,
                    help="fill our team's core(s) before the run, e.g. silicon:400,phase-fabric:200; "
                         "on a blank world a core-shard is placed next to the processor")
    ap.add_argument("--base", metavar="X,Y", default=None,
                    help="tile for the processor: default 0,0 on a blank world, auto "
                         "next to your core with --save")
    ap.add_argument("--no-preflight", action="store_true",
                    help="skip the tick-1 check that spawned units are still alive")
    ap.add_argument("--links", choices=["auto", "off"], default="auto",
                    help="auto (default): for a .mlog, place a block for every name it "
                         "reads but never assigns (cell1 -> memory-cell, message1 -> message) "
                         "and wire it; off: leave them null as a plain .mlog would")
    ap.add_argument("--sweep-orphans", action="store_true",
                    help="kill simulator JVMs left behind by killed runs, then exit")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.sweep_orphans:
        killed = sweep_orphans()
        print("orphan sweep: %d stale JVM(s)%s" % (
            len(killed), (" " + " ".join(str(p) for p in killed)) if killed else ""))
        return 0

    ticks = args.ticks is not None
    steps = args.ticks if ticks else (args.steps if args.steps is not None else 200)
    update = args.update if args.update is not None else ("all" if ticks else "none")
    power = 1.0 if args.power == "full" else 0.0
    combat = (args.combat == "on") if args.combat else ticks
    want_trace = bool(args.trace or args.asserts or args.asserts_last)
    preflight = not args.no_preflight

    if not os.path.exists(args.jar):
        sys.exit(f"missing server jar: {args.jar}")

    world_size = None if args.host or args.world == 0 else args.world
    if args.remote:
        return remote_client(args)
    if args.daemon:
        serve_daemon(args, steps, ticks, update, power, combat, preflight)
        return 0
    if not args.files:
        sys.exit("no input files (or use --daemon)")
    srv = Server(args.java, args.jar, world_size, args.save)
    configure_server(srv, args)
    results = []
    try:
        for idx, f in enumerate(args.files):
            if idx:
                # one JVM for the whole batch: reload the world (~0.12 s) instead
                # of paying the ~3 s server boot for every file
                srv.reset_world()
            srv.prepare()
            if f.lower().endswith(".msch"):
                results.extend(run_msch(srv, os.path.basename(f), f, steps, ticks, update, power,
                                        combat, want_trace, args.unit_ai, preflight))
            else:
                # utf-8-sig: PowerShell's `Set-Content -Encoding utf8` prepends a BOM,
                # and the parser turns a BOM-prefixed first line into a noop
                with open(f, encoding="utf-8-sig") as fh:
                    code = fh.read()
                itr_path = args.itrace
                if itr_path and len(args.files) > 1:
                    itr_path = itr_path + "." + os.path.basename(f)
                results.append(run_mlog(srv, os.path.basename(f), code, steps, ticks,
                                        update, power, args.processor, combat, want_trace,
                                        args.unit_ai, preflight, args.links,
                                        itr_path, tuple(args.itrace_vars)))
    except FileNotFoundError as e:
        sys.exit(f"missing input: {e.filename}")
    except SimError as e:
        sys.exit(f"sim failed: {e}")
    finally:
        try:
            with open(os.path.join(ROOT, "out", "_sim_server.log"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(srv.all))
        except OSError:
            pass
        srv.close()

    trace_rows: list[list[str]] = []
    for r in results:
        trace_rows.extend(r.pop("trace", []) or [])
    if args.trace:
        _write_trace(args.trace, trace_rows)
    failed = check_asserts(trace_rows, args.asserts) if args.asserts else 0
    if args.asserts_last:
        failed += check_asserts(trace_rows[-1:], args.asserts_last)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            unit = "ticks" if r.get("mode") == "tick" else "steps"
            print(f"== {r['name']} @{r.get('label', '')} ({r['steps']} {unit}) ==")
            for k, v in r["vars"].items():
                print(f"  {k} = {v}")
            if r["text"]:
                print(f"  print: {r['text']}")
            print(f"  @counter = {r['counter']}  ipt = {r.get('ipt')}  acc = {r.get('accumulator')}")
            if r.get("mode") == "tick":
                print(f"  world = {r.get('update')}  power = {'full' if power else 'none'}"
                      f"  combat = {'on' if combat else 'off'}"
                      + (f"  processor = {r['processor']}" if r.get("processor") else ""))
            if r.get("skipped"):
                print(f"  skipped: {', '.join(r['skipped'])}")
    if args.trace:
        print(f"TRACE  {len(trace_rows)} row(s) -> {args.trace}")
    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
