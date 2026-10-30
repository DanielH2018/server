#!/usr/bin/env python3
"""The container case for the Pi's held Docker engine (#6590).

H7's *Verification* names the proof: a dummy `docker-ce` at version A is installed and held, the
real homelab-daniel-pi pins docker-ce to version B, and after a homelab-updater run docker-ce is
still at A and `homelab_docker_engine_pending` reads 1. The package declares no engine Depends,
so the updater never asks apt to move the engine, and the hold is apt's to keep.

`scripts/validate/host_package_cases.py` runs it after its other cases. The case:

  * builds docker-ce at A and B with `dpkg-deb` (`engine_debs`), B matching the real package's
    docker-ce pin and A not, and every other pinned package at its pin (`companion_debs`), so
    the pending series reads 1 only while docker-ce is off its pin;
  * installs A and the others and holds them, installs the real Pi package by hand, serves B
    beside a newer build of that package, runs the updater, which installs that build with
    apt, and asserts the engine and the pending series (`expect_held`);
  * serves a red fixture, the real Pi package with `Depends: docker-ce (= B)`
    (`depends_variant`), and installs it twice, differing only in the hold. WITH the hold
    (`apt-mark showhold` lists docker-ce), apt's simulation does not plan B
    (`planned_version`), the install is refused with docker-ce at A (`red_outcome` is
    `refused`), and apt's text names `docker-ce (= B)` against A (`refusal_names_the_hold`),
    which "held broken packages" alone would not. WITHOUT the hold, the same install moves
    docker-ce from A to B (`red_outcome` is `moved`), onto the pin, and the next updater run
    reads `homelab_docker_engine_pending` 0. So the hold is the only thing that kept the
    engine at A, and the pending series follows the version. The job log's `[PASS]` line
    carries both outcomes for each release leg.

Measured. The container half runs only in the `host_packages` job: it needs
`docker run --privileged --cgroupns=host` with systemd as PID 1, which `dockrun` refuses, and
daniel-box has no Docker. What ran locally, on 2026-10-30 with apt 2.8.3 as a non-root
simulation (`apt-get install -s` over a throwaway status file, the same dummy debs from
`engine_debs`, a stand-in `homelab-daniel-pi` with `Depends: docker-ce (= B)`):

  * docker-ce held at A (`Status: hold ok installed`): rc 100, `homelab-daniel-pi : Depends:
    docker-ce (= 5:29.8.1-1~ubuntu.24.04~noble) but 5:0.0.1-1~ubuntu.24.04~noble is to be
    installed` and `E: Unable to correct problems, you have held broken packages.`
  * the same status without the hold: rc 0, `Inst docker-ce [5:0.0.1-1~ubuntu.24.04~noble]
    (5:29.8.1-1~ubuntu.24.04~noble ...)`.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import shutil
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path

from renderer.renderer_lib import host as host_mode
from validate.validate_lib import secrets_case
from validate.validate_lib.host_container import PROM, CaseFailed, Container, expect
from validate.validate_lib.host_fixtures import HOST, build, deb_version, publish

ENGINE = "docker-ce"
PINS = "/usr/lib/homelab/docker-engine.pins"
UPDATER_UNIT = "homelab-updater.service"
PENDING = f'homelab_docker_engine_pending{{host="{HOST}"}} 1'
CLEARED = PENDING[:-1] + "0"
# The upstream version A carries, below any pin the real package can hold.
HELD_UPSTREAM = "0.0.1"
HOMELAB_APT = (
    "-o Dir::Etc::sourcelist=sources.list.d/homelab.sources -o Dir::Etc::sourceparts=-"
)
REFUSED = "refused"
MOVED = "moved"
NAME = "hold the Docker engine through an updater run"


def pins(package: host_mode.Package) -> dict[str, str]:
    """Each package `package`'s pins file names, with the glob its version must match."""
    rows = (row.split() for row in package.files[PINS][0].decode().splitlines())
    return {
        row[0]: row[1] for row in rows if len(row) == 2 and not row[0].startswith("#")
    }


def engine_pin(package: host_mode.Package) -> str:
    """The glob `package`'s pins file holds for docker-ce."""
    return pins(package)[ENGINE]


def _deb(out: Path, name: str, version: str) -> Path:
    """Build an empty `name` deb at `version` under `out`; return it."""
    root = out / f"root-{name}-{version}"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "DEBIAN" / "control").write_text(
        f"Package: {name}\nVersion: {version}\nArchitecture: all\n"
        "Maintainer: homelab <t@example.invalid>\n"
        "Description: Fixture stand-in for a held Docker engine package\n"
    )
    # dpkg-deb refuses a control directory a restrictive umask left group-writable.
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    deb = out / f"{name}_{version.split(':', 1)[-1]}_all.deb"
    subprocess.run(
        ["dpkg-deb", "--root-owner-group", "--build", str(root), str(deb)],
        check=True,
        stdout=subprocess.DEVNULL,
        timeout=60,
    )
    return deb


def engine_debs(out: Path, pin: str | None = None) -> list[Path]:
    """Build dummy docker-ce debs at A and B under `out`.

    Args:
        out: A directory that does not exist yet.
        pin: The docker-ce glob B must match; None reads it from the real Pi package.

    Returns:
        [A, B]. B is the pin with its revision glob set to 1; A is HELD_UPSTREAM in the same
        epoch and suffix, which sorts below B and matches no pin.
    """
    pin = pin or engine_pin(host_mode.render_host(HOST))
    epoch, rest = pin.split(":", 1)
    suffix = rest.split("-*", 1)[1]
    out.mkdir(parents=True)
    held = f"{epoch}:{HELD_UPSTREAM}-1{suffix}"
    return [_deb(out, ENGINE, version) for version in (held, pin.replace("*", "1"))]


def companion_debs(out: Path, pinned: dict[str, str] | None = None) -> list[Path]:
    """Build every pinned package but docker-ce at its pin, revision glob set to 1.

    `docker-engine pending` reads 1 while any pinned package is missing, so without these the
    series reads 1 whatever docker-ce's version. `pinned` defaults to the real Pi package's pins.
    """
    pinned = pinned or pins(host_mode.render_host(HOST))
    out.mkdir(parents=True)
    return [
        _deb(out, name, glob.replace("*", "1"))
        for name, glob in pinned.items()
        if name != ENGINE
    ]


def planned_version(simulation: str, name: str) -> str:
    """The version `apt-get install -s` output plans to install for `name`, or ""."""
    for line in simulation.splitlines():
        words = line.split()
        if words[:2] == ["Inst", name]:
            rest = (
                words[3:] if len(words) > 2 and words[2].startswith("[") else words[2:]
            )
            return rest[0].lstrip("(") if rest else ""
    return ""


def depends_variant(
    out: Path, version: str, sops_dir: Path = host_mode.HOST_SOPS
) -> Path:
    """Render the real Pi package with one more Depends, `docker-ce (= version)`; return it.

    `sops_dir` holds the ciphertext the package carries.
    """
    package = host_mode.render_host(HOST, sops_dir=sops_dir)
    package.depends.append(f"{ENGINE} (= {version})")
    host_mode.write_packages([package], out)
    return out / package.directory


def red_outcome(held: str, after: str, apt_ok: bool) -> str:
    """Name what apt did with the hold when a Depends asked for another engine version.

    Args:
        held: The version docker-ce was held at.
        after: docker-ce's installed version after the install, or "" when not installed.
        apt_ok: Whether apt exited 0.

    Returns:
        MOVED when docker-ce left the held version, REFUSED when apt failed and left it.

    Raises:
        CaseFailed: apt succeeded with docker-ce still held, so the Depends never reached
            apt's resolution of the hold.
    """
    if after != held:
        return MOVED
    if not apt_ok:
        return REFUSED
    raise CaseFailed(f"apt installed the red fixture with {ENGINE} still at {held}")


def refusal_names_the_hold(said: str, held: str, offered: str) -> bool:
    """Whether apt's text says the engine is stuck at `held` while a Depends asks for `offered`.

    apt prints "held broken packages" for any unresolvable Depends, so that line alone is no
    proof of the hold. The line that is: `homelab-daniel-pi : Depends: docker-ce (= <offered>)
    but <held> is to be installed`. It names docker-ce, the version asked for and the version
    the hold keeps. `said` is apt's stdout and stderr.
    """
    return f"{ENGINE} (= {offered})" in said and held in said


def expect_held(held: str, installed: str, prom: str, pin: str | None = None) -> None:
    """Assert docker-ce is still at `held` and the prom reads the engine pending.

    Args:
        held: The version docker-ce was held at.
        installed: What `dpkg-query -W docker-ce` read after the updater run.
        prom: homelab_host.prom's text.
        pin: The docker-ce glob the Pi package pins. When given, `installed` must sit outside
            it, so pending 1 reads "held off its pin" and not "moved onto it".

    Raises:
        CaseFailed: docker-ce moved, is gone or sits on the pin, or the prom has no pending
            series reading 1.
    """
    expect(installed == held, f"{ENGINE} is at {installed or 'nothing'}, not {held}")
    expect(
        pin is None or not fnmatchcase(installed, pin),
        f"{ENGINE} at {installed} matches the pin {pin}, so it is not held off it",
    )
    expect(PENDING in prom.splitlines(), f"the prom lacks {PENDING!r}:\n{prom}")


def _engine(c: Container) -> str:
    """docker-ce's version as dpkg holds it `ii `, or ""."""
    out = c.sh(f"dpkg-query -W -f '${{db:Status-Abbrev}}${{Version}}' {ENGINE}").stdout
    return out[3:] if out.startswith("ii ") else ""


def expect_on_hold(c: Container) -> None:
    """Assert `apt-mark showhold` lists docker-ce."""
    held = c.ok("apt-mark showhold").split()
    expect(ENGINE in held, f"apt-mark showhold lists {held}, not {ENGINE}")


def _body(cases, c: Container) -> str:
    """Run the green half and both halves of the red fixture; return what apt did."""
    build_root = cases.work / "build" / "engine"
    held_deb, offered_deb = engine_debs(build_root / "debs")
    companions = companion_debs(build_root / "companions")
    for deb in (held_deb, offered_deb, *companions):
        shutil.copy(deb, cases.work / "debs" / deb.name)
    # The other pinned packages sit at their pins, so pending reads 1 only while docker-ce
    # is off its pin.
    local = " ".join(f"/srv/debs/{deb.name}" for deb in (held_deb, *companions))
    c.ok(f"DEBIAN_FRONTEND=noninteractive apt-get install -y {local}")
    names = " ".join(deb.name.split("_", 1)[0] for deb in companions)
    c.ok(f"apt-mark hold {ENGINE} {names}")
    held = _engine(c)
    expect(bool(held), f"{ENGINE} did not install from {held_deb.name}")
    expect_on_hold(c)
    pin = engine_pin(host_mode.render_host(HOST))
    secrets_case.seal_pi(cases, c)
    labels = ("updater-head", "pi-head", *cases.tools)
    debs = " ".join(f"/srv/debs/{cases.debs[label].name}" for label in labels)
    c.ok(f"DEBIAN_FRONTEND=noninteractive apt-get install -y {debs}")
    # Joins the reconcile the postinst queued, so the updater's own cannot merge into it.
    c.ok("systemctl start homelab-reconcile.service")
    # A newer Pi build makes the updater run apt-get install with B on offer; the common
    # path would install nothing and never put the hold in front of apt.
    served = [cases.debs[label] for label in (*labels, "pi-dropin")]
    publish(cases.work / "repo", [*served, offered_deb])
    c.sh(f"systemctl start {UPDATER_UNIT}")
    status = c.ok(f"systemctl show -P ExecMainStatus {UPDATER_UNIT}").strip()
    expect(status == "0", f"the updater exited {status}")
    cases.expect_installed(c, "updater-head", "pi-dropin")
    expect_held(held, _engine(c), c.read(PROM), pin)

    # The ciphertext seal_pi wrote, so the red build differs from pi-dropin only by its Depends.
    sealed = cases.work / "build" / f"pi-sealed-{c.name}" / "sops"
    rendered = depends_variant(build_root / "red", deb_version(offered_deb), sealed)
    red = build(rendered, f"{cases.versions['updater-head']}.8", build_root / "red-deb")
    red_deb = cases.work / "debs" / f"engine-red-{red.name}"
    shutil.copy(red, red_deb)
    publish(cases.work / "repo", [*served, offered_deb, red_deb])
    # The homelab source alone, so a failed mirror fetch fails the case rather than reading
    # as a refusal.
    c.ok(f"apt-get update {HOMELAB_APT}")
    offered = deb_version(offered_deb)
    install = (
        "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends"
        f" homelab-{HOST}"
    )

    # With the hold: the plan leaves docker-ce at A, and the install is refused in terms that
    # name the engine, so the hold is what stopped it and not some other Depends.
    expect_on_hold(c)
    plan = c.sh(f"apt-get install -s --no-install-recommends homelab-{HOST}")
    planned = planned_version(plan.stdout, ENGINE)
    expect(
        plan.returncode != 0 and planned != offered,
        f"held, apt plans {ENGINE} at {planned or 'nothing'}, rc {plan.returncode}:\n"
        f"{plan.stdout}{plan.stderr}",
    )
    apt = c.sh(install)
    said = apt.stdout + apt.stderr
    with_hold = red_outcome(held, _engine(c), apt.returncode == 0)
    expect(with_hold == REFUSED, f"with the hold, the engine {with_hold}:\n{said}")
    expect(
        refusal_names_the_hold(said, held, offered),
        f"apt's refusal does not name {ENGINE} (= {offered}) against {held}:\n{said}",
    )
    expect_on_hold(c)

    # Without the hold, the same install moves the engine onto the pin. This is the red half:
    # it fails the case if B is not the indexed candidate, and it shows pending following the
    # version, so the green half's `pending 1` is not a constant.
    c.ok(f"apt-mark unhold {ENGINE}")
    free = c.sh(install)
    freed = _engine(c)
    without_hold = red_outcome(held, freed, free.returncode == 0)
    expect(
        without_hold == MOVED and freed == offered,
        f"without the hold, the engine {without_hold} at {freed or 'nothing'}, not"
        f" {offered}:\n{free.stdout}{free.stderr}",
    )
    expect(fnmatchcase(freed, pin), f"{ENGINE} at {freed} is off the pin {pin}")
    c.ok("systemctl start homelab-reconcile.service")
    c.sh(f"systemctl start {UPDATER_UNIT}")
    prom = c.read(PROM)
    expect(CLEARED in prom.splitlines(), f"pending does not read 0 at {freed}:\n{prom}")
    cause = [line.strip() for line in said.splitlines() if f"Depends: {ENGINE}" in line]
    return (
        f"with the hold {with_hold} ({cause[0] if cause else said.strip()[-120:]});"
        f" without it {without_hold} ({held} -> {freed})"
    )


def run(cases) -> None:
    """Boot a container, run the case, and record the verdict on `cases`."""
    c = cases.boot("engine")
    if c is None:
        return
    try:
        outcome = _body(cases, c)
    except (CaseFailed, subprocess.SubprocessError) as exc:
        cases.failed.append(NAME)
        print(f"[FAIL] {c.name}: {NAME}: {exc}", file=sys.stderr)
        journal = c.sh(
            f"journalctl -u {UPDATER_UNIT} -u homelab-reconcile.service -n 60"
            " --no-pager -o short-monotonic"
        ).stdout
        print(f"::group::journal after {NAME}\n{journal}::endgroup::", file=sys.stderr)
    else:
        print(
            f"[PASS] {c.name}: {NAME}: {ENGINE} stayed held and the engine reads pending;"
            f" the red fixture's Depends on the offered version: {outcome}",
            file=sys.stderr,
        )
    finally:
        c.remove()
