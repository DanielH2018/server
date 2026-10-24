"""`homelab-compose.service`: its timeout and its place in reconcile's restart contract (H5.2a3).

Each case is one of H5's *Verification* cases (docs/target-architecture/host/h5-pi-compose.md).
The unit renders from the real component with the Pi's real healthcheck variables. The restart
cases run the real reconcile against the stub `systemctl` of `_reconcile_host.py`, with the
rendered unit as the unit file.

Run: uv run pytest hosts/lib/tests/test_pi_compose_unit.py
"""

import re

import pytest
from _compose_tree import ALLOY, ComposeTree, text
from _reconcile_host import SECRETS_UNIT, Node

from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.environment import RenderError

UNIT_PATH = "/usr/lib/systemd/system/homelab-compose.service"
UNIT = "homelab-compose.service"
ENV_DESTINATION = "usr/lib/homelab/compose/compose.env"
RECONCILE_LIMIT = 30 * 60


@pytest.fixture
def tree(tmp_path):
    return ComposeTree(tmp_path)


def seconds(value):
    return int(value.rstrip("s"))


def unit_of(tree):
    return text(tree.render(), UNIT_PATH)


def setting(unit, name):
    match = re.search(rf"{name}=(\d+)", unit)
    assert match, f"the unit sets no {name}"
    return match[1]


def timeouts(unit):
    return (
        int(setting(unit, "HOMELAB_COMPOSE_WAIT_TIMEOUT")),
        int(setting(unit, "TimeoutStartSec")),
    )


def timeout_problems(unit, variables):
    """Why the unit's wait cannot cover the project's slowest healthcheck verdict."""
    wait, start = timeouts(unit)
    worst = seconds(variables["start_period"]) + int(variables["retries"]) * (
        seconds(variables["interval"]) + seconds(variables["timeout"])
    )
    problems = []
    if wait < worst:
        problems.append(f"wait {wait}s is below the {worst}s worst case")
    if start < wait + 600:
        problems.append(f"TimeoutStartSec {start}s is below the wait plus 600s")
    if start >= RECONCILE_LIMIT:
        problems.append(f"TimeoutStartSec {start}s reaches reconcile's limit")
    return problems


def pi_variables(tree):
    context, _ = host_mode.host_context("daniel-pi", {}, tree.inventory())
    return {
        "interval": context["container_healthcheck_interval"],
        "timeout": context["container_healthcheck_timeout"],
        "retries": context["container_healthcheck_retries"],
        "start_period": "10s",
    }


# The unit and its timeout.


def test_the_unit_waits_for_the_pis_slowest_verdict_and_stays_inside_reconciles_limit(
    tree,
):
    unit = unit_of(tree)

    assert timeouts(unit) == (730, 1390)
    assert timeout_problems(unit, pi_variables(tree)) == []
    assert "Wants=docker.service homelab-secrets.service" in unit
    assert "After=docker.service homelab-secrets.service" in unit
    assert not re.search(r"^Requires=", unit, re.MULTILINE)
    assert not re.search(r"^ExecStop", unit, re.MULTILINE)
    assert "ExecStart=/usr/lib/homelab/compose-run" in unit
    assert "systemd-tmpfiles --create /usr/lib/tmpfiles.d/homelab-compose.conf" in unit


def test_a_fixture_unit_with_a_wait_timeout_of_600_is_flagged(tree):
    unit = unit_of(tree).replace(
        "HOMELAB_COMPOSE_WAIT_TIMEOUT=730", "HOMELAB_COMPOSE_WAIT_TIMEOUT=600"
    )

    assert timeout_problems(unit, pi_variables(tree)) != []


def test_a_larger_start_period_lengthens_both_timeouts_and_an_hour_is_refused(tree):
    tree.write(defaults={"compose_start_period": "30s"})
    assert timeouts(unit_of(tree)) == (750, 1410)

    tree.write(defaults={"compose_start_period": "1h"})
    with pytest.raises(RenderError, match="whole_seconds_or_minutes"):
        unit_of(tree)


def follows_the_project(tree):
    """Whether editing a compose file of the project changes the rendered unit."""
    before = unit_of(tree)
    tree.edit("alloy", "compose.yaml.j2", ALLOY + "    # one more line\n")
    return unit_of(tree) != before


def test_the_inputs_line_follows_any_file_of_the_project(tree):
    unit = unit_of(tree)
    assert unit_of(tree) == unit
    assert re.search(r"HOMELAB_COMPOSE_INPUTS_SHA256=[0-9a-f]{64}", unit)

    assert follows_the_project(tree)
    changed = unit_of(tree)
    tree.edit("alloy", "config.alloy.j2", "// a different config\n")
    assert unit_of(tree) != changed


def test_a_unit_template_without_the_inputs_line_is_flagged(tree):
    template = tree.component / "units" / "homelab-compose.service.j2"
    template.write_text(
        "\n".join(
            line
            for line in template.read_text().splitlines()
            if "compose_inputs_sha256" not in line
        )
        + "\n"
    )

    assert not follows_the_project(tree)


# # The restart contract, against the real reconcile.


def order_problems(writes):
    """Why compose is restarted before the secrets unit's restart has returned."""
    calls = [tuple(call) for call in writes]
    secrets, compose = ("restart", SECRETS_UNIT), ("restart", UNIT)
    if (
        secrets in calls
        and compose in calls
        and calls.index(compose) < calls.index(secrets)
    ):
        return ["homelab-compose restarted before homelab-secrets"]
    return []


@pytest.fixture
def compose_unit(tree):
    return unit_of(tree)


@pytest.fixture
def node(tmp_path):
    node = Node(tmp_path)
    node.unit("homelab-updater.service", policy="manual")
    node.unit("homelab-reconcile.service", policy="manual")
    node.secrets(
        outputs=[
            {"destination": ENV_DESTINATION, "persist": False, "dependents": [UNIT]}
        ]
    )
    node.output_file(ENV_DESTINATION)
    node.state["effects"] = {f"restart {SECRETS_UNIT}": {"queue": [UNIT]}}
    return node


def enable(node, compose_unit):
    node.unit(UNIT, compose_unit, active="active", enabled="enabled", linked=True)
    assert node.run().returncode == 0
    node.manifest["secrets_inputs_sha256"] = "b" * 64
    node.state["units"][UNIT]["active"] = "failed"
    node.queue(UNIT)


def calls_on(node, unit):
    return [call for call in node.writes() if call[-1] == unit]


def test_a_changed_env_restarts_secrets_then_resets_and_restarts_the_failed_unit_once(
    node, compose_unit
):
    enable(node, compose_unit)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.writes() == [
        ["restart", SECRETS_UNIT],
        ["reset-failed", UNIT],
        ["restart", UNIT],
    ]
    assert all(call[0] != "try-restart" for call in node.actions())
    assert node.pending() == []
    assert order_problems(node.writes()) == []


def test_a_log_that_restarts_compose_before_secrets_is_flagged():
    writes = [["restart", UNIT], ["restart", SECRETS_UNIT]]

    assert order_problems(writes) != []


def test_a_unit_that_is_neither_enabled_nor_active_gets_no_call(node, compose_unit):
    node.unit(UNIT, compose_unit)
    node.queue(UNIT)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert calls_on(node, UNIT) == []
    assert node.pending() == []


def test_a_restart_that_fails_fails_reconcile_and_keeps_the_unit_queued(
    node, compose_unit
):
    enable(node, compose_unit)
    node.state["fail"] = [f"restart {UNIT}"]

    result = node.run()

    assert result.returncode != 0
    assert node.pending() == [UNIT]
