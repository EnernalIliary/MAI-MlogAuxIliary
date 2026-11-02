#!/usr/bin/env python3
"""The installable package must be installable: build it, then run it.

Why this exists
---------------
"打包安装即用" is easy to fake: zip the mod folder, list the entries, call it
done.  That package cannot work -- the mod is a front end and without the
sidecar it can only say "cannot reach the sidecar", which is exactly what the
old `out/_dist/MlogAuxiliary.zip` shipped.  So this test asserts the four things
that actually make a package usable, and it gets them by *running* what a player
would double-click:

1. the sidecar closure is complete -- every local module `logicd.py` imports
   (directly or through another module) is inside the package, and so is every
   JSON file the closure opens by name.  A missing one dies on the player's
   machine with ModuleNotFoundError, not here.
2. the packaged sidecar serves: start `sidecar/logicd.py` from the extracted
   tree, ask `/health` (version must equal tools/logicd.py SIDECAR_VERSION) and
   `/capabilities`.
3. `install.ps1` copies the mod byte-identically into a throwaway
   `Mindustry\\saves\\mods` and `uninstall.ps1` removes it again -- driven
   through the same PowerShell a double-click uses, with `-Target` instead of
   path guessing.
4. `VERSION.txt` hashes every shipped file (install.ps1 trusts it to decide
   "installed"), and the zip carries no `__pycache__`/`.pyc`.

The .bat wrappers (encoding, `%~dp0`, the busy-port guard) are covered by
out/_pack_e2e.py against the real zip; that one is manual because it starts a
real sidecar and the machine may already be running the player's own.

Exit   : 0 all clean / 1 something to fix / 2 cannot run
"""
import sys
import tempfile
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))

import package as P                                               # noqa: E402


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []

    tmp = Path(tempfile.mkdtemp(prefix='mai-packtest-'))
    try:
        info = P.build(out_dir=tmp / 'build', quiet=True)
        tree = info['tree']
        print('PACK:built %s, %d file(s), %d bytes, zip %d bytes'
              % (info['name'], len(info['files']), info['bytes'],
                 info.get('zip_bytes') or 0))

        # ---- 1. closure + data files -------------------------------------
        shipped = {p.stem for p in (tree / P.SIDE).glob('*.py')}
        local_all = {p.stem for p in P.TOOLS.glob('*.py')}
        for name in P.sidecar_modules():
            if name not in shipped:
                problems.append('sidecar/%s.py is imported but not shipped' % name)
                continue
            src = (tree / P.SIDE / (name + '.py')).read_text(encoding='utf-8')
            for mod in P.local_imports(src):
                if mod in local_all and mod not in shipped:
                    problems.append('sidecar/%s.py imports %s, which is not shipped'
                                    % (name, mod))
        notes.append('sidecar closure: %d module(s) + %d data file(s) + %d rule(s)'
                     % (len(shipped), len(P.sidecar_data()), len(P.sidecar_rules())))
        for p in P.sidecar_data():
            if not (tree / P.SIDE / p.name).is_file():
                problems.append('sidecar/%s is opened by name at runtime but missing' % p.name)
        print('PACK:closure %s' % ','.join(sorted(shipped)))

        # ---- 1b. the frozen sidecar is in the package --------------------
        # This is what makes the package "install and play": the mod spawns
        # sidecar/bin/logicd.exe itself, so the player needs no Python and no
        # console window.  A missing exe is not fatal for a dev checkout, but
        # the shipped package must have it -- otherwise the whole point is lost.
        exe = tree / P.SIDE / P.EXE_SUB / 'logicd.exe'
        if not exe.is_file():
            problems.append('%s%s/logicd.exe missing from the package'
                            % (P.SIDE, P.EXE_SUB))
        elif exe.stat().st_size < 1_000_000:
            problems.append('%s%s/logicd.exe is %d B -- not a frozen build'
                            % (P.SIDE, P.EXE_SUB, exe.stat().st_size))
        elif not (tree / P.SIDE / P.EXE_SUB / '_internal').is_dir():
            problems.append('%s%s/_internal missing -- onefile builds unpack to %%TEMP%%, '
                            'which is blocked on the machines this ships to'
                            % (P.SIDE, P.EXE_SUB))
        else:
            n_exe = len([q for q in (tree / P.SIDE / P.EXE_SUB).rglob('*') if q.is_file()])
            notes.append('frozen sidecar: %.1f MB exe, %d file(s) in %s%s/'
                         % (exe.stat().st_size / 1e6, n_exe, P.SIDE, P.EXE_SUB))
            print('PACK:exe %d B files=%d' % (exe.stat().st_size, n_exe))

        # ---- 1c. the launcher bat prefers the exe ------------------------
        bat = (tree / '启动逻辑后端.bat').read_bytes().decode('ascii', 'replace')
        if r'sidecar\bin\logicd.exe' not in bat:
            problems.append('启动逻辑后端.bat does not look for the bundled logicd.exe')
        if 'goto run_exe' not in bat:
            problems.append('启动逻辑后端.bat has no exe branch')
        print('PACK:launcher-bat %s' % ('exe-first' if 'goto run_exe' in bat else 'python-only'))

        # ---- 2. mod bytes are the repo bytes -----------------------------
        for rel in ('mod.hjson', 'README.md', 'scripts/main.js'):
            if P.sha256(P.MOD / rel) != P.sha256(tree / 'mod' / 'MlogAuxiliary' / rel):
                problems.append('mod/MlogAuxiliary/%s differs from the repo copy' % rel)
        print('PACK:mod main.js %s' % P.sha256(P.MOD / 'scripts' / 'main.js')[:16])

        # ---- 2b. the mod bundle ships (v0.17.1: keybind names live there) ---
        bundles = sorted((P.MOD / 'bundles').glob('*.properties'))
        if not bundles:
            problems.append('MlogAuxiliary/bundles/*.properties is gone -- the keybind '
                            'would show up as the raw id "mai-select"')
        for p in bundles:
            shipped = tree / 'mod' / 'MlogAuxiliary' / 'bundles' / p.name
            if not shipped.is_file():
                problems.append('bundles/%s did not make it into the package' % p.name)
            elif 'mai-select' not in shipped.read_text(encoding='utf-8'):
                problems.append('bundles/%s has no keybind.mai-select.name' % p.name)
        print('PACK:bundles %d file(s)' % len(bundles))

        # ---- 2c. 每个 scripts/*.js 都要进包（v0.18.3）----------------------
        # 用户报「本地快检显示未加载」：layout() 原来只拷 scripts/main.js，
        # 而 main.js 自己 require("rules.generated") / require("quickcheck")。
        # 少一个文件在游戏里就是 require 抛异常、RULES 恒 null，静默功能缺失。
        scripts = sorted((P.MOD / 'scripts').glob('*.js'))
        if not scripts:
            problems.append('MlogAuxiliary/scripts/*.js is empty -- nothing to ship')
        for p in scripts:
            shipped = tree / 'mod' / 'MlogAuxiliary' / 'scripts' / p.name
            if not shipped.is_file():
                problems.append('scripts/%s did not make it into the package' % p.name)
            elif P.sha256(p) != P.sha256(shipped):
                problems.append('scripts/%s differs from the repo copy' % p.name)
        main_js = (P.MOD / 'scripts' / 'main.js').read_text(encoding='utf-8')
        for name in sorted(scripts):
            stem = name.stem
            if stem == 'main':
                continue
            if ('require("%s")' % stem) in main_js:
                notes.append('scripts/%s is required by main.js' % name.name)
            else:
                notes.append('scripts/%s ships but main.js never requires it' % name.name)
        print('PACK:scripts %s' % ','.join(p.name for p in scripts))

        # ---- 3. VERSION.txt lists everything shipped ---------------------
        vt = (tree / 'VERSION.txt').read_text(encoding='utf-8')
        listed = {ln.split(' ', 2)[2] for ln in vt.splitlines() if ln.startswith('sha256 ')}
        for rel, _src in P.layout():
            if rel not in listed:
                problems.append('VERSION.txt does not list %s' % rel)
        exe_root = tree / P.SIDE / P.EXE_SUB
        if exe_root.is_dir():
            for q in exe_root.rglob('*'):
                if q.is_file():
                    rel = P.SIDE + P.EXE_SUB + '/' + str(q.relative_to(exe_root)).replace('\\', '/')
                    if rel not in listed:
                        problems.append('VERSION.txt does not list %s' % rel)
                        break
        if ('MAI ' + P.mod_version()) not in vt or ('sidecar ' + P.sidecar_version()) not in vt:
            problems.append('VERSION.txt does not name both versions')
        # v0.19.0: the mod retires a sidecar whose /health version != MOD_VERSION
        # (main.js:sidecarVersionMismatch), so equal numbers are a hard requirement,
        # not a style choice -- 0.18.4 shipped without this check and v0.19.0 first
        # built a package whose bundled backend the mod would refuse to use.
        if P.mod_version() != P.sidecar_version():
            problems.append('mod %s != sidecar %s: main.js retires a mismatched sidecar'
                            % (P.mod_version(), P.sidecar_version()))
        print('PACK:version MAI=%s sidecar=%s hashes=%d'
              % (P.mod_version(), P.sidecar_version(), len(listed)))

        # ---- 3b. the zip the game imports in-game ------------------------
        # Mindustry's importer only looks at the zip root: Mods.importMod()
        # copies the file to saves/mods/<name>.zip, loadMod() runs
        # resolveRoot() (Mods.java:1070-1074) which descends one level *only*
        # when the root holds exactly one directory, and findMeta()
        # (Mods.java:997) then wants mod.json/mod.hjson right there.  The full
        # package zip has several top-level entries, so the game reports
        # "no mod.hjson" when a player picks it -- hence the second, shaped zip.
        import_zip = info.get('import_zip') or {}
        izp = import_zip.get('zip')
        if izp is None or not Path(izp).is_file():
            problems.append('the in-game import zip (%s-mod.zip) was not written'
                            % info['name'])
        else:
            with zipfile.ZipFile(izp) as z:
                names = [n for n in z.namelist() if not n.endswith('/')]
            tops = sorted({n.split('/', 1)[0] for n in names})
            # what the game does: resolveRoot() -> single folder, findMeta() -> meta
            if tops != [P.MOD_NAME]:
                problems.append('%s: resolveRoot() would not descend -- top-level '
                                'entries are %s, want exactly [%r]'
                                % (Path(izp).name, tops, P.MOD_NAME))
            else:
                meta = [n for n in names
                        if n in tuple(P.MOD_NAME + '/' + m for m in
                                      ('mod.json', 'mod.hjson', 'plugin.json',
                                       'plugin.hjson'))]
                if not meta:
                    problems.append('%s: no meta file at %s/ -- the game says '
                                    '"no mod.hjson"' % (Path(izp).name, P.MOD_NAME))
                elif (P.MOD_NAME + '/scripts/main.js') not in names:
                    problems.append('%s: %s/scripts/main.js missing'
                                    % (Path(izp).name, P.MOD_NAME))
                elif (P.MOD_NAME + '/' + P.SIDE.split('/')[2] + '/' + P.EXE_SUB
                      + '/logicd.exe') not in names:
                    problems.append('%s: the sidecar exe is not inside the import zip'
                                    % Path(izp).name)
                else:
                    notes.append('import zip: %s/%s at root, %d file(s), %.1f MB'
                                 % (P.MOD_NAME, Path(meta[0]).name, len(names),
                                    Path(izp).stat().st_size / 1e6))
            # the package zip itself must keep failing that check -- that is the
            # bug the player hit, and it is why two zips ship
            with zipfile.ZipFile(info['zip']) as z:
                pkg_names = [n for n in z.namelist() if not n.endswith('/')]
            pkg_tops = sorted({n.split('/', 1)[0] for n in pkg_names})
            if pkg_tops == [P.MOD_NAME]:
                problems.append('the package zip now looks importable -- then the '
                                'separate import zip is dead weight, decide one shape')
            print('PACK:import-zip %s files=%d tops=%s'
                  % (Path(izp).name if izp else '-', len(names) if izp else 0,
                     ','.join(tops) if izp else '-'))

        # ---- 4. the packaged sidecar serves ------------------------------
        port = P.free_port()
        proc, banner, kind = P.start_sidecar(tree, port)
        try:
            if kind != 'exe':
                problems.append('the packaged sidecar started via %r, not the frozen exe'
                                % kind)
            st, health = P.http_get(port, '/health')
            if st != 200 or health.get('version') != P.sidecar_version():
                problems.append('/health from the package answered %s %r (want version %s)'
                                % (st, health, P.sidecar_version()))
            else:
                notes.append('packaged sidecar serves /health %s on port %d'
                             % (health['version'], port))
            st, caps = P.http_get(port, '/capabilities')
            if st != 200:
                problems.append('/capabilities answered %s %r' % (st, caps))
            # /health proves the process started; /analyze proves the closure is
            # really complete.  rule_ids is imported *inside* a function, so a
            # package missing it answers /health happily and dies here.
            st, res = P.post_json(port, '/analyze',
                                  {'code': 'print "hello"\nprintflush message1\n'})
            if st != 200 or res.get('ok') is not True or not res.get('statements'):
                problems.append('/analyze from the package answered %s %r' % (st, res))
            else:
                notes.append('packaged sidecar analysed a program: %d statement(s), %d ERR'
                             % (res['statements'], res.get('errors', -1)))
            print('PACK:sidecar health=%s capabilities=%s analyze=%s'
                  % (health.get('version'), 'ok' if st == 200 else st,
                     'ok' if res.get('ok') is True else 'FAIL'))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:                                     # noqa: BLE001
                proc.kill()

        # ---- 5. install.ps1 / uninstall.ps1 round trip -------------------
        mods = tmp / 'Mindustry' / 'saves' / 'mods'
        mods.mkdir(parents=True)
        try:
            rc, out = P.run_ps1(tree, 'install.ps1', ['-Target', str(mods), '-Silent'])
            dest = mods / 'MlogAuxiliary'
            if rc != 0:
                problems.append('install.ps1 exited %s: %s' % (rc, out.strip()[-500:]))
            elif not (dest / 'scripts' / 'main.js').is_file():
                problems.append('install.ps1 said ok but scripts/main.js is not installed')
            elif P.sha256(dest / 'scripts' / 'main.js') != P.sha256(P.MOD / 'scripts/main.js'):
                problems.append('install.ps1 installed a main.js that differs from the repo')
            else:
                notes.append('install.ps1 -> byte-identical mod in a fake saves\\mods')
            print('PACK:install rc=%s' % rc)
            rc, out = P.run_ps1(tree, 'uninstall.ps1', ['-Target', str(mods), '-Silent'])
            if rc != 0:
                problems.append('uninstall.ps1 exited %s: %s' % (rc, out.strip()[-500:]))
            elif dest.exists():
                problems.append('uninstall.ps1 said ok but the folder is still there')
            else:
                notes.append('uninstall.ps1 removed it again')
            print('PACK:uninstall rc=%s' % rc)

            # ---- 5b. which saves folder it picks without -Target --------
            # Vars.loadSettings() sets the data directory to saves\ next to the
            # game on a Steam build (Mindustry-Source/core/src/mindustry/Vars.java:494-495)
            # and only uses %APPDATA%\Mindustry otherwise.  Guessing wrong
            # installs into a folder the game never opens -- which is exactly
            # what happened once here: %APPDATA% existed, so it won, and the
            # player saw "the mod does nothing".  Both env hooks exist only so
            # this test can point the detector at fake folders.
            steam = tmp / 'steam' / 'steamapps' / 'common' / 'Mindustry'
            (steam / 'saves').mkdir(parents=True)
            appdata = tmp / 'appdata'
            (appdata / 'Mindustry' / 'saves').mkdir(parents=True)
            env = {'MAI_STEAM_ROOTS': str(steam), 'MAI_APPDATA': str(appdata)}
            rc, out = P.run_ps1(tree, 'install.ps1', ['-Silent'], env=env)
            if rc != 0:
                problems.append('install.ps1 without -Target exited %s: %s'
                                % (rc, out.strip()[-500:]))
            elif not (steam / 'saves' / 'mods' / 'MlogAuxiliary' / 'scripts' / 'main.js').is_file():
                problems.append('install.ps1 ignored the Steam saves folder (it must win over '
                                '%APPDATA%%); it picked: %s' % out.strip()[-300:])
            elif (appdata / 'Mindustry' / 'saves' / 'mods' / 'MlogAuxiliary').exists():
                problems.append('install.ps1 also wrote into %APPDATA% when a Steam folder existed')
            else:
                notes.append('install.ps1 auto-detect prefers the Steam saves folder')
            print('PACK:detect-steam rc=%s' % rc)

            # and with no game folder at all it must fall back to %APPDATA%
            env2 = {'MAI_STEAM_ROOTS': str(tmp / 'nope'), 'MAI_APPDATA': str(appdata)}
            rc, out = P.run_ps1(tree, 'install.ps1', ['-Silent'], env=env2)
            apdest = appdata / 'Mindustry' / 'saves' / 'mods' / 'MlogAuxiliary'
            if rc != 0 or not (apdest / 'scripts' / 'main.js').is_file():
                problems.append('install.ps1 did not fall back to %%APPDATA%%: rc=%s %s'
                                % (rc, out.strip()[-300:]))
            else:
                notes.append('install.ps1 falls back to %APPDATA% when no game folder is found')
            print('PACK:detect-appdata rc=%s' % rc)
        except RuntimeError as exc:            # no powershell on this machine
            print('SKIP installer round trip: %s' % exc)

        # ---- 6. encodings: the scripts are read by PowerShell and cmd ----
        # A .ps1 without a UTF-8 BOM is decoded as ANSI by PowerShell 5.1, so every
        # Chinese string inside it turns into mojibake on a GBK machine -- the
        # installer still works, it just prints garbage at the player.  This bit
        # once: an editor round trip stripped the BOM and the next install printed
        # "鍚姩閫昏緫鍚庣".  A .bat must stay pure ASCII (cmd + chcp mangles UTF-8).
        for p in sorted((tree / 'packaging').glob('*.ps1')) if (tree / 'packaging').is_dir() \
                else sorted(tree.glob('*.ps1')):
            raw = p.read_bytes()
            if not raw.startswith(b'\xef\xbb\xbf'):
                problems.append('%s has no UTF-8 BOM (PowerShell 5.1 will read it as ANSI)'
                                % p.name)
        for p in sorted(tree.glob('*.bat')):
            try:
                p.read_bytes().decode('ascii')
            except UnicodeDecodeError:
                problems.append('%s is not pure ASCII (cmd + chcp mangles UTF-8)' % p.name)
        notes.append('%d .ps1 with BOM, %d .bat pure ASCII'
                     % (len(list(tree.glob('*.ps1'))), len(list(tree.glob('*.bat')))))
        print('PACK:encoding ps1=%d bat=%d'
              % (len(list(tree.glob('*.ps1'))), len(list(tree.glob('*.bat')))))

        # ---- 7. the zip is clean ----------------------------------------
        with zipfile.ZipFile(info['zip']) as z:
            names = z.namelist()
        junk = [n for n in names if '__pycache__' in n or n.endswith('.pyc')]
        if junk:
            problems.append('zip ships build junk: %r' % junk[:3])
        if any(not n.startswith(info['name'] + '/') for n in names):
            problems.append('zip has entries outside the top folder %s/' % info['name'])
        notes.append('zip: %d entries, %d bytes, sha256 %s'
                     % (len(names), info['zip_bytes'], info['zip_sha256'][:16]))
        print('PACK:zip %d entries sha256=%s' % (len(names), info['zip_sha256'][:16]))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    for n in notes:
        print('  ok  ' + n)
    for p in problems:
        print('PROBLEM ' + p)
    print('%d check(s), %d problem(s)' % (len(notes) + len(problems), len(problems)))
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())
