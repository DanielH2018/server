"""The rendered `local-sweep` component: the shadow sweep in daniel-box's package.

Slice B1b of docs/ci-throughput.md. The tool's behaviour is tested in
`scripts/deploy_tools/tests/test_local_sweep.py`. These tests hold the unit to three things it
does not own: the tool it starts, the time limits the tool sets, and the fleet's memory bound
it runs inside.

Run: uv run pytest hosts/lib/tests/test_local_sweep_component.py
"""

import re

import pytest
from _checkout_package import render_daniel_box

import local_sweep
from lib import yaml_fast
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

UNITS = "/usr/lib/systemd/system"
FLEET = REPO / "hosts" / "components" / "claude-code" / "defaults" / "main.yml"
_BYTES = {"M": 2**20, "G": 2**30}
_SECONDS = {"min": 60, "h": 3600}


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> host_mode.Package:
    return render_daniel_box(tmp_path_factory.mktemp("local-sweep"), "local-sweep")


@pytest.fixture(scope="module")
def unit(package) -> dict[str, str]:
    """The service's directives. `Environment=` repeats and keeps its last line only."""
    text = package.files[f"{UNITS}/homelab-local-sweep.service"][0].decode()
    return dict(re.findall(r"^(\w+)=(.*)$", text, re.M))


def _size(value: str) -> int:
    return int(value[:-1]) * _BYTES[value[-1]]


def test_the_timer_is_armed_and_starts_the_tool_once_per_commit(package, unit):
    assert "homelab-local-sweep.timer" in package.enable_links()
    argv = unit["ExecStart"].split()
    (script,) = [arg for arg in argv if arg.endswith(".py")]
    assert (REPO / script).samefile(local_sweep.__file__)
    # Without the record the timer sweeps the same commit every run.
    assert "--once" in argv


def test_the_unit_outlasts_the_time_limits_the_tool_puts_on_its_checks(unit):
    ((number, suffix),) = re.findall(r"^(\d+)([a-z]+)$", unit["TimeoutStartSec"])
    checks = sum(local.timeout for local in local_sweep.LOCAL.values())
    assert int(number) * _SECONDS[suffix] > checks


def test_the_documented_replay_command_sets_what_the_unit_sets(unit):
    """A replay by hand has no unit, so the doc's command carries the unit's bounds itself."""
    doc = (REPO / "docs" / "ci-throughput.md").read_text()
    command = doc.split("#### Replaying a hosted run")[1].split("```")[1]
    properties = dict(re.findall(r"-p (\w+)=(\S+)", command))
    del properties["WorkingDirectory"]
    assert properties == {
        key: unit[key]
        for key in ("MemoryHigh", "MemorySwapMax", "SyslogIdentifier", "SyslogLevel")
    }
    # The fixture keeps the unit's last Environment= line, which is the worker count.
    assert f"-E {unit['Environment']} " in command


def test_the_sweep_runs_inside_the_fleets_bound_with_a_smaller_one_of_its_own(unit):
    fleet = yaml_fast.safe_load(FLEET.read_text())
    assert unit["Slice"] == fleet["claude_code_fleet_slice"]
    assert _size(unit["MemoryHigh"]) < _size(fleet["claude_code_fleet_memory_high"])
    assert _size(unit["MemorySwapMax"]) < _size(fleet["claude_code_fleet_swap_max"])
