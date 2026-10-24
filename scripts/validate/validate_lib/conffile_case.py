#!/usr/bin/env python3
"""The container case for a line-arm edit in a conffile another package owns (#6088).

H7's *Red proofs*, line arm, names two upgrades of the package that owns the conffile. In the
first, the conffile is unchanged. In the second, the upgrade ships a changed conffile and runs
non-interactively under `--force-confold`, the dpkg option unattended-upgrades passes when a
host configures it. dpkg then either keeps the local file or holds the package back. The case
records which, and asserts the arm's line is present after the next reconcile.

`scripts/validate/host_package_cases.py` runs it after its other cases. The case:

  * builds fixture-conffile-owner at three versions with `dpkg-deb` (`owner_debs`): a base, an
    upgrade whose conffile is unchanged, and a later upgrade whose conffile changed;
  * installs the base owner, then the `conffile` fixture of `hosts/lib/tests/container/fixture/`,
    whose `lines:` edit lands in that conffile, and asserts reconcile wrote the line;
  * for each upgrade, serves it, upgrades the owner, records `outcome`, runs reconcile again,
    and asserts the line is there.

The line is read before that reconcile as well as after it. Reconcile re-adds a missing line, so
only the read before it can see an upgrade that replaced the local file.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from validate.validate_lib.host_container import CaseFailed, Container, expect
from validate.validate_lib.host_fixtures import build, publish, render_fixture

OWNER = "fixture-conffile-owner"
CONFFILE = f"/etc/{OWNER}.conf"
# The line the `conffile` fixture's `lines:` edit declares.
LINE = "homelab_fixture_line=kept"
# Each owner build: its version and the conffile it ships. The unchanged upgrade ships the
# base's bytes; the changed one sorts after it, so one container takes both upgrades in turn.
BASE = ("0.1", "# fixture-conffile-owner's conffile\nsetting=base\n")
UNCHANGED = ("0.2", BASE[1])
CHANGED = ("0.3", "# fixture-conffile-owner's conffile, changed\nsetting=changed\n")
KEPT = "kept"
HELD_BACK = "held back"
NAME = "upgrade the owner of a conffile the line arm edits"
# systemd's MESSAGE_ID for "Starting <unit>...", logged once per start job.
UNIT_STARTING = "7d4958e842da4a758f6c1cdc7b36dcc5"


def owner_debs(out: Path) -> list[Path]:
    """Build fixture-conffile-owner's base, unchanged and changed builds under `out`.

    Returns:
        The three debs, in that order. Each lists CONFFILE as its one conffile.
    """
    out.mkdir(parents=True)
    debs = []
    for version, conffile in (BASE, UNCHANGED, CHANGED):
        root = out / f"root-{version}"
        (root / "DEBIAN").mkdir(parents=True)
        (root / "DEBIAN" / "control").write_text(
            f"Package: {OWNER}\nVersion: {version}\nArchitecture: all\n"
            "Maintainer: homelab <t@example.invalid>\n"
            "Description: Fixture package that owns a conffile the line arm edits\n"
        )
        (root / "DEBIAN" / "conffiles").write_text(CONFFILE + "\n")
        target = root / CONFFILE.lstrip("/")
        target.parent.mkdir(parents=True)
        target.write_text(conffile)
        # dpkg-deb refuses a control directory a restrictive umask left group-writable, and the
        # modes it packs are the modes dpkg installs.
        for path in [root, *root.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        deb = out / f"{OWNER}_{version}_all.deb"
        subprocess.run(
            ["dpkg-deb", "--root-owner-group", "--build", str(root), str(deb)],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=60,
        )
        debs.append(deb)
    return debs


def outcome(before: str, offered: str, after: str, line_kept: bool) -> str:
    """Name what dpkg did with an upgrade of the owner run under `--force-confold`.

    Args:
        before: The owner's installed version before the upgrade.
        offered: The version the repository offered.
        after: The owner's installed version after the upgrade, or "" when it is not installed.
        line_kept: Whether the arm's line was in the conffile after the upgrade, before the
            next reconcile.

    Returns:
        KEPT when dpkg installed the offered version and kept the local file, HELD_BACK when
        the owner stayed at its old version.

    Raises:
        CaseFailed: The upgrade replaced the local file, or left the owner at a version that is
            neither.
    """
    if not line_kept:
        raise CaseFailed(f"the upgrade to {offered} replaced the local {CONFFILE}")
    if after == offered:
        return KEPT
    if after == before:
        return HELD_BACK
    raise CaseFailed(
        f"the upgrade from {before} to {offered} left {OWNER} at {after or 'nothing'}"
    )


def _version(c: Container) -> str:
    """The owner's version as dpkg holds it `ii `, or ""."""
    out = c.sh(f"dpkg-query -W -f '${{db:Status-Abbrev}}${{Version}}' {OWNER}").stdout
    return out[3:] if out.startswith("ii ") else ""


def _has_line(c: Container) -> bool:
    return c.sh(f"grep -qxF {shlex.quote(LINE)} {CONFFILE}").returncode == 0


def _reconcile(c: Container, unit: str) -> None:
    """Run `unit`, reconcile's oneshot, to its end; assert one new run happened and succeeded.

    The run is counted in the journal. systemd unloads an inactive oneshot, so its
    InvocationID reads "" before and after a run, and cannot tell one happened.
    """
    cursor = c.ok("journalctl -n 0 --show-cursor").rsplit("cursor: ", 1)[-1].strip()
    status = c.sh(f"systemctl start {unit}").returncode
    starts = c.ok(
        f"journalctl --after-cursor={shlex.quote(cursor)} -o cat"
        f" UNIT={unit} MESSAGE_ID={UNIT_STARTING}"
    ).splitlines()
    expect(len(starts) == 1, f"{unit} started {len(starts)} times, not once")
    expect(status == 0, f"{unit} exited non-zero ({status})")


def _upgrade(c: Container, offered: str) -> str:
    """Upgrade the owner to `offered`, which the repository serves, then reconcile.

    The upgrade is apt's, with dpkg under `--force-confold` and no terminal, as an
    unattended-upgrades run configured with that option makes it. Without the option dpkg
    would stop at the conffile prompt and fail the install.

    Returns:
        The outcome the upgrade recorded.
    """
    before = _version(c)
    c.ok(
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y"
        f" --only-upgrade -o Dpkg::Options::=--force-confold {OWNER}"
    )
    result = outcome(before, offered, _version(c), _has_line(c))
    _reconcile(c, "homelab-reconcile.service")
    expect(_has_line(c), f"{CONFFILE} lacks {LINE!r} after the next reconcile")
    return result


def _body(cases, c: Container) -> list[str]:
    build_root = cases.work / "build"
    owners = []
    for deb in owner_debs(build_root / "conffile-owner"):
        owners.append(cases.work / "debs" / deb.name)
        shutil.copy(deb, owners[-1])
    rendered = render_fixture("conffile", build_root / "conffile")
    version = f"{cases.versions['updater-head']}.6"
    deb = build(rendered, version, build_root / "conffile-deb")
    host = cases.work / "debs" / deb.name
    shutil.copy(deb, host)
    updater = cases.debs["updater-head"]
    # The owner goes first. With no owner, the arm would create the file, and the owner's
    # install would then meet a file it does not own at its conffile's path.
    c.ok(
        f"DEBIAN_FRONTEND=noninteractive apt-get install -y /srv/debs/{owners[0].name}"
    )
    c.ok(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y"
        f" /srv/debs/{updater.name} /srv/debs/{host.name}"
    )
    # Joins the start the postinst queued. Without it, the checked start below could merge into
    # that queued run, and its journal would hold no start of its own.
    c.ok("systemctl start homelab-reconcile.service")
    _reconcile(c, "homelab-reconcile.service")
    expect(_has_line(c), f"reconcile did not write {LINE!r} into {CONFFILE}")
    results = []
    for owner, (offered, _) in zip(owners[1:], (UNCHANGED, CHANGED), strict=True):
        publish(cases.work / "repo", [updater, host, owner])
        results.append(_upgrade(c, offered))
    return results


def run(cases) -> None:
    """Boot a container, run the case, and record the verdict on `cases`."""
    c = cases.boot("conffile")
    if c is None:
        return
    try:
        results = _body(cases, c)
    except (CaseFailed, subprocess.SubprocessError) as exc:
        cases.failed.append(NAME)
        print(f"[FAIL] {c.name}: {NAME}: {exc}", file=sys.stderr)
        journal = c.sh(
            "journalctl -u homelab-reconcile.service -n 60 --no-pager -o short-monotonic"
        ).stdout
        print(f"::group::journal after {NAME}\n{journal}::endgroup::", file=sys.stderr)
    else:
        unchanged, changed = results
        print(
            f"[PASS] {c.name}: {NAME}: the unchanged conffile's upgrade was {unchanged},"
            f" the changed conffile's was {changed}",
            file=sys.stderr,
        )
    finally:
        c.remove()
