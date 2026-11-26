"""The `pi-updater-poll` component: daniel-pi starts its updater when the builder publishes.

The render tests hold the paths, the enable link and the 15-minute schedule. The script runs
against stub `curl`, `dpkg-query`, `systemctl` and `hostname`, with the real `dpkg` comparing
versions and the real `homelab.sources` that homelab-updater ships.

Run: uv run pytest hosts/lib/tests/test_pi_updater_poll_component.py
"""

import gzip
import os
import re
from pathlib import Path

import pytest

from _component_contract import component_contract
from lib.proc_testing import fake_bin, run, write_exec
from renderer.renderer_lib import host as host_mode

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


# ioprio class numbers, as `ionice -c` takes them: a lower number is served first.
IO_CLASSES = {"realtime": 1, "best-effort": 2, "idle": 3}


@pytest.mark.parametrize(
    "unit", ["homelab-updater-poll.service", "homelab-updater.service"]
)
def test_the_updater_units_outrank_the_aide_scans_io_class(package, unit):
    """An idle-class AIDE scan must not hold the idle-class updater's apt steps in D state."""
    source = (
        package
        if unit.startswith("homelab-updater-poll")
        else host_mode.render_package("homelab-updater")
    )
    (unit_class,) = re.findall(
        r"^IOSchedulingClass=(\S+)$", _text(source, f"{UNITS}/{unit}"), re.M
    )
    # daniel-pi lists no aide-scan, so the scan's class is read where it ships.
    box = host_mode.render_host("daniel-box")
    (aide_class,) = re.findall(
        r"ionice -c(\d) \S*/aide ", _text(box, "/usr/lib/homelab/aide-check")
    )
    assert IO_CLASSES[unit_class] < int(aide_class)


def test_the_claude_md_carries_the_contract():
    contract = component_contract("pi-updater-poll")
    assert [part for part in CONTRACT_PARTS if part not in contract] == []


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
        self.dpkg_calls = root / "dpkg.calls"
        self.script = write_exec(
            root / "homelab-updater-poll", package.files[SCRIPT][0].decode()
        )
        self.bin = fake_bin(
            root / "bin",
            hostname="echo daniel-pi\n",
            dpkg=f'echo "$*" >>{self.dpkg_calls}\nexec /usr/bin/dpkg "$@"\n',
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
                    "for unit; do :; done\n"
                    "case $1 in\n"
                    f"  is-enabled) [ -e {self.masked} ] && echo masked || echo enabled ;;\n"
                    # A unit in `active` is a running oneshot: is-active exits 3 for it.
                    f'  show) grep -qx "$unit" {self.active} && echo activating || echo inactive ;;\n'
                    f'  is-active) grep -qx "$unit" {self.active} && echo activating; exit 3 ;;\n'
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

    def compares(self) -> int:
        if not self.dpkg_calls.exists():
            return 0
        calls = self.dpkg_calls.read_text().splitlines()
        return sum(call.startswith("--compare-versions ") for call in calls)

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
    # The next poll, before the updater installed it, must not start a second run.
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
        "homelab-reconcile-wait.service",
        "apt-daily-upgrade.service",
    ],
)
def test_a_busy_unit_skips_without_recording(pi, unit):
    pi.publish(NEWER)
    pi.active.write_text(f"{unit}\n")
    skipped = pi.poll()
    assert skipped.returncode == 0
    assert f"{unit} is activating" in skipped.stderr
    assert pi.starts() == 0
    pi.active.write_text("")
    pi.poll()
    assert pi.starts() == 1


def test_a_failed_fetch_exits_1_and_starts_nothing(pi):
    result = pi.poll()
    assert result.returncode == 1
    assert "could not fetch" in result.stderr
    assert pi.starts() == 0


def test_many_published_versions_cost_a_handful_of_dpkg_compares(pi):
    """An index of 134 daniel-pi versions must not cost one dpkg fork each."""
    pi.installed.write_text("0.149+g000000000095")
    # The highest sits in the middle, so neither the first nor the last row wins.
    pi.publish(*(f"0.{n}+g{n:012x}" for n in (*range(1, 75), 150, *range(75, 150))))
    result = pi.poll()
    assert result.returncode == 0, result.stderr
    assert "0.150+g000000000096 is published" in result.stderr
    assert pi.compares() <= 2


def test_a_version_the_builder_count_cannot_rank_is_still_compared(pi):
    pi.publish(OLDER, "1.0", NEWER)
    assert pi.poll().returncode == 0
    assert (pi.root / "var/lib/homelab-updater-poll/triggered").read_text() == "1.0\n"
