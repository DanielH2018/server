"""The tick routes setup roles by origin's playbooks, and routes none when it cannot (#3734).

`setup_routing.py` is the derivation and has its own tests under `scripts/deploy_tools/tests`.
These hold the deployer's half: what `plan_tick` installs, what a failed or partial
derivation leaves the tick applying, and that the subprocess's argv is one the script takes.
"""

import dataclasses
import json
import subprocess

import pytest

import deploy_phases
import deploy_setup_roles
import setup_routing
from _deploy_fakes import checkout_routing
from deploy_changes import setup_tags_for, tick_applies_setup_role

GITOPS_TASKS = "ansible/roles/setup/gitops_deploy/tasks/main.yml"


@pytest.fixture
def installed_routing():
    """The routing a test installs, put back afterwards so no later test inherits it."""
    saved = deploy_setup_roles.current_routing()
    yield
    deploy_setup_roles.use_routing(saved)


def _with_routing(tick, routing):
    return dataclasses.replace(tick.tools, setup_routing=routing)


def test_plan_tick_routes_by_origins_tree_for_this_host(
    tick, settings, state, installed_routing
):
    asked = []

    def routing(repo, ref, host):
        asked.append((repo, ref, host))
        return checkout_routing()

    tick.paths = [GITOPS_TASKS]
    target = deploy_phases.assess(tick.tools, state, settings)
    deploy_phases.plan_tick(_with_routing(tick, routing), state, settings, target)
    assert asked == [(settings.repo, target.origin, settings.hostname)]
    assert setup_tags_for([GITOPS_TASKS]) == {"gitops_deploy"}


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(["uv"], 60),
        RuntimeError("cannot read 1234's ansible/ tree"),
        ValueError("Expecting value: line 1 column 1 (char 0)"),
    ],
    ids=["timeout", "exit-nonzero", "bad-json"],
)
def test_a_failed_derivation_routes_no_role(
    tick, settings, state, installed_routing, capsys, error
):
    """A guessed `--tags` exits 0 having applied nothing (PR #702), so nothing is guessed."""

    def routing(_repo, _ref, _host):
        raise error

    deploy_phases.adopt_setup_routing(_with_routing(tick, routing), settings, "a" * 40)
    assert not tick_applies_setup_role("gitops_deploy")
    assert setup_tags_for([GITOPS_TASKS]) == set()
    assert (
        "applies no setup role and records each one for a hand"
        in capsys.readouterr().out
    )


def test_an_unplaced_role_is_not_applied_and_its_neighbours_are(
    tick, settings, installed_routing, capsys
):
    routes, _ = checkout_routing()
    routes = {r: v for r, v in routes.items() if r != "nut_host"}
    unplaced = {"nut_host": "its gate cannot be read: `ups_host` is undefined"}

    def routing(_repo, _ref, _host):
        return routes, unplaced

    deploy_phases.adopt_setup_routing(_with_routing(tick, routing), settings, "a" * 40)
    assert not tick_applies_setup_role("nut_host")
    assert tick_applies_setup_role("gitops_deploy")
    assert "setup role nut_host cannot be routed" in capsys.readouterr().out


def test_the_subprocess_argv_is_one_the_script_takes(capsys):
    """The fakes replace the subprocess, and with it the only place a bad flag would show."""
    argv = deploy_setup_roles.routing_argv("HEAD", "daniel-box")
    assert argv[:5] == [
        "uv",
        "run",
        "--frozen",
        "python",
        deploy_setup_roles.ROUTING_SCRIPT,
    ]
    assert setup_routing.main(argv[5:]) == 0
    routes, unplaced = deploy_setup_roles.routes_from_json(capsys.readouterr().out)
    assert routes["chezmoi_setup"].tag == "chezmoi"
    assert unplaced == {}


@pytest.mark.parametrize(
    "route",
    [
        {"playbook": 1, "tag": "x", "on_tick_host": True, "host": None},
        {"playbook": None, "tag": "x", "on_tick_host": "yes", "host": None},
        {"playbook": None, "tag": "x", "on_tick_host": True},
    ],
    ids=["playbook-not-a-path", "flag-not-a-bool", "key-missing"],
)
def test_a_malformed_route_is_rejected_rather_than_read(route):
    text = json.dumps({"routes": {"x": route}, "unplaced": {}})
    with pytest.raises(ValueError, match=r"malformed|not the routing JSON"):
        deploy_setup_roles.routes_from_json(text)


def test_a_role_gated_off_this_host_is_recorded_with_its_host(
    gitops_deploy, tick, capsys, state
):
    """#3933: `--tags optimize_pi` on daniel-box exits 0 having applied nothing."""
    tick.paths = ["ansible/roles/setup/optimize_pi/tasks/main.yml"]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    (entry,) = state.manual_plane_pending()
    assert (entry.role, entry.host) == ("optimize_pi", "daniel-pi")
    assert "--tags optimize_pi -e target=daniel-pi" in capsys.readouterr().out
