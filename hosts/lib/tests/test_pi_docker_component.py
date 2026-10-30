"""The docker_install role's subjects as the `pi-docker` component on daniel-pi (H7.26).

These tests hold what the move must keep: the engine moves only through
homelab-docker-engine.service, which stops and recreates the project around the apt run; the
package can never move the engine itself; the containerd block adopts the one Ansible wrote; and
the Compose unit creates exactly the external networks the project names. The engine script runs
against the stubs in `_docker_engine_stub.py`.

Run: uv run pytest hosts/lib/tests/test_pi_docker_component.py
"""

import json
import re
import shlex
import sys
from pathlib import Path

import pytest
from _updater_host import Host
from _updater_host import reconcile_ok as _ok

from lib import yaml_fast
from lib.proc_testing import run
from lib.repo_paths import REPO
from reconcile_lib import lines
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "pi-docker"
ENGINE = COMPONENT / "docker-engine"
STUB = Path(__file__).with_name("_docker_engine_stub.py")
STUBBED = (
    "dpkg-query",
    "apt-cache",
    "apt-get",
    "apt-mark",
    "systemctl",
    "docker",
    "sleep",
)
PINS = "/usr/lib/homelab/docker-engine.pins"
COMPOSE_UNIT = "/usr/lib/systemd/system/homelab-compose.service"
ENGINE_UNIT = "/usr/lib/systemd/system/homelab-docker-engine.service"
CONTAINERD_CONFIG = "/etc/containerd/config.toml"
SUFFIX = "~ubuntu.24.04~noble"
# What `grep -v '^#' /etc/containerd/config.toml` printed on daniel-pi on 2026-10-29, with the
# markers the Ansible task wrote around its block.
LIVE_CONTAINERD = """disabled_plugins = ["cri"]

# BEGIN ANSIBLE MANAGED BLOCK (roles/setup/docker_install)
[metrics]
  address = "10.0.0.139:1338"
[plugins.'io.containerd.shim.v1.manager']
  env = ["GOGC=100", "GOMEMLIMIT=off"]
# END ANSIBLE MANAGED BLOCK (roles/setup/docker_install)
"""


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def _pins(package: host_mode.Package) -> dict[str, str]:
    rows = (row.split() for row in _text(package, PINS).splitlines())
    return {
        row[0]: row[1] for row in rows if len(row) == 2 and not row[0].startswith("#")
    }


class Engine:
    """A scratch Pi for the engine script: the rendered pins, a project file and the stubs."""

    def __init__(self, tmp_path: Path, pins: str) -> None:
        self.root = tmp_path / "root"
        self.stub = tmp_path / "stub"
        self.bin = tmp_path / "bin"
        for directory in (self.root / "usr/lib/homelab/compose", self.stub, self.bin):
            directory.mkdir(parents=True)
        (self.root / PINS.lstrip("/")).write_text(pins)
        self.project = self.root / "usr/lib/homelab/compose/compose.yaml"
        self.project.write_text("include: []\n")
        for command in STUBBED:
            wrapper = self.bin / command
            wrapper.write_text(
                f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(STUB))} "
                f'{command} "$@"\n'
            )
            wrapper.chmod(0o755)
        self.state: dict = {
            "installed": {},
            "offered": {},
            "held": [],
            "timer": "enabled",
            "active": {},
            "install_exit": 0,
        }

    def offer(
        self, pins: dict[str, str], revisions: tuple[str, ...] = ("-2", "-1")
    ) -> None:
        """Offer each pinned upstream version at `revisions`, newest first."""
        for package, glob in pins.items():
            upstream = glob.split("-*", 1)[0]
            self.state["offered"][package] = [
                f"{upstream}{r}{SUFFIX}" for r in revisions
            ]

    def run(self, *args: str):
        (self.stub / "state.json").write_text(json.dumps(self.state))
        (self.stub / "calls.jsonl").write_text("")
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOMELAB_ROOT": str(self.root),
            "STUB_STATE": str(self.stub),
        }
        result = run(["/bin/sh", str(ENGINE), *args], env=env)
        self.state = json.loads((self.stub / "state.json").read_text())
        return result

    def calls(self) -> list[list[str]]:
        text = (self.stub / "calls.jsonl").read_text()
        return [json.loads(line) for line in text.splitlines()]

    def index(self, *prefix: str) -> int:
        """The position of the first call that starts with `prefix`, or -1."""
        found = (
            i for i, c in enumerate(self.calls()) if c[: len(prefix)] == list(prefix)
        )
        return next(found, -1)


@pytest.fixture
def engine(tmp_path: Path, package: host_mode.Package) -> Engine:
    pins = _pins(package)
    pi = Engine(tmp_path, _text(package, PINS))
    pi.offer(pins)
    return pi


def _at_pins(engine: Engine, package: host_mode.Package) -> None:
    for name, versions in engine.state["offered"].items():
        engine.state["installed"][name] = versions[0]


def _behind(engine: Engine, package: host_mode.Package) -> None:
    _at_pins(engine, package)
    engine.state["installed"]["containerd.io"] = f"2.0.0-1{SUFFIX}"


# The engine script.


def test_a_fresh_host_installs_the_newest_matching_revision_and_holds_it(
    engine, package
):
    result = engine.run("apply")

    assert result.returncode == 0, result.stderr
    assert engine.state["installed"]["docker-ce"] == f"5:29.8.1-2{SUFFIX}"
    assert sorted(engine.state["held"]) == sorted(_pins(package))
    assert engine.index("docker", "compose") == -1


def test_an_engine_at_its_pins_is_held_and_nothing_stops(engine, package):
    _at_pins(engine, package)

    result = engine.run("apply")

    assert result.returncode == 0, result.stderr
    assert sorted(engine.state["held"]) == sorted(_pins(package))
    assert engine.index("apt-get", "install") == -1
    assert engine.index("docker", "compose") == -1
    assert engine.state["timer"] == "enabled"


def test_an_upgrade_waits_for_the_updater_and_stops_the_project_around_apt(
    engine, package
):
    _behind(engine, package)
    engine.state["active"] = {"homelab-updater.service": 2}

    result = engine.run("apply")

    assert result.returncode == 0, result.stderr
    assert engine.state["installed"]["containerd.io"] == f"2.3.5-2{SUFFIX}"
    mask = engine.index("systemctl", "mask")
    stop = engine.index("docker", "compose", "-f", str(engine.project), "stop")
    install = engine.index("apt-get", "install")
    recreate = engine.index("docker", "compose", "-f", str(engine.project), "up")
    assert -1 < mask < stop < install < recreate, engine.calls()
    assert "--force-recreate" in engine.calls()[recreate]
    assert engine.state["active"]["homelab-updater.service"] == 0
    assert engine.state["timer"] == "enabled"


def test_an_upgrade_over_the_edited_containerd_conffile_completes(engine, package):
    """The line arm edits containerd.io's conffile, so its upgrade must not stop at the prompt."""
    _behind(engine, package)
    engine.state["modified_conffile"] = True

    result = engine.run("apply")

    assert result.returncode == 0, result.stderr
    assert engine.state["installed"]["containerd.io"] == f"2.3.5-2{SUFFIX}"
    assert engine.state["timer"] == "enabled"


def test_an_upgrade_leaves_a_timer_the_operator_masked_masked(engine, package):
    _behind(engine, package)
    engine.state["timer"] = "masked"

    assert engine.run("apply").returncode == 0
    assert engine.index("systemctl", "unmask") == -1


def test_a_pin_apt_cannot_serve_fails_before_any_container_stops(engine, package):
    _behind(engine, package)
    engine.state["offered"]["containerd.io"] = [f"2.3.4-1{SUFFIX}"]

    result = engine.run("apply")

    assert result.returncode == 1
    assert "apt offers no containerd.io matching" in result.stderr
    assert engine.index("docker", "compose") == -1
    assert engine.index("apt-get", "install") == -1


def test_a_failed_apt_run_leaves_the_timer_masked_and_the_stop_post_holds(
    engine, package
):
    _behind(engine, package)
    engine.state["install_exit"] = 100

    assert engine.run("apply").returncode == 1
    assert engine.state["timer"] == "masked"
    assert engine.run("hold").returncode == 0
    assert sorted(engine.state["held"]) == sorted(_pins(package))


@pytest.mark.parametrize(("off_pin", "expected"), [(False, "0"), (True, "1")])
def test_pending_reads_one_only_off_the_pins(engine, package, off_pin, expected):
    (_behind if off_pin else _at_pins)(engine, package)

    result = engine.run("pending")

    assert result.stdout.strip() == expected, result.stderr


def _seconds(text: str) -> int:
    match = re.search(r"^TimeoutStartSec=(\d+)(min|s)?$", text, re.MULTILINE)
    assert match, "the unit carries no TimeoutStartSec"
    return int(match[1]) * (60 if match[2] == "min" else 1)


def limit_problems(script: str, updater_unit: str, engine_unit: str) -> list[str]:
    """Whether the engine's updater wait outlasts an updater run and fits in its own unit."""
    match = re.search(r"DOCKER_ENGINE_WAIT_LIMIT_S:-(\d+)", script)
    assert match, "the engine script names no default updater wait"
    wait = int(match[1])
    found = []
    if wait <= _seconds(updater_unit):
        found.append(f"the wait ({wait}s) ends before an updater run can")
    if _seconds(engine_unit) <= wait:
        found.append("the engine unit times out inside its own wait")
    return found


def test_the_updater_wait_outlasts_an_updater_run_is_clean(package):
    updater = REPO / "hosts/packages/homelab-updater/units/homelab-updater.service"
    found = limit_problems(
        ENGINE.read_text(), updater.read_text(), _text(package, ENGINE_UNIT)
    )
    assert found == []


def test_a_wait_at_reconciles_thirty_minutes_is_flagged(package):
    script = ENGINE.read_text().replace("LIMIT_S:-3060", "LIMIT_S:-1800")
    updater = "[Service]\nTimeoutStartSec=50min\n"

    assert limit_problems(script, updater, _text(package, ENGINE_UNIT)) == [
        "the wait (1800s) ends before an updater run can"
    ]


# The package around it.


def engine_moves(nfpm: dict, files: dict) -> list[str]:
    """What in a rendered package could move a held engine package without the engine unit."""
    held = set(
        yaml_fast.safe_load(
            (REPO / "ansible/inventory/group_vars/all.yml").read_text()
        )["docker_engine_packages"]
    )
    depends = {re.split(r"[\s(]", d, maxsplit=1)[0] for d in nfpm.get("depends", [])}
    found = [f"Depends on {name}" for name in sorted(depends & held)]
    found += [dst for dst in files if dst.startswith("/etc/apt/preferences")]
    return found


def test_the_package_cannot_move_the_engine_is_clean(package):
    nfpm = {"depends": package.depends}
    assert engine_moves(nfpm, package.files) == []
    assert _pins(package).keys() == set(
        yaml_fast.safe_load(
            (REPO / "ansible/inventory/group_vars/all.yml").read_text()
        )["docker_engine_packages"]
    )


def test_a_versioned_depends_on_the_engine_is_flagged(package):
    assert engine_moves({"depends": ["docker-ce (= 5:29.9.0-1)"]}, {}) == [
        "Depends on docker-ce"
    ]
    assert engine_moves({}, {"/etc/apt/preferences.d/docker": b""}) == [
        "/etc/apt/preferences.d/docker"
    ]


def test_reconcile_never_starts_or_restarts_the_engine_or_its_daemons(package):
    for unit in (
        "homelab-docker-engine.service",
        "docker.service",
        "containerd.service",
    ):
        assert package.policies.get(unit) == "manual", unit
    assert "\n[Install]" not in _text(package, ENGINE_UNIT)
    assert "homelab-docker-engine.service" not in package.enable_links()


def test_the_containerd_block_adopts_the_one_ansible_wrote(package):
    (edit,) = (e for e in package.lines if e["path"] == CONTAINERD_CONFIG)

    assert lines.with_block(LIVE_CONTAINERD, edit) == LIVE_CONTAINERD
    assert package.activations[CONTAINERD_CONFIG] == "manual:containerd.service"


# The Compose networks.


def _external(files: dict) -> set[str]:
    names = set()
    for dst, (content, _mode) in files.items():
        if dst.startswith("/usr/lib/homelab/compose/") and dst.endswith(
            "/compose.yaml"
        ):
            for key, spec in (
                yaml_fast.safe_load(content.decode()).get("networks") or {}
            ).items():
                if isinstance(spec, dict) and spec.get("external") is True:
                    names.add(spec.get("name", key))
    return names


def missing_networks(unit: str, files: dict) -> list[str]:
    """The external networks the compose files name and no `ExecStartPre=` line creates."""
    created = set(
        re.findall(r"^ExecStartPre=.*docker network create (\S+?)'$", unit, re.M)
    )
    return sorted(_external(files) - created)


def test_the_compose_unit_creates_every_external_network_is_clean(package):
    assert {"proxy", "lifecycle"} <= _external(package.files)
    assert missing_networks(_text(package, COMPOSE_UNIT), package.files) == []


def test_an_external_network_the_unit_omits_is_flagged(package):
    unit = _text(package, COMPOSE_UNIT)
    omitted = "\n".join(
        line for line in unit.splitlines() if "create lifecycle" not in line
    )

    assert missing_networks(omitted, package.files) == ["lifecycle"]


# The updater's report.


@pytest.mark.parametrize("pending", ["0", "1"])
def test_the_updater_reports_the_value_the_engine_script_prints(tmp_path, pending):
    host = Host(tmp_path)
    engine = host.root / "usr/lib/homelab/docker-engine"
    engine.parent.mkdir(parents=True)
    engine.write_text(f'#!/bin/sh\n[ "$1" = pending ] && echo {pending}\n')
    engine.chmod(0o755)

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert _ok(host.metrics()) == "1"
    assert (
        host.metrics()["homelab_docker_engine_pending"]
        == f'{{host="daniel-pi"}} {pending}'
    )


def test_a_host_without_the_engine_script_reports_no_engine_series(tmp_path):
    host = Host(tmp_path)

    assert host.run().returncode == 0
    assert "homelab_docker_engine_pending" not in host.metrics()
