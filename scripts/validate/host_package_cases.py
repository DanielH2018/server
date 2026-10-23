#!/usr/bin/env python3
"""Run the host_packages job's container cases: the updater against real apt, dpkg and systemd.

H3's *CI (builds, never signs)*, step 5 (docs/target-architecture/host/h3-pi-pilot.md). The job
builds the test image from `hosts/lib/tests/container/Dockerfile` for one Ubuntu release, then
runs this. It boots that image with systemd as PID 1 and hostname `daniel-pi`, serves the
packages from a repository signed with a throwaway key with `python3 -m http.server` on
127.0.0.1:8791 inside the container, and drives `homelab-updater` through these cases:

  * install the base version on a bare container;
  * upgrade to the head version, which drops a unit and a file and adds `Depends: tree`;
  * stop the server, and the head version's updater exits non-zero with `last_success`
    unadvanced. This is the red proof of step 3's `--error-on=any`: it fails when the
    updater lets a failed fetch through;
  * serve an index past its `Valid-Until`, and the head version's updater exits non-zero with
    `last_success` unadvanced, so a host stops trusting an index whose re-sign stopped;
  * upgrade homelab-updater under a running updater, which finishes with no restart. Both
    versions ship the archive keyring at H3.4c's path, which the test's source does not name;
  * kill the updater's cgroup during an unpack, then hold dpkg's frontend lock over the next
    run, which exits 75 and writes nothing, then run again and leave `dpkg --audit` empty;
  * the common path, which leaves `pkgcache.bin` unchanged;
  * remove the host package, and the next run reinstalls it;
  * cold-install the head version on a fresh container;
  * install the real Pi package by hand on a fresh container, then change its drop-in on
    homelab-updater.timer, and the timer, which has an enable link (H3.9b), stays active;
  * install the real Pi package by hand on a fresh container, then upgrade to one rendered with
    optimize_pi_recovery_restart_enabled false, and homelab-pi-recovery.timer, which was
    active, is stopped with no reboot (H3.8b's recovery disarm).

A last case installs a fixture package that carries `secrets-unit` with a ciphertext encrypted to
a throwaway age key, and runs `homelab-secrets` (H4.1c; `validate_lib/secrets_case.py`).

Every case that reaches step 3 asserts that the server log holds its `GET` of
`dists/homelab/InRelease` or `dists/homelab/Release`. Every case asserts that `/var/lib/apt/lists/` holds a list for the test
repository and none named after daniel-box's address.

The real homelab-updater comes from the job's base and head builds. The by-hand and disarm
cases install the head build of the real homelab-daniel-pi, `hosts/daniel-pi/`. Every other case needs a unit,
a file and a payload the real package does not ship, so it installs a fixture homelab-daniel-pi:
`hosts/lib/tests/container/fixture/`'s specs, rendered by the real renderer with the real
reconcile component and maintainer scripts.

Usage:
    uv run python scripts/validate/host_package_cases.py --image homelab-host-test:24.04 \\
        --python 3.12 --render "$RUNNER_TEMP/render" --debs "$RUNNER_TEMP/debs" \\
        --work "$RUNNER_TEMP/cases"

Exit codes: 0 every case passed; 1 a case failed, each `[FAIL]` line on stderr naming it and why,
or a container never booted; 64 a usage error.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import json
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lib.cli_help import ArgumentParser
from validate.validate_lib.host_container import (
    LAST_SUCCESS,
    PORT,
    PROM,
    CaseFailed,
    Container,
    expect,
    list_problems,
    release_fetched,
)
from validate.validate_lib import secrets_case
from validate.validate_lib.host_fixtures import (
    DROPIN,
    DROPIN_CHANGED,
    HOST_PKG,
    KEYRING,
    PAYLOAD,
    RECOVERY_TIMER,
    TEST_KEYRING,
    UNIT_CHANGE,
    UNIT_DIR,
    UPDATER,
    box_address,
    build,
    deb_version,
    newer,
    pi_disarmed_variant,
    pi_dropin_variant,
    publish,
    render_fixture,
    updater_variant,
)

TOOLS_BIN = "/usr/lib/homelab/bin"
TOOLS = ("sops", "chezmoi")
LOCK_HELD = "/run/test-lock.held"
PKGCACHE = "/var/cache/apt/pkgcache.bin"
RECONCILE_RECORD = "/var/lib/homelab-reconcile/applied.json"
# systemd's MESSAGE_ID for "Starting <unit>...", logged once per start job.
UNIT_STARTING = "7d4958e842da4a758f6c1cdc7b36dcc5"


class Cases:
    """The step-5 cases for one release, with the packages they serve."""

    def __init__(self, args) -> None:
        self.image = args.image
        self.python = args.python
        self.work = args.work.resolve()
        self.box = box_address(args.render / "head")
        self.failed: list[str] = []
        self.debs: dict[str, Path] = {}
        self.versions: dict[str, str] = {}
        # The head homelab-tools build the real Pi package Depends on (H3.11), if any.
        self.tools: tuple[str, ...] = ()
        self._prepare(args.render.resolve(), args.debs.resolve())

    def _prepare(self, render: Path, debs: Path) -> None:
        for directory in ("repo", "debs", "build"):
            path = self.work / directory
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True)
        for side in ("base", "head"):
            (deb,) = (debs / side).glob(f"{UPDATER}_*.deb")
            self._add(f"updater-{side}", deb)
        # The base is master before H3.6 or after it, so only the head build is sure to hold
        # the real Pi package. The fixture's head build carries the same file name.
        (deb,) = (debs / "head").glob(f"{HOST_PKG}_*.deb")
        self._add("pi-head", deb, name=f"real-{deb.name}")
        for deb in sorted((debs / "head").glob("homelab-tools_*_amd64.deb")):
            self._add("tools-head", deb)
            self.tools = ("tools-head",)
        base, head = self.versions["updater-base"], self.versions["updater-head"]
        if not newer(head, base):
            raise CaseFailed(
                f"the head build {head} is not newer than the base build {base}, so no "
                "case can upgrade; a dispatch needs a branch with a commit past master"
            )
        build_root = self.work / "build"
        fixture_versions = {
            "base": base,
            "head": head,
            "payload": f"{head}.2",
        }
        for variant, version in fixture_versions.items():
            rendered = render_fixture(variant, build_root / variant)
            self._add(
                f"host-{variant}",
                build(rendered, version, build_root / f"{variant}-deb"),
            )
        rendered = updater_variant(render / "head", build_root / "updater-unit")
        self._add(
            "updater-unit",
            build(rendered, f"{head}.1", build_root / "updater-unit-deb"),
        )
        rendered = pi_dropin_variant(render / "head", build_root / "pi-dropin")
        self._add(
            "pi-dropin", build(rendered, f"{head}.3", build_root / "pi-dropin-deb")
        )
        rendered = pi_disarmed_variant(build_root / "pi-disarmed")
        self._add(
            "pi-disarmed",
            build(rendered, f"{head}.4", build_root / "pi-disarmed-deb"),
        )

    def _add(self, label: str, deb: Path, name: str | None = None) -> None:
        target = self.work / "debs" / (name or deb.name)
        shutil.copy(deb, target)
        self.debs[label] = target
        self.versions[label] = deb_version(target)

    def serving(self, *labels: str, valid_until: datetime | None = None) -> None:
        publish(
            self.work / "repo",
            [self.debs[label] for label in labels],
            valid_until=valid_until,
        )

    def case(
        self, container: Container, name: str, body: Callable[[], None], fetch=True
    ) -> None:
        """Run one case; record and print its verdict.

        Args:
            container: The container the case runs in.
            name: The case's name, as the job log prints it.
            body: The case, which raises CaseFailed when an assertion does not hold.
            fetch: Whether the case reaches updater step 3, and so must leave a GET of the
                suite's Release in the server log. False asserts the log holds none.
        """
        offset = container.log_size()
        try:
            body()
            log = container.log_since(offset)
            if fetch:
                expect(release_fetched(log), "the server log holds no GET of Release")
            else:
                expect(not release_fetched(log), f"the server log holds a GET:\n{log}")
            lists = container.ok("ls /var/lib/apt/lists/").split()
            problems = list_problems(lists, self.box)
            expect(not problems, "; ".join(problems))
        except (CaseFailed, subprocess.TimeoutExpired) as exc:
            self.failed.append(name)
            print(f"[FAIL] {container.name}: {name}: {exc}", file=sys.stderr)
            journal = container.sh(
                f"journalctl -u {UPDATER}.service -u homelab-reconcile.service"
                " -n 60 --no-pager -o short-monotonic"
            ).stdout
            print(
                f"::group::journal after {name}\n{journal}::endgroup::", file=sys.stderr
            )
            return
        print(f"[PASS] {container.name}: {name}", file=sys.stderr)

    def boot(self, label: str) -> Container | None:
        container = Container(self.image, f"homelab-host-test-{label}", self.work)
        try:
            container.boot(self.python)
        except (CaseFailed, subprocess.SubprocessError) as exc:
            self.failed.append(f"boot {label}")
            print(f"[FAIL] {container.name}: boot: {exc}", file=sys.stderr)
            container.remove()
            return None
        container.serve()
        return container

    def expect_installed(self, c: Container, updater: str, host: str) -> None:
        installed = c.installed()
        want = {UPDATER: self.versions[updater], HOST_PKG: self.versions[host]}
        got = {package: installed.get(package) for package in want}
        expect(got == want, f"dpkg holds {got}, not {want}")

    def expect_reconciled(self, c: Container, updater: str, host: str) -> None:
        """Reconcile's record says its last run succeeded for these two versions."""
        try:
            result = json.loads(c.read(RECONCILE_RECORD))["result"]
        except (ValueError, KeyError) as exc:
            raise CaseFailed(f"{RECONCILE_RECORD} holds no result: {exc}") from exc
        want = {UPDATER: self.versions[updater], HOST_PKG: self.versions[host]}
        expect(
            result.get("ok") is True and result.get("versions") == want,
            f"reconcile recorded {result}, not a success for {want}",
        )

    def upgrade_path(self) -> None:
        """The chained cases: one container from a bare install to a removal."""
        c = self.boot("upgrade")
        if c is None:
            return
        try:
            self._upgrade_path(c)
        finally:
            c.remove()

    def _upgrade_path(self, c: Container) -> None:
        def install_base() -> None:
            self.serving("updater-base", "host-base")
            status = c.bootstrap(self.debs["updater-base"].name)
            expect(status == 0, f"the first run exited {status}")
            self.expect_installed(c, "updater-base", "host-base")
            mode = c.ok(f"stat -c %a {PROM}").strip()
            expect(mode == "644", f"{PROM} has mode {mode}, not 644")
            for unit in (
                "homelab-fixture-kept.service",
                "homelab-fixture-dropped.service",
            ):
                expect(c.active(unit) == "active", f"reconcile did not start {unit}")

        def stopped_server() -> None:
            # The red proof reaches a change to step 3 only through the head's updater.
            self.expect_installed(c, "updater-head", "host-head")
            # last_success holds whole seconds, so a run inside the same second as the
            # first would write the same value and hide a fetch let through.
            time.sleep(1.1)
            before = (c.read(LAST_SUCCESS), success_timestamp(c.read(PROM)))
            expect(before[0] != "", f"{LAST_SUCCESS} is empty before the case")
            c.stop_serving()
            try:
                status = c.run_updater()
            finally:
                c.serve()
            expect(status != 0, "the updater exited 0 with the repository stopped")
            after = (c.read(LAST_SUCCESS), success_timestamp(c.read(PROM)))
            expect(after == before, f"the success timestamp moved: {before} -> {after}")

        def expired_release() -> None:
            # The Valid-Until half of stopped_server: the server answers, but with an index
            # whose re-sign stopped. apt refuses it only because the source checks InRelease
            # against TEST_KEYRING; with no signature check it would accept it.
            self.expect_installed(c, "updater-head", "host-head")
            time.sleep(1.1)
            before = (c.read(LAST_SUCCESS), success_timestamp(c.read(PROM)))
            expect(before[0] != "", f"{LAST_SUCCESS} is empty before the case")
            past = datetime.now(UTC) - timedelta(hours=1)
            self.serving("updater-head", "host-head", valid_until=past)
            status = c.run_updater()
            expect(status != 0, "the updater exited 0 with the Release expired")
            after = (c.read(LAST_SUCCESS), success_timestamp(c.read(PROM)))
            expect(after == before, f"the success timestamp moved: {before} -> {after}")

        def upgrade() -> None:
            self.serving("updater-base", "host-base", "updater-head", "host-head")
            status = c.run_updater()
            expect(status == 0, f"the updater exited {status}")
            self.expect_installed(c, "updater-head", "host-head")
            expect(
                not c.exists("/usr/lib/homelab/fixture-dropped"),
                "the dropped file is still installed",
            )
            dropped = c.active("homelab-fixture-dropped.service")
            expect(dropped != "active", f"the dropped unit is {dropped}")
            kept = c.active("homelab-fixture-kept.service")
            expect(kept == "active", f"the kept unit is {kept}")
            expect(
                "tree" in c.installed(),
                "tree, a Depends of the head fixture, is not ii",
            )

        def unit_change() -> None:
            self.serving("updater-head", "host-head", "updater-unit")
            cursor = c.ok("journalctl -n 0 --show-cursor").rsplit("cursor: ", 1)[-1]
            c.sh(f"systemctl start {UPDATER}.service")
            result = c.ok(f"systemctl show -P Result {UPDATER}.service").strip()
            expect(result == "success", f"the running updater ended with {result}")
            self.expect_installed(c, "updater-unit", "host-head")
            unit = c.read(f"{UNIT_DIR}/{UPDATER}.service")
            expect(UNIT_CHANGE in unit, "the changed unit is not installed")
            starts = c.ok(
                f"journalctl --after-cursor={shlex.quote(cursor.strip())} -o cat"
                f" UNIT={UPDATER}.service MESSAGE_ID={UNIT_STARTING}"
            ).splitlines()
            expect(len(starts) == 1, f"{UPDATER}.service started {len(starts)} times")
            expect(c.exists(KEYRING), f"{KEYRING} is not installed")
            source = c.read("/etc/apt/sources.list.d/homelab.sources")
            expect(
                f"127.0.0.1:{PORT}" in source
                and f"Signed-By: /srv/repo/{TEST_KEYRING}" in source,
                f"the test's homelab.sources was replaced:\n{source}",
            )

        def kill_during_unpack() -> None:
            self.serving("updater-unit", "host-head", "host-payload")
            c.ok(
                f"systemctl start --no-block {UPDATER}.service"
                f" && timeout 300 sh -c 'until [ -e {PAYLOAD}.dpkg-new ]; do :; done'"
                f" && systemctl kill -s KILL {UPDATER}.service"
            )
            c.ok(
                "timeout 60 sh -c 'until systemctl show -P ActiveState"
                f" {UPDATER}.service | grep -qx -e failed -e inactive; do sleep 0.1; done'"
            )
            audit = c.sh("dpkg --audit").stdout
            expect(
                HOST_PKG in audit, f"dpkg --audit does not list {HOST_PKG}: {audit!r}"
            )

        def lock_held() -> None:
            before = c.written()
            c.ok(
                "systemd-run --unit=test-lock python3 -c "
                + shlex.quote(
                    "import fcntl, pathlib, time\n"
                    "lock = open('/var/lib/dpkg/lock-frontend', 'a')\n"
                    "fcntl.lockf(lock, fcntl.LOCK_EX)\n"
                    f"pathlib.Path('{LOCK_HELD}').touch()\n"
                    "time.sleep(3600)\n"
                )
            )
            try:
                c.ok(f"timeout 30 sh -c 'until [ -e {LOCK_HELD} ]; do sleep 0.1; done'")
                expect(c.sh("dpkg --audit").stdout != "", "dpkg --audit lists nothing")
                status = c.run_updater()
            finally:
                c.sh(f"systemctl stop test-lock.service; rm -f {LOCK_HELD}")
            expect(status == 75, f"the updater exited {status}, not 75")
            expect(
                c.written() == before, "the updater wrote while dpkg's lock was held"
            )

        def after_kill() -> None:
            status = c.run_updater()
            expect(status == 0, f"the updater exited {status}")
            audit = c.sh("dpkg --audit").stdout
            expect(audit == "", f"dpkg --audit still lists: {audit!r}")
            self.expect_installed(c, "updater-unit", "host-payload")

        def common_path() -> None:
            stat = f"sha256sum {PKGCACHE}; stat -c %y {PKGCACHE}"
            before = c.ok(stat)
            reconcile = "systemctl show -P InvocationID homelab-reconcile.service"
            invocation = c.ok(reconcile)
            status = c.run_updater()
            expect(status == 0, f"the updater exited {status}")
            expect(c.ok(reconcile) == invocation, "reconcile ran: not the common path")
            after = c.ok(stat)
            expect(after == before, f"{PKGCACHE} changed: {before!r} -> {after!r}")

        def remove() -> None:
            c.ok(f"DEBIAN_FRONTEND=noninteractive apt-get remove -y {HOST_PKG}")
            kept = c.active("homelab-fixture-kept.service")
            expect(kept != "active", f"prerm left the kept unit {kept}")
            expect(
                not c.exists("/usr/lib/homelab/manifest.json"),
                "the removed package's manifest is still installed",
            )
            status = c.run_updater()
            expect(status == 0, f"the run after the removal exited {status}")
            self.expect_installed(c, "updater-unit", "host-payload")
            kept = c.active("homelab-fixture-kept.service")
            expect(kept == "active", f"the reinstall left the kept unit {kept}")

        self.case(c, "install the base version", install_base)
        self.case(c, "upgrade to the head version", upgrade)
        # After the upgrade, so the run is the head build's updater. The base build's is
        # master's, which a pull request's change to step 3 does not reach.
        self.case(c, "the stopped server", stopped_server, fetch=False)
        self.case(c, "a Release past its Valid-Until", expired_release)
        self.case(c, "upgrade homelab-updater under a running updater", unit_change)
        self.case(c, "kill the updater during the unpack", kill_during_unpack)
        self.case(c, "dpkg's frontend lock held", lock_held, fetch=False)
        self.case(c, "the run after the kill", after_kill)
        self.case(c, "the common path", common_path)
        self.case(c, "remove the host package", remove)

    def cold_install(self) -> None:
        """Cold-install the head version on a fresh container."""
        c = self.boot("cold")
        if c is None:
            return

        def body() -> None:
            self.serving("updater-base", "host-base", "updater-head", "host-head")
            status = c.bootstrap(self.debs["updater-head"].name)
            expect(status == 0, f"the first run exited {status}")
            self.expect_installed(c, "updater-head", "host-head")
            expect(c.exists(PROM), f"{PROM} was not written")
            kept = c.active("homelab-fixture-kept.service")
            expect(kept == "active", f"the kept unit is {kept}")

        try:
            self.case(c, "cold-install the head version", body)
        finally:
            c.remove()

    def by_hand(self) -> None:
        """Install the real Pi package by hand, then change its drop-in on the updater's timer."""
        c = self.boot("by-hand")
        if c is None:
            return
        timer = f"{UPDATER}.timer"

        def body() -> None:
            debs = " ".join(
                f"/srv/debs/{self.debs[label].name}"
                for label in ("updater-head", "pi-head", *self.tools)
            )
            # apt-get, as bootstrap-host.sh installs on a host. dpkg -i leaves a package whose
            # Depends is not installed unconfigured, so H3.7's `Depends: tree` would fail here.
            c.ok(f"DEBIAN_FRONTEND=noninteractive apt-get install -y {debs}")
            # Joins the start the postinst queued, and returns once that reconcile ends.
            c.ok("systemctl start homelab-reconcile.service")
            self.expect_reconciled(c, "updater-head", "pi-head")
            state = c.active(timer)
            expect(state == "active", f"{timer} is {state} after the install")
            if self.tools:
                self.expect_tools_run(c)
            self.serving("updater-head", "pi-head", "pi-dropin", *self.tools)
            status = c.run_updater()
            expect(status == 0, f"the updater exited {status}")
            self.expect_installed(c, "updater-head", "pi-dropin")
            expect(
                DROPIN_CHANGED in c.read(DROPIN), "the changed drop-in is not installed"
            )
            state = c.active(timer)
            expect(state == "active", f"{timer} is {state} after its drop-in changed")

        try:
            self.case(
                c, "install the real Pi package by hand, then change its drop-in", body
            )
        finally:
            c.remove()

    def expect_tools_run(self, c: Container) -> None:
        for tool in TOOLS:
            c.ok(f"{TOOLS_BIN}/{tool} --version")
            on_path = c.sh(f"command -v {tool}").returncode
            expect(on_path != 0, f"{tool} is on PATH; it installs at {TOOLS_BIN} only")

    def disarm(self) -> None:
        """Install the real Pi package by hand, then upgrade to one whose recovery timer is disarmed.

        The disarmed build drops the timer's enable link. Reconcile step 1 stops a timer whose
        link the new manifest dropped, so the timer stops on that install, with no reboot.
        """
        c = self.boot("disarm")
        if c is None:
            return

        def body() -> None:
            debs = " ".join(
                f"/srv/debs/{self.debs[label].name}"
                for label in ("updater-head", "pi-head", *self.tools)
            )
            c.ok(f"DEBIAN_FRONTEND=noninteractive apt-get install -y {debs}")
            c.ok("systemctl start homelab-reconcile.service")
            self.expect_reconciled(c, "updater-head", "pi-head")
            state = c.active(RECOVERY_TIMER)
            expect(state == "active", f"{RECOVERY_TIMER} is {state} while armed")
            self.serving("updater-head", "pi-head", "pi-disarmed", *self.tools)
            status = c.run_updater()
            expect(status == 0, f"the updater exited {status}")
            self.expect_installed(c, "updater-head", "pi-disarmed")
            self.expect_reconciled(c, "updater-head", "pi-disarmed")
            state = c.active(RECOVERY_TIMER)
            expect(state == "inactive", f"{RECOVERY_TIMER} is {state} once disarmed")

        try:
            self.case(c, "disarm the recovery timer by dropping its enable link", body)
        finally:
            c.remove()


def success_timestamp(prom: str) -> str:
    """The value of the success-timestamp series in a homelab_host.prom, or ""."""
    for line in prom.splitlines():
        if line.startswith("homelab_host_update_success_timestamp_seconds{"):
            return line.rsplit(" ", 1)[1]
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--image", required=True, help="the test image's tag")
    parser.add_argument(
        "--python", required=True, help="the python3 minor the leg's release carries"
    )
    parser.add_argument(
        "--render",
        type=Path,
        required=True,
        help="the job's render, holding base/ and head/",
    )
    parser.add_argument(
        "--debs",
        type=Path,
        required=True,
        help="the job's builds, holding base/ and head/",
    )
    parser.add_argument(
        "--work", type=Path, required=True, help="a scratch directory for the cases"
    )
    args = parser.parse_args(argv)
    try:
        cases = Cases(args)
    except CaseFailed as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    cases.upgrade_path()
    cases.cold_install()
    cases.by_hand()
    cases.disarm()
    secrets_case.run(cases)
    if cases.failed:
        print(
            f"[FAIL] {len(cases.failed)} cases failed: {cases.failed}", file=sys.stderr
        )
        return 1
    print("every case passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
