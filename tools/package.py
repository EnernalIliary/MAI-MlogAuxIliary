#!/usr/bin/env python3
"""Build the installable MAI package (mod + sidecar + double-click installers).

Why this exists
---------------
"打包安装即用" is the last item of the 2026-10-31 list (m02047): until now
installing meant two hand copies (`MlogAuxiliary/` into `saves/mods`, and a
`python tools/logicd.py` window started from the *repo*), and the previous
"package" was a zip of the mod folder only (`out/_dist/MlogAuxiliary.zip`) --
which cannot work, because the mod is a front end: without the sidecar it can
only say "cannot reach the sidecar".

What makes this package different from a folder copy
----------------------------------------------------
* The sidecar is **not** the whole `tools/` tree.  `logicd.py` imports six local
  modules and reads five JSON data files by `Path(__file__).with_name(...)`; the
  closure is computed from the sources here, so a new import cannot silently
  ship a package that dies on the player's machine with `ModuleNotFoundError`.
* The `rules/*.json` set ships next to the sidecar so findings keep their rule
  ids (`tools/rule_ids.py` walks up to `parents[:4]` looking for `rules/`).
* `VERSION.txt` carries the SHA256 of every file; `install.ps1` re-hashes the
  copied mod files, so "installed" means byte-identical, not "the copy ran".
* `--verify` does not read the zip, it **runs** it: extract to a temp dir, start
  the packaged sidecar on a free port, ask `/health` and `/capabilities`, then
  drive `install.ps1`/`uninstall.ps1` against a temp `saves\\mods` folder.

Usage
-----
    python tools/package.py                  # build out/_dist/MAI-<ver>/ + .zip
    python tools/package.py --verify         # build to a temp dir and prove it
    python tools/package.py --out D:\\tmp     # elsewhere
    python tools/package.py --no-zip         # tree only

The zip is an artifact, not a source file: `out/_dist/` is not in git (see
.gitignore).  The installers themselves live in `packaging/` and are reviewed
like any other source.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / 'tools'
MOD = ROOT / 'MlogAuxiliary'
PACKAGING = ROOT / 'packaging'
DIST = ROOT / 'out' / '_dist'
EXE_CACHE = ROOT / 'out' / '_exe'

#: v0.13.1: the sidecar also ships as a PyInstaller onedir build, so the player
#: needs no Python.  Everything sidecar-shaped lives under the *mod* folder --
#: the mod finds it through `Vars.mods.getMod("mlog-auxiliary").root`, and
#: uninstalling the mod takes the backend with it.
SIDE = 'mod/MlogAuxiliary/sidecar/'
#: onedir, not onefile: onefile unpacks itself into %TEMP% and *executes* from
#: there, which Windows Defender's ASR rules block on locked-down machines
#: ("Could not create temporary directory!" from the bootloader).  onedir never
#: touches %TEMP%.
EXE_SUB = 'bin'
#: v0.14.5a: the folder name inside the zip the game imports in-app.  Mindustry
#: only looks at the zip root (or one level down when the root holds exactly one
#: directory), so the importable zip must have this as its *only* top-level entry
#: -- see `build_mod_zip()`.
MOD_NAME = 'MlogAuxiliary'

# data files the sidecar loads by name; discovered from the sources, not listed
_JSON_RE = re.compile(r"""["']([A-Za-z0-9_]+\.json)["']""")


def local_imports(src: str) -> list[str]:
    """Module names this source imports, at *any* nesting depth.

    Depth is the whole point: `logicd.py` does `from rule_ids import rule_id`
    inside `_rid()` and `mlogcheck.py` does the same inside a helper, so a
    line-anchored regex (`^from ...`) silently drops them -- and the package
    then ships without `sidecar/rule_ids.py`.  `/health` still answers, so the
    package looks fine; `/analyze` dies on the player's machine with
    ModuleNotFoundError.  Parsing the AST sees imports wherever they sit.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                out.append(node.module.split('.')[0])
    return out


def mod_version() -> str:
    text = (MOD / 'mod.hjson').read_text(encoding='utf-8')
    m = re.search(r'^\s*version:\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit('cannot read version: from MlogAuxiliary/mod.hjson')
    return m.group(1)


def sidecar_version() -> str:
    text = (TOOLS / 'logicd.py').read_text(encoding='utf-8')
    m = re.search(r"^SIDECAR_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
    if not m:
        raise SystemExit('cannot read SIDECAR_VERSION from tools/logicd.py')
    return m.group(1)


def sidecar_modules() -> list[str]:
    """Local modules logicd.py needs, in import order (closure over tools/*.py)."""
    local = {p.stem for p in TOOLS.glob('*.py')}
    seen: list[str] = []

    def walk(name: str) -> None:
        if name in seen or name not in local:
            return
        seen.append(name)
        src = (TOOLS / (name + '.py')).read_text(encoding='utf-8')
        for mod in local_imports(src):
            walk(mod)

    walk('logicd')
    return seen


def sidecar_data() -> list[Path]:
    """The JSON files the sidecar closure opens by name."""
    names: set[str] = set()
    for name in sidecar_modules():
        src = (TOOLS / (name + '.py')).read_text(encoding='utf-8')
        names.update(_JSON_RE.findall(src))
    out = []
    for name in sorted(names):
        p = TOOLS / name
        if p.is_file():
            out.append(p)
    return out


def sidecar_rules() -> list[Path]:
    d = ROOT / 'rules'
    return sorted(p for p in d.glob('*.json') if p.is_file())


def layout() -> list[tuple[str, Path]]:
    """(path inside the package, source file) for every shipped file.

    The exe tree is not here: it does not exist until `build_exe()` runs, so
    `build()` appends those rows after copying.  Keep the two lists merged
    wherever "every shipped file" matters (`VERSION.txt`, verify step 6).
    """
    rows: list[tuple[str, Path]] = []
    for rel in ('mod.hjson', 'README.md', 'scripts/main.js'):
        rows.append(('mod/MlogAuxiliary/' + rel, MOD / rel))
    # v0.17.1：按键绑定条目的名字来自 bundles/bundle*.properties
    # （Mods.java:636-660 把 mod.root/bundles 合进根 bundle）。漏了不会报错，
    # 只是设置→控制→按键绑定 里显示成原始键名 "mai-select"。
    for p in sorted((MOD / 'bundles').glob('*.properties')):
        rows.append(('mod/MlogAuxiliary/bundles/' + p.name, p))
    for name in sidecar_modules():
        rows.append((SIDE + name + '.py', TOOLS / (name + '.py')))
    for p in sidecar_data():
        rows.append((SIDE + p.name, p))
    for p in sidecar_rules():
        rows.append((SIDE + 'rules/' + p.name, p))
    for p in sorted(PACKAGING.iterdir()):
        if p.is_file():
            rows.append((p.name, p))
    return rows


def exe_sources() -> list[Path]:
    """Every file the frozen exe is built from (newest mtime decides rebuilds)."""
    out = [TOOLS / (name + '.py') for name in sidecar_modules()]
    out += sidecar_data() + sidecar_rules()
    return out


def pyinstaller_cmd() -> list[str]:
    """The interpreter + PyInstaller entry point, or a RuntimeError."""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise RuntimeError('PyInstaller is not installed (pip install pyinstaller)')
    return [sys.executable, '-m', 'PyInstaller']


def build_exe(dest: Path, quiet: bool = False) -> list[tuple[str, Path]]:
    """Build (or reuse) the onedir exe and copy it into `dest`; returns its rows."""
    exe = EXE_CACHE / 'dist' / 'logicd' / 'logicd.exe'
    srcs = exe_sources()
    stale = (not exe.is_file()) or exe.stat().st_mtime < max(p.stat().st_mtime for p in srcs)
    if stale:
        cmd = pyinstaller_cmd() + [
            '--onedir', '--noconfirm', '--name', 'logicd',
            '--distpath', str(EXE_CACHE / 'dist'),
            '--workpath', str(EXE_CACHE / 'build'),
            '--specpath', str(EXE_CACHE),
        ]
        for p in sidecar_data():
            cmd += ['--add-data', '%s;.' % p]
        cmd += ['--add-data', '%s;rules' % (ROOT / 'rules')]
        cmd.append(str(TOOLS / 'logicd.py'))
        if not quiet:
            print('building sidecar/logicd.exe (PyInstaller onedir) ...')
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding='utf-8', errors='replace')
        if proc.returncode != 0 or not exe.is_file():
            raise RuntimeError('PyInstaller failed: %s'
                               % ((proc.stdout or '') + (proc.stderr or ''))[-1200:])
    built = EXE_CACHE / 'dist' / 'logicd'
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(built, dest)
    rows = []
    for p in sorted(dest.rglob('*')):
        if p.is_file():
            rows.append((SIDE + EXE_SUB + '/' + str(p.relative_to(dest)).replace('\\', '/'), p))
    return rows


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest().upper()


def version_txt(rows: list[tuple[str, Path]]) -> bytes:
    lines = ['MAI ' + mod_version(), 'sidecar ' + sidecar_version(),
             'built ' + time.strftime('%Y-%m-%dT%H:%M:%S%z')]
    for rel, src in rows:
        lines.append('sha256 %s %s' % (sha256(src), rel))
    return ('\n'.join(lines) + '\n').encode('utf-8')


def build_mod_zip(tree: Path, out_dir: Path, name: str) -> dict:
    """Zip *only* the mod folder, shaped so the game can import it in-app.

    Mindustry's importer (`Mods.importMod()` -> `loadMod()` -> `resolveRoot()`)
    descends into the single top-level folder when there is exactly one, then
    `findMeta()` looks for mod.json/mod.hjson right there.  The full package zip
    has several top-level entries (install.ps1, 使用说明.md, mod/, ...), so the
    game never finds mod.hjson inside it and reports "no mod.hjson".  This zip
    has one folder -- MlogAuxiliary/ -- with mod.hjson at its root.
    """
    root = tree / 'mod' / MOD_NAME
    files = sorted(str(p.relative_to(root)).replace('\\', '/')
                   for p in root.rglob('*') if p.is_file())
    if 'mod.hjson' not in files:
        raise RuntimeError('%s/mod.hjson missing -- the import zip would be useless'
                           % (tree / 'mod' / MOD_NAME))
    zp = out_dir / (name + '-mod.zip')
    if zp.exists():
        zp.unlink()
    with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(root / f, MOD_NAME + '/' + f)
    return {'zip': zp, 'files': files, 'bytes': zp.stat().st_size, 'sha256': sha256(zp)}


def build(out_dir: Path | None = None, make_zip: bool = True,
          make_exe: bool = True, quiet: bool = False) -> dict:
    """Write the package tree (and zip) and return a summary dict."""
    ver = mod_version()
    name = 'MAI-' + ver
    out_dir = Path(out_dir) if out_dir else DIST
    tree = out_dir / name
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True)
    rows = layout()
    for rel, src in rows:
        dst = tree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    if make_exe:
        rows = rows + build_exe(tree / SIDE / EXE_SUB, quiet=quiet)
    (tree / 'VERSION.txt').write_bytes(version_txt(rows))
    files = sorted(str(p.relative_to(tree)).replace('\\', '/') for p in tree.rglob('*')
                   if p.is_file())
    total = sum((tree / f).stat().st_size for f in files)
    info = {'name': name, 'version': ver, 'sidecar': sidecar_version(),
            'tree': tree, 'files': files, 'bytes': total, 'zip': None, 'zip_sha256': None}
    if make_zip:
        zp = out_dir / (name + '.zip')
        if zp.exists():
            zp.unlink()
        with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(tree / f, name + '/' + f)
        info['zip'] = zp
        info['zip_sha256'] = sha256(zp)
        info['zip_bytes'] = zp.stat().st_size
        # the game cannot import the package zip (several top-level entries, so
        # resolveRoot never descends to mod.hjson) -- ship a second, shaped zip
        info['import_zip'] = build_mod_zip(tree, out_dir, name)
    if not quiet:
        print('package %s  (mod %s, sidecar %s)' % (name, ver, sidecar_version()))
        for f in files:
            print('  %-46s %8d B' % (f, (tree / f).stat().st_size))
        print('  %d files, %d bytes' % (len(files), total))
        iz = info.get('import_zip')
        if iz:
            print('import zip %s  %d B  files %d  sha256 %s'
                  % (iz['zip'], iz['bytes'], len(iz['files']), iz['sha256']))
        if info['zip']:
            print('zip %s  %d B  sha256 %s'
                  % (info['zip'], info['zip_bytes'], info['zip_sha256']))
    return info


# --------------------------------------------------------------------------- #
# verify: run the package, do not just read it
# --------------------------------------------------------------------------- #

def free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_get(port: int, path: str, timeout: float = 10.0) -> tuple[int, dict]:
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode('utf-8') or '{}')


def post_json(port: int, path: str, body: dict,
              timeout: float = 30.0) -> tuple[int, dict]:
    """POST a JSON object to the sidecar; returns (status, decoded reply)."""
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path), data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode('utf-8') or '{}')


def sidecar_entry(tree: Path) -> tuple[list[str], str]:
    """The command that starts the packaged sidecar, and a label for it.

    The exe is what players run; the `.py` copy stays in the package as the
    escape hatch for a machine where the exe is blocked, so prefer the exe and
    fall back only when it is missing.
    """
    exe = tree / SIDE / EXE_SUB / 'logicd.exe'
    if exe.is_file():
        return [str(exe)], 'exe'
    py = tree / SIDE / 'logicd.py'
    if py.is_file():
        return [sys.executable, str(py)], 'py'
    raise RuntimeError('the package has neither %s nor %s' % (exe, py))


def start_sidecar(tree: Path, port: int, timeout: float = 40.0):
    """Start the packaged sidecar; wait for its ready line.  Returns (proc, banner, kind)."""
    cmd, kind = sidecar_entry(tree)
    proc = subprocess.Popen(cmd + ['--port', str(port)],
                            cwd=str(tree), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding='utf-8', errors='replace')
    deadline = time.time() + timeout
    seen: list[str] = []
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError('sidecar exited before it was ready: rc=%s out=%r'
                                   % (proc.returncode, ''.join(seen)[-800:]))
            continue
        seen.append(line)
        if 'listening on' in line:
            return proc, ''.join(seen), kind
    proc.kill()
    raise RuntimeError('sidecar did not become ready in %.0fs: %r'
                       % (timeout, ''.join(seen)[-800:]))


def run_ps1(tree: Path, script: str, args: list[str],
            env: dict | None = None) -> tuple[int, str]:
    exe = shutil.which('powershell') or shutil.which('pwsh')
    if not exe:
        raise RuntimeError('no powershell on PATH')
    cmd = [exe, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
           str(tree / script), *args]
    full = dict(os.environ)
    if env:
        full.update(env)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=180, env=full)
    return proc.returncode, (proc.stdout or '') + (proc.stderr or '')


def verify(tree: Path | None = None, keep: bool = False) -> dict:
    """Prove the package works: closure, sidecar over HTTP, installer round trip."""
    tmp = Path(tempfile.mkdtemp(prefix='mai-pack-'))
    try:
        info = build(out_dir=tmp / 'build', quiet=True) if tree is None else None
        if tree is None:
            tree = info['tree']
        tree = Path(tree)
        problems: list[str] = []
        notes: list[str] = []

        # 1. the sidecar closure is complete inside the package
        side_dir = tree / SIDE
        present = {p.name for p in side_dir.glob('*.py')}
        for name in sidecar_modules():
            if name + '.py' not in present:
                problems.append(SIDE + '%s.py missing from the package' % name)
        # every local import in every shipped module must be shipped too
        shipped = {p.stem for p in side_dir.glob('*.py')}
        local_all = {p.stem for p in TOOLS.glob('*.py')}
        for name in sidecar_modules():
            src = (side_dir / (name + '.py')).read_text(encoding='utf-8')
            for mod in local_imports(src):
                if mod in local_all and mod not in shipped:
                    problems.append(SIDE + '%s.py imports %s, which is not shipped'
                                    % (name, mod))
        # 2. data files and rules
        for p in sidecar_data():
            if not (side_dir / p.name).is_file():
                problems.append(SIDE + '%s missing (opened by name at runtime)' % p.name)
        rules_shipped = list((side_dir / 'rules').glob('*.json'))
        if len(rules_shipped) != len(sidecar_rules()):
            problems.append(SIDE + 'rules has %d file(s), repo has %d'
                            % (len(rules_shipped), len(sidecar_rules())))
        # 2b. the frozen exe is there and is a real onedir build
        exe = side_dir / EXE_SUB / 'logicd.exe'
        if not exe.is_file():
            problems.append(SIDE + EXE_SUB + '/logicd.exe missing (players run this)')
        elif exe.stat().st_size < 1_000_000:
            problems.append(SIDE + EXE_SUB + '/logicd.exe is only %d B -- not a build'
                            % exe.stat().st_size)
        elif not (side_dir / EXE_SUB / '_internal').is_dir():
            problems.append(SIDE + EXE_SUB + '/_internal missing (onedir, not onefile)')
        else:
            notes.append('frozen exe %.1f MB' % (exe.stat().st_size / 1e6))

        # 3. the mod bytes are exactly the repo bytes
        for rel in ('mod.hjson', 'scripts/main.js'):
            a = sha256(MOD / rel)
            b = sha256(tree / 'mod' / 'MlogAuxiliary' / rel)
            if a != b:
                problems.append('mod/MlogAuxiliary/%s differs from the repo copy' % rel)

        # 4. the packaged sidecar actually serves
        port = free_port()
        proc, banner, kind = start_sidecar(tree, port)
        notes.append('started the packaged sidecar via the %s' % kind)
        try:
            st, health = http_get(port, '/health')
            if st != 200 or not health.get('ok'):
                problems.append('/health answered %s %r' % (st, health))
            elif health.get('version') != sidecar_version():
                problems.append('/health version %r != tools/logicd.py SIDECAR_VERSION %r'
                                % (health.get('version'), sidecar_version()))
            else:
                notes.append('sidecar %s on port %d, /health ok' % (health['version'], port))
            st, caps = http_get(port, '/capabilities')
            if st != 200 or not caps.get('ok', True):
                problems.append('/capabilities answered %s %r' % (st, caps))
            else:
                notes.append('/capabilities ok (%d keys)' % len(caps))
            # /health only proves the process started; /analyze proves the
            # closure is complete (rule_ids is imported inside a function, so a
            # package missing it answers /health and fails here).
            st, res = post_json(port, '/analyze',
                                {'code': 'print "hello"\nprintflush message1\n'})
            if st != 200 or res.get('ok') is not True or not res.get('statements'):
                problems.append('/analyze from the package answered %s %r' % (st, res))
            else:
                notes.append('/analyze ok (%d statement(s), %d ERR)'
                             % (res['statements'], res.get('errors', -1)))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

        # 5. install.ps1 / uninstall.ps1 against a throwaway saves\mods
        mods = tmp / 'Mindustry' / 'saves' / 'mods'
        mods.mkdir(parents=True)
        try:
            rc, out = run_ps1(tree, 'install.ps1', ['-Target', str(mods), '-Silent'])
            if rc != 0:
                problems.append('install.ps1 exited %s: %s' % (rc, out.strip()[-600:]))
            else:
                dest = mods / 'MlogAuxiliary'
                if not (dest / 'scripts' / 'main.js').is_file():
                    problems.append('install.ps1 reported ok but scripts/main.js is not there')
                elif sha256(dest / 'scripts' / 'main.js') != sha256(MOD / 'scripts/main.js'):
                    problems.append('install.ps1 copied a main.js that differs from the repo')
                else:
                    notes.append('install.ps1 copied the mod byte-identically')
            rc, out = run_ps1(tree, 'uninstall.ps1', ['-Target', str(mods), '-Silent'])
            if rc != 0:
                problems.append('uninstall.ps1 exited %s: %s' % (rc, out.strip()[-600:]))
            elif (mods / 'MlogAuxiliary').exists():
                problems.append('uninstall.ps1 reported ok but the folder is still there')
            else:
                notes.append('uninstall.ps1 removed the folder')
        except RuntimeError as exc:
            notes.append('installer round trip skipped: ' + str(exc))

        # 6. VERSION.txt lists every file the package *ships* (a run of the
        # packaged sidecar leaves __pycache__ and out/ behind, and those are not
        # part of the package -- do not require them to be hashed).
        vt = (tree / 'VERSION.txt').read_text(encoding='utf-8')
        listed = set()
        for line in vt.splitlines():
            if line.startswith('sha256 '):
                listed.add(line.split(' ', 2)[2])
        for rel, _src in layout():
            if rel not in listed:
                problems.append('VERSION.txt does not list %s' % rel)
        # the exe tree is not in layout() (it is generated), so check it apart
        exe_root = side_dir / EXE_SUB
        if exe_root.is_dir():
            for p in exe_root.rglob('*'):
                if p.is_file():
                    rel = SIDE + EXE_SUB + '/' + str(p.relative_to(exe_root)).replace('\\', '/')
                    if rel not in listed:
                        problems.append('VERSION.txt does not list %s' % rel)
        if 'MAI ' + mod_version() not in vt:
            problems.append('VERSION.txt does not say "MAI %s"' % mod_version())

        # 7. the import zip is shaped the way Mindustry's importer wants it:
        #    resolveRoot() (Mods.java:1070-1074) descends only when the root holds
        #    exactly one directory, then findMeta() (Mods.java:997) needs
        #    mod.hjson right there.  Anything else and the player gets
        #    "no mod.hjson" after picking the zip in-game.
        izp = tree.parent / (tree.name + '-mod.zip')
        if not izp.is_file():
            problems.append('%s missing -- that is the zip the player imports in-game'
                            % izp.name)
        else:
            with zipfile.ZipFile(izp) as z:
                names = [n for n in z.namelist() if not n.endswith('/')]
            tops = sorted({n.split('/', 1)[0] for n in names})
            if tops != [MOD_NAME]:
                problems.append('%s must have exactly one top-level folder %s/, got %s'
                                % (izp.name, MOD_NAME, tops))
            elif (MOD_NAME + '/mod.hjson') not in names:
                problems.append('%s has no %s/mod.hjson -- the game will say '
                                '"no mod.hjson"' % (izp.name, MOD_NAME))
            elif not any(n == MOD_NAME + '/scripts/main.js' for n in names):
                problems.append('%s has no %s/scripts/main.js' % (izp.name, MOD_NAME))
            else:
                notes.append('%s: %s/mod.hjson at the mod root (%d file(s), %d B)'
                             % (izp.name, MOD_NAME, len(names), izp.stat().st_size))

        return {'ok': not problems, 'problems': problems, 'notes': notes,
                'tree': tree, 'info': info}
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='build (and verify) the MAI package')
    ap.add_argument('--out', help='output directory (default out/_dist)')
    ap.add_argument('--no-zip', action='store_true', help='write the tree only')
    ap.add_argument('--no-exe', action='store_true',
                    help='skip the frozen sidecar/logicd.exe (dev-only package)')
    ap.add_argument('--verify', action='store_true',
                    help='build into a temp dir and prove it runs (sidecar + installer)')
    ap.add_argument('--keep', action='store_true', help='with --verify, keep the temp tree')
    args = ap.parse_args(argv)
    if args.verify:
        res = verify(keep=args.keep)
        for n in res['notes']:
            print('  ok  ' + n)
        for p in res['problems']:
            print('  FAIL ' + p)
        print('verify: ' + ('PASS' if res['ok'] else 'FAIL'))
        return 0 if res['ok'] else 1
    info = build(out_dir=Path(args.out) if args.out else None,
                 make_zip=not args.no_zip, make_exe=not args.no_exe)
    return 0


if __name__ == '__main__':
    sys.exit(main())
