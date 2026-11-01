"""The rendered `worktree-sweep` component: the weekly git object-store repair (H7.10b).

Until H7.36 `initial_setup` installed the sweep as the `Weekly git object-store repair` cron in
`/etc/cron.d/git-object-store-repair`. These tests hold what the move carries: the unit that runs
the script as `sys_user`, the timer's slot, which equals the `initial_setup_worktree_sweep_cron_*`
defaults the deadman fragment and monitor-bridge still read, the Healthchecks.io key credential
every package shipping the sweep lists (#6815), and the legacy entry. The script's exit-code ping is run in
`ansible/tests/setup/test_worktree_sweep_cron_reports_its_exit.py`.

Run: uv run pytest hosts/lib/tests/test_worktree_sweep_component.py
"""

import re

import pytest
from _checkout_package import render_daniel_box

from lib import yaml_fast
from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

PING_KEY = "healthchecks_ping_key"
INITIAL_SETUP_DEFAULTS = REPO / "ansible/roles/setup/initial_setup/defaults/main.yml"
UNITS = "/usr/lib/systemd/system"
SCRIPT = "/usr/lib/homelab/worktree-sweep"
DAYS = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> host_mode.Package:
    return render_daniel_box(
        tmp_path_factory.mktemp("worktree-sweep"), "worktree-sweep"
    )


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def _user() -> str:
    return render_context(None, "daniel-box", inventory=REPO_INVENTORY)["sys_user"]


def slot_problems(timer: str, defaults: dict) -> list[str]:
    """Why the timer's OnCalendar differs from the cron slot the defaults name, [] when equal."""
    match = re.search(r"^OnCalendar=(\w{3}) \*-\*-\* (\d\d):(\d\d):00$", timer, re.M)
    if not match:
        return ["the timer has no `OnCalendar=<day> *-*-* HH:MM:00`"]
    day, hour, minute = DAYS[match.group(1)], int(match.group(2)), int(match.group(3))
    want = (
        defaults["initial_setup_worktree_sweep_cron_weekday"],
        defaults["initial_setup_worktree_sweep_cron_hour"],
        defaults["initial_setup_worktree_sweep_cron_minute"],
    )
    return (
        []
        if (day, hour, minute) == want
        else [f"timer {(day, hour, minute)} != {want}"]
    )


def test_the_script_installs_and_the_timer_is_linked(package):
    assert package.files[SCRIPT][1] == 0o755
    assert "homelab-worktree-sweep.timer" in package.enable_links()


def test_the_service_runs_the_script_as_sys_user_and_logs_under_its_old_tag(package):
    unit = _text(package, f"{UNITS}/homelab-worktree-sweep.service")
    assert f"User={_user()}\n" in unit
    assert f"ExecStart={SCRIPT}\n" in unit
    assert "SyslogIdentifier=worktree-sweep\n" in unit
    assert "ProtectHome" not in unit


def test_the_service_loads_the_ping_key_after_the_secrets_unit(package):
    unit = _text(package, f"{UNITS}/homelab-worktree-sweep.service")
    assert f"LoadCredential={PING_KEY}:/run/credstore/{PING_KEY}\n" in unit
    assert "homelab-secrets.service" in unit


def credential_problems(packages: dict[str, dict]) -> list[str]:
    """Each package listing worktree-sweep without the Healthchecks.io key credential."""
    return [
        f"{host} lists worktree-sweep without the {PING_KEY} credential"
        for host, spec in sorted(packages.items())
        if "worktree-sweep" in spec.get("components", [])
        and PING_KEY not in spec.get("credentials", [])
    ]


def _packages() -> dict[str, dict]:
    return {
        path.parent.name: yaml_fast.safe_load(path.read_text())
        for path in (REPO / "hosts").glob("*/package.yml")
    }


def test_both_checkout_hosts_ship_the_sweep():
    shipping = {
        h for h, s in _packages().items() if "worktree-sweep" in s["components"]
    }
    assert {"daniel-box", "daniel-server"} <= shipping


def test_every_package_shipping_the_sweep_lists_its_ping_key_is_clean():
    assert credential_problems(_packages()) == []


def test_a_package_shipping_the_sweep_without_its_ping_key_is_flagged():
    packages = {"h": {"components": ["worktree-sweep"], "credentials": ["other"]}}
    assert credential_problems(packages) == [
        f"h lists worktree-sweep without the {PING_KEY} credential"
    ]


def test_the_timer_slot_equals_the_defaults_the_deadman_fragment_reads(package):
    defaults = yaml_fast.safe_load(INITIAL_SETUP_DEFAULTS.read_text())
    timer = _text(package, f"{UNITS}/homelab-worktree-sweep.timer")
    assert slot_problems(timer, defaults) == []


def test_a_timer_moved_off_the_defaults_slot_is_flagged(package):
    defaults = yaml_fast.safe_load(INITIAL_SETUP_DEFAULTS.read_text())
    timer = _text(package, f"{UNITS}/homelab-worktree-sweep.timer").replace(
        "04:20:00", "05:20:00"
    )
    assert slot_problems(timer, defaults) == ["timer (0, 5, 20) != (0, 4, 20)"]


def test_the_package_retires_the_cron_file(package):
    assert {"cron_file": "git-object-store-repair"} in package.legacy
