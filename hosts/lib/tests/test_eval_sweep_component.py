"""The rendered `eval-sweep` component: the weekly eval sweep in daniel-box's package (H7.10b).

Until H7.36 `initial_setup` installed the sweep as a cron and rendered its Anthropic key into an
env file. These tests hold what the move carries: the two credentials that replace the env file
and the token rendered into the script, the `+` ExecStartPre that creates the state directory as
root for a service that runs as `sys_user`, the PATH and HOME the cron set, the timer's slot and
the legacy entries. The script's behaviour is run in `ansible/tests/setup/test_eval_sweep_cron.py`
and `test_eval_run_failure_path.py`.

Run: uv run pytest hosts/lib/tests/test_eval_sweep_component.py
"""

import re

import pytest
from _checkout_package import render_daniel_box

from lib import yaml_fast
from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "eval-sweep" / "component.yml"
UNITS = "/usr/lib/systemd/system"
SCRIPT = "/usr/lib/homelab/eval-run"
TMPFILES = "/usr/lib/tmpfiles.d/homelab-eval-sweep.conf"
CREDENTIALS = ("anthropic_api_key", "homelab_eval_push_token")
CONTRACT_PARTS = (
    "Autonomous-role contract",
    "Scope:",
    "Mode:",
    "Abort valves:",
    "Required evidence:",
    "Next-run review:",
)


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> host_mode.Package:
    return render_daniel_box(tmp_path_factory.mktemp("eval-sweep"), "eval-sweep")


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def _user() -> str:
    return render_context(None, "daniel-box", inventory=REPO_INVENTORY)["sys_user"]


def credential_problems(unit: str, spec: dict) -> list[str]:
    """Why `unit` does not load every credential `spec` declares, [] when it does."""
    problems = []
    for key in spec.get("credentials", []):
        if f"LoadCredential={key}:/run/credstore/{key}\n" not in unit:
            problems.append(f"{key} is declared but the unit does not load it")
    if "Wants=homelab-secrets.service\n" not in unit:
        problems.append("the unit does not want homelab-secrets.service")
    return problems


def test_the_script_tmpfiles_and_timer_install_and_link(package):
    assert package.files[SCRIPT][1] == 0o755
    assert TMPFILES in package.files
    assert "homelab-eval-sweep.timer" in package.enable_links()


def test_the_service_creates_the_state_directory_as_root_and_runs_as_sys_user(package):
    unit = _text(package, f"{UNITS}/homelab-eval-sweep.service")
    assert f"User={_user()}\n" in unit
    assert f"ExecStartPre=+/usr/bin/systemd-tmpfiles --create {TMPFILES}\n" in unit
    assert f"ExecStart={SCRIPT}\n" in unit
    assert "ProtectHome" not in unit
    assert re.search(
        rf"^d /var/lib/homelab/eval-run\.d\s+0755 {_user()} {_user()} -$",
        _text(package, TMPFILES),
        re.M,
    )


def test_the_service_keeps_the_crons_path_and_home(package):
    unit = _text(package, f"{UNITS}/homelab-eval-sweep.service")
    assert "Environment=PATH=/usr/local/bin:/usr/bin:/bin\n" in unit
    assert f"Environment=HOME=/home/{_user()}\n" in unit


def test_both_secrets_are_credentials_and_the_script_holds_neither(package):
    spec = yaml_fast.safe_load(COMPONENT.read_text())
    assert tuple(spec["credentials"]) == CREDENTIALS
    unit = _text(package, f"{UNITS}/homelab-eval-sweep.service")
    assert credential_problems(unit, spec) == []
    assert "CREDENTIALS_DIRECTORY" in _text(package, SCRIPT)


def test_a_unit_missing_a_credential_is_flagged(package):
    spec = yaml_fast.safe_load(COMPONENT.read_text())
    unit = _text(package, f"{UNITS}/homelab-eval-sweep.service")
    broken = unit.replace("LoadCredential=anthropic_api_key", "#")
    assert credential_problems(broken, spec) == [
        "anthropic_api_key is declared but the unit does not load it"
    ]


def test_the_timer_keeps_the_crons_weekly_slot(package):
    timer = _text(package, f"{UNITS}/homelab-eval-sweep.timer")
    assert re.search(r"^OnCalendar=Sun \*-\*-\* 02:00:00$", timer, re.M)


def test_the_package_retires_the_cron_file_the_script_and_the_env_file(package):
    legacy = [entry for entry in package.legacy if "eval-run" in str(entry)]
    assert {"cron_file": "eval-run"} in legacy
    assert {"path": "/usr/local/bin/eval-run.sh"} in legacy
    assert {"path": "/etc/homelab/eval-run.env"} in legacy


def test_the_component_header_carries_the_contract():
    header = "\n".join(
        line for line in COMPONENT.read_text().splitlines() if line.startswith("#")
    )
    assert [part for part in CONTRACT_PARTS if part not in header] == []
