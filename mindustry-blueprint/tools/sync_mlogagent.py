#!/usr/bin/env python3
"""Vendor the mlog toolchain into every copy of itself.

Four copies of this toolchain exist on this machine, and this script is the one
place that writes them -- so "which version is in that copy" never becomes a
guess:

    skill    .dsh/skills/mlog-agentskill/tools        standalone skill payload
    bp       mindustry-blueprint/tools                superset used by the src tree
    install  .dsh/skills/mindustry-blueprint/tools    what DSH actually loads
    global   $DSH_HOME/skills/mlog-agentskill/tools   outside the repo, opt-in

    python tools/sync_mlogagent.py                       # skill only (default)
    python tools/sync_mlogagent.py --mirror all          # skill + bp + install
    python tools/sync_mlogagent.py --mirror bp --check   # compare, exit 1 if stale
    python tools/sync_mlogagent.py --global              # also user-level copy

Only the *tools* are synced.  `mlogagent/SKILL.md` is authored in place: it
belongs to that skill alone, while `mindustry-blueprint/SKILL.md` is a
different document.  (Copying it here once clobbered the playbook and silently
removed the skill from the catalog -- hence the explicit exclusion below.)

`--mirror all` deliberately does NOT include the global copy: that directory is
outside the checkout (`C:/Users/<you>/.dsh/skills`, a junction into
`.dsh-pack/skills/default`) and installing it changes every DSH project on the
machine.  Use `--global` for that; it composes with `--mirror` in either
order, and it always installs the *skill* payload (never bp/install).

Each mirror receives `mlogagent._MODULES` + `mlogagent._DATA` plus a freshly
written MANIFEST.sha256.  `bp` and `install` are supersets -- they also carry
src-side helpers this script does not manage -- so they are updated in place:
only payload files that already exist there are overwritten.  A payload file
missing from a superset is reported, never silently created: adding files to
those trees is a decision, not a sync.  The copy of this script that lives in
bp/install is refreshed too (it is tracked and the consistency gate compares it
against tools/).

After writing, every target is self-verified by running its own
`mlogagent.py info --verify` (recomputes SHA256 against its MANIFEST).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _repo_root(start: Path) -> Path:
    """Find the checkout root by looking for `.dsh/skills`.

    This script lives in two places at *different depths*
    (`mindustry-blueprint/tools/` and, after the sync, `tools/`), so a fixed
    `parents[N]` resolves correctly in one and silently lands one level too high
    in the other (symptom: "skill directory not found").  Walking up until the
    marker appears keeps both copies honest.
    """
    for p in [start, *start.parents]:
        if (p / ".dsh" / "skills").is_dir():
            return p
    return start.parent


ROOT = _repo_root(HERE)
SKILL = ROOT / ".dsh" / "skills" / "mlog-agentskill"
DEST = SKILL / "tools"

#: DSH's user-level ("every project") skills directory.  `~/.dsh/skills` is a
#: junction into `.dsh-pack/skills/default` on this machine, so writing here is
#: the same as installing a built-in skill.
GLOBAL_SKILL = (Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
                / "skills" / "mlog-agentskill")
GLOBAL_DEST = GLOBAL_SKILL / "tools"

#: Refreshed in every target that already has it (gate compares it to tools/).
SELF = "sync_mlogagent.py"

sys.path.insert(0, str(HERE))
import mlogagent  # noqa: E402  (needs the sys.path line above)


#: target name -> (directory, may it gain files, does it carry a MANIFEST).
MIRRORS: dict[str, tuple[Path, bool, bool]] = {
    "skill": (DEST, True, True),
    "bp": (ROOT / "mindustry-blueprint" / "tools", False, True),
    "install": (ROOT / ".dsh" / "skills" / "mindustry-blueprint" / "tools", False, True),
}
MIRROR_ORDER = ["skill", "bp", "install"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def payload() -> list[str]:
    """Modules + data files, in a stable order.  Never SKILL.md."""
    return list(mlogagent._MODULES) + list(mlogagent._DATA)


def _manifest_text(d: Path, names: list[str]) -> str:
    return "".join(f"{sha256(d / n)}  {n}\n" for n in names)


def _run_verify(d: Path) -> bool | None:
    """Ask the copy in `d` to verify itself.  None = it has no MANIFEST."""
    if not (d / "MANIFEST.sha256").is_file():
        print(f"verify  : {d} -- no MANIFEST.sha256, skipped")
        return None
    exe = d / "mlogagent.py"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        cp = subprocess.run([sys.executable, str(exe), "info", "--verify"],
                            cwd=str(d), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
    except OSError as exc:
        print(f"!! verify {d}: could not run {exe}: {exc}")
        return False
    tail = [ln for ln in (cp.stdout or "").splitlines() if ln.strip()]
    print(f"verify  : {d.name or d} -> {tail[-1].strip() if tail else '(no output)'}")
    if cp.returncode != 0:
        for ln in tail:
            if "MISMATCH" in ln:
                print(f"          {ln.strip()}")
        return False
    return True


def write_target(name: str, check_only: bool, allow_absent: bool = False) -> int:
    d, may_create, want_manifest = MIRRORS[name]
    if not d.is_dir():
        # 公开仓/精简导出里没有某个镜像目录是正常的（比如 install 那份只在作者本机），
        # 不是「陈旧」——只提示一下，别让 --check 因此判定失败。
        print(f".. {name}: directory not found, skipped: {d}")
        return 0

    present = [n for n in payload() if (d / n).is_file()]
    absent = [n for n in payload() if n not in present]
    rc = 0

    if check_only:
        stale = [n for n in present if sha256(HERE / n) != sha256(d / n)]
        for n in stale:
            print(f"  {name}/{n}: differs")
        if absent:
            print(f"  {name}: absent payload {', '.join(absent)}")
            if not allow_absent:
                # write mode already refuses (rc=1), and a silently short copy is
                # worse than a loud one: a missing data JSON degrades to "{}" in
                # every consumer and the tests then grade a table that is not there.
                print(f"STALE  {name}: {len(absent)} payload file(s) missing from {d}")
                print(f"  run: python tools/sync_mlogagent.py --mirror {name}"
                      f"   (or pass --allow-absent if this tree is meant to be short)")
                return 1
        if want_manifest:
            want = _manifest_text(d, present)
            mp = d / "MANIFEST.sha256"
            if not mp.is_file():
                print(f"  {name}/MANIFEST.sha256: missing")
                stale.append("MANIFEST.sha256")
            elif mp.read_text("utf-8") != want:
                print(f"  {name}/MANIFEST.sha256: stale")
                stale.append("MANIFEST.sha256")
        sp = d / SELF
        if sp.is_file() and sha256(sp) != sha256(HERE / SELF):
            print(f"  {name}/{SELF}: differs")
            stale.append(SELF)
        if stale:
            print(f"STALE  {name}: {len(stale)} file(s) differ from {HERE}")
            print(f"  run: python tools/sync_mlogagent.py --mirror {name}")
            return 1
        print(f"ok     {name}: {len(present)} tool file(s) in sync")
        return 0

    wrote = 0
    if absent and may_create:
        # The skill payload is generated, so a new module belongs there by
        # definition: leaving it out let a vendored copy import a module that
        # does not exist (mlogcheck -> rule_ids, 2026-10-26). bp/install stay
        # "overwrite what is there" -- they are supersets with their own files.
        d.mkdir(parents=True, exist_ok=True)
        for n in absent:
            shutil.copy2(HERE / n, d / n)
            wrote += 1
        absent = []
        present = [n for n in payload() if (d / n).is_file()]   # for the manifest
    for n in present:
        dst = d / n
        if sha256(HERE / n) != sha256(dst):
            shutil.copy2(HERE / n, dst)
            wrote += 1
    if want_manifest:
        mp = d / "MANIFEST.sha256"
        text = _manifest_text(d, present)
        if not mp.is_file() or mp.read_text("utf-8") != text:
            mp.write_text(text, "utf-8")
            wrote += 1
    if absent:
        print(f"WARN   {name}: payload file(s) absent from this tree, not created: "
              f"{', '.join(absent)}")
        print(f"       (if they belong there, copy them in by hand once, then re-run)")
        rc = 1
    sp = d / SELF
    if sp.is_file() and sp.resolve() != (HERE / SELF).resolve() and sha256(sp) != sha256(HERE / SELF):
        shutil.copy2(HERE / SELF, sp)
        wrote += 1
    print(f"ok     {name}: {len(present)} tool file(s) vendored into {d}"
          f"  ({wrote} changed)")
    return rc


def install_global(check_only: bool = False) -> int:
    """Copy the vendored skill into DSH's user-level skills directory.

    Verified file-by-file afterwards, because the whole point of this copy is
    that it is the one DSH actually loads -- a half-finished copy would look
    installed while still running the previous version.
    """
    if not GLOBAL_SKILL.parent.is_dir():
        print(f"!! no global skills directory: {GLOBAL_SKILL.parent}")
        print("   (DSH_HOME unset and ~/.dsh/skills missing?)")
        return 2

    rels = ["SKILL.md", *[f"tools/{n}" for n in payload()], "tools/MANIFEST.sha256"]

    if check_only:
        bad = []
        for rel in rels:
            a, b = SKILL / rel, GLOBAL_SKILL / rel
            if not b.exists():
                bad.append(f"{rel}: missing")
            elif sha256(a) != sha256(b):
                bad.append(f"{rel}: differs")
        if bad:
            print(f"STALE  {len(bad)} file(s) differ in {GLOBAL_SKILL}:")
            for x in bad:
                print(f"  {x}")
            print("run: python tools/sync_mlogagent.py --global")
            return 1
        print(f"global : in sync with {GLOBAL_SKILL}")
        return 0

    GLOBAL_DEST.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL / "SKILL.md", GLOBAL_SKILL / "SKILL.md")
    for n in payload():
        shutil.copy2(DEST / n, GLOBAL_DEST / n)
    shutil.copy2(DEST / "MANIFEST.sha256", GLOBAL_DEST / "MANIFEST.sha256")

    bad = [rel for rel in rels if sha256(SKILL / rel) != sha256(GLOBAL_SKILL / rel)]
    if bad:
        print(f"!! {len(bad)} file(s) did not copy cleanly: {bad}")
        return 1
    print(f"global : installed {len(rels)} file(s) into {GLOBAL_SKILL}")
    print("         available in every DSH project "
          "(reload the session if the catalog does not refresh)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="targets: skill (default), bp, install, all.  --global is orthogonal.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mirror", default="skill",
                    choices=[*MIRROR_ORDER, "all"],
                    help="which copy to write: skill (default), bp, install, or all "
                         "(the three inside the repo; never the global one)")
    ap.add_argument("--check", action="store_true",
                    help="do not write; report what differs from this directory")
    ap.add_argument("--global", dest="install_global", action="store_true",
                    help="also install into $DSH_HOME/skills, so the skill is in "
                         "scope for every project (combine with --check to verify)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the per-target 'mlogagent.py info --verify' pass")
    ap.add_argument("--allow-absent", action="store_true",
                    help="with --check: tolerate payload files missing from a copy "
                         "(trimmed exports only; the repo copies must be complete)")
    args = ap.parse_args(argv)

    targets = MIRROR_ORDER if args.mirror == "all" else [args.mirror]
    if not SKILL.is_dir() and "skill" in targets:
        # 公开仓/CI 的 fresh checkout 没有 .dsh 技能目录：write_target 对缺席
        # 目录本来就跳过（return 0），这里只有显式点名 skill 才值得硬退。
        print(f"!! skill directory not found: {SKILL}")
        if args.mirror == "all" and args.check:
            print("   (checking the in-tree mirrors only; skill copy absent)")
            targets = [n for n in targets if n != "skill"]
        else:
            return 2
    names = payload()
    print(f"source : {HERE}  ({len(names)} payload file(s))")
    rc = 0
    for name in targets:
        rc = max(rc, write_target(name, check_only=args.check,
                                  allow_absent=args.allow_absent))

    if not args.check and not args.no_verify:
        for name in targets:
            d = MIRRORS[name][0]
            if d.is_dir():
                ok = _run_verify(d)
                if ok is False:
                    rc = 1

    if args.install_global:
        rc = max(rc, install_global(check_only=args.check))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
