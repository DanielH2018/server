"""The `pi-updater-poll` component: daniel-pi starts its updater when the builder publishes (#7065).

The render tests hold the paths, the enable link and the 15-minute schedule. The script runs
against stub `curl`, `dpkg-query`, `systemctl` and `hostname`, with the real `dpkg` comparing
versions and the real `homelab.sources` that homelab-updater ships.

Run: uv run pytest hosts/lib/tests/test_pi_updater_poll_component.py
"""

import gzip
import os
from pathlib import Path

import pytest

from lib.proc_testing import fake_bin, run
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "pi-updater-poll"
SCRIPT = "/usr/lib/homelab/homelab-updater-poll"
UNITS = "/usr/lib/systemd/system"
SOURCES = "/etc/apt/sources.list.d/homelab.sources"
CONTRACT_PARTS = (
    "Autonomous-role contract",
    "Scope / exclusions:",
    "Mode (explicit and reversible):",
    "Authoritative sources:",
    "Abort valves:",
    "Required evidence, every cycle:",
    "Next-run review:",
)
INSTALLED = "0.6721+g11375cb8d4e9"
NEWER = "0.6730+gaaaaaaaaaaaa"
OLDER = "0.6700+gbbbbbbbbbbbb"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def test_the_component_ships_the_script_and_links_the_timer(package):
    assert package.files[SCRIPT][1] == 0o755
    assert {
        f"{UNITS}/homelab-updater-poll.{kind}" for kind in ("service", "timer")
    } <= set(package.files)
    assert "homelab-updater-poll.timer" in package.enable_links()
    # The daily timer stays armed as the fallback.
    assert "homelab-updater.timer" in package.enable_links()


def test_the_timer_fires_every_15_minutes(package):
    timer = _text(package, f"{UNITS}/homelab-updater-poll.timer")
    assert "OnUnitActiveSec=15min\n" in timer
    assert "Unit=" not in timer


def test_the_service_runs_the_script_and_keeps_its_state_directory(package):
    unit = _text(package, f"{UNITS}/homelab-updater-poll.service")
    assert f"ExecStart={SCRIPT}\n" in unit
    assert "StateDirectory=homelab-updater-poll\n" in unit
    assert "LoadCredential" not in unit


def test_the_component_header_carries_the_contract():
    header = "\n".join(
        line
        for line in (COMPONENT / "component.yml").read_text().splitlines()
        if line.startswith("#")
    )
    assert [part for part in CONTRACT_PARTS if part not in header] == []


def _packages_index(*rows: tuple[str, str]) -> bytes:
    stanzas = "".join(
        f"Package: {name}\nVersion: {version}\nArchitecture: arm64\n\n"
        for name, version in rows
    )
    return gzip.compress(stanzas.encode())


class Pi:
    """A stubbed daniel-pi under a temporary root, with the script ready to run."""

    def __init__(self, root: Path, package: host_mode.Package):
        self.root = root
        sources = root / SOURCES.lstrip("/")
        sources.parent.mkdir(parents=True)
        sources.write_bytes(
            host_mode.render_package("homelab-updater").files[SOURCES][0]
        )
        self.index = root / "index.gz"
        self.calls = root / "systemctl.calls"
        self.urls = root / "curl.urls"
        self.installed = root / "installed"
        self.installed.write_text(INSTALLED)
        self.masked = root / "masked"
        self.active = root / "active"
        self.active.write_text("")
        self.script = root / "homelab-updater-poll"
        self.script.write_bytes(package.files[SCRIPT][0])
        self.script.chmod(0o755)
        self.bin = fake_bin(
            root / "bin",
            hostname="echo daniel-pi\n",
            curl=(
                'out=""; url=""\n'
                "while [ $# -gt 0 ]; do case $1 in -o) out=$2; shift ;; http*) url=$1 ;; esac; shift; done\n"
                f'echo "$url" >>{self.urls}\n'
                f"[ -e {self.index} ] || exit 22\n"
                f'cp {self.index} "$out"\n'
            ),
            **{
                "dpkg-query": f"[ -s {self.installed} ] || exit 1\ncat {self.installed}\n",
                "systemctl": (
                    f'echo "$*" >>{self.calls}\n'
                    "case $1 in\n"
                    f"  is-enabled) [ -e {self.masked} ] && echo masked || echo enabled ;;\n"
                    f'  is-active) grep -qx "$3" {self.active} ;;\n'
                    "esac\n"
                ),
            },
        )

    def publish(self, *versions: str) -> None:
        self.index.write_bytes(
            _packages_index(
                ("homelab-daniel-box", "9.9999+gcccccccccccc"),
                *(("homelab-daniel-pi", v) for v in versions),
            )
        )

    def poll(self):
        return run(
            [str(self.script)],
            env={**os.environ, "HOMELAB_ROOT": str(self.root)},
            stub_bin=self.bin,
        )

    def starts(self) -> int:
        if not self.calls.exists():
            return 0
        return (
            self.calls.read_text()
            .splitlines()
            .count("start --no-block homelab-updater.service")
        )


@pytest.fixture
def pi(tmp_path, package) -> Pi:
    return Pi(tmp_path, package)


def test_a_newer_publish_starts_the_updater_once(pi):
    pi.publish(OLDER, NEWER, INSTALLED)
    first = pi.poll()
    assert first.returncode == 0, first.stderr
    assert pi.starts() == 1
    assert NEWER in first.stderr
    assert (
        pi.root / "var/lib/homelab-updater-poll/triggered"
    ).read_text() == f"{NEWER}\n"
    # The next poll, before the updater installed it, must not start a second run (#6911).
    again = pi.poll()
    assert again.returncode == 0, again.stderr
    assert pi.starts() == 1


def test_the_index_is_read_from_the_source_apt_reads(pi):
    pi.publish(NEWER)
    pi.poll()
    (url,) = pi.urls.read_text().splitlines()
    assert url.startswith("http://") and ":8791/dists/homelab/main/binary-" in url
    assert url.endswith("/Packages.gz")


@pytest.mark.parametrize("published", [INSTALLED, OLDER])
def test_an_equal_or_older_publish_starts_nothing(pi, published):
    pi.publish(published)
    result = pi.poll()
    assert result.returncode == 0, result.stderr
    assert pi.starts() == 0


def test_a_missing_package_starts_the_updater(pi):
    pi.installed.write_text("")
    pi.publish(NEWER)
    assert pi.poll().returncode == 0
    assert pi.starts() == 1


def test_a_masked_updater_timer_skips_without_recording(pi):
    pi.publish(NEWER)
    pi.masked.write_text("")
    skipped = pi.poll()
    assert skipped.returncode == 0
    assert "masked" in skipped.stderr
    assert pi.starts() == 0
    # Once the engine run unmasks the timer, the next poll starts the updater.
    pi.masked.unlink()
    assert pi.poll().returncode == 0
    assert pi.starts() == 1


@pytest.mark.parametrize(
    "unit",
    [
        "homelab-docker-engine.service",
        "homelab-updater.service",
        "homelab-reconcile.service",
        "apt-daily-upgrade.service",
    ],
)
def test_a_busy_unit_skips_without_recording(pi, unit):
    pi.publish(NEWER)
    pi.active.write_text(f"{unit}\n")
    skipped = pi.poll()
    assert skipped.returncode == 0
    assert unit in skipped.stderr
    assert pi.starts() == 0
    pi.active.write_text("")
    pi.poll()
    assert pi.starts() == 1


def test_a_failed_fetch_exits_1_and_starts_nothing(pi):
    result = pi.poll()
    assert result.returncode == 1
    assert "could not fetch" in result.stderr
    assert pi.starts() == 0
