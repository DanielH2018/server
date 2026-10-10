"""The tick routes setup roles by origin's playbooks, and routes none when it cannot (#3734).

`setup_routing.py` is the derivation and has its own tests under `scripts/deploy_tools/tests`.
These hold the deployer's half: what `plan_tick` installs, what a failed or partial
derivation leaves the tick applying, and that the subprocess's argv is one the script takes.
"""

import dataclasses
import json
import subprocess

import pytest

import deploy_cross_role
import deploy_defer
import deploy_phases
import deploy_setup_roles
import setup_routing
from _deploy_fakes import checkout_routing
from deploy_changes import setup_tags_for, tick_applies_setup_role
from deploy_remediation import broad_remediation
from gitops_markers import NO_PLAYBOOK, UNROUTED_PLAYBOOK, by_hand

GITOPS_TASKS = "ansible/roles/setup/gitops_deploy/tasks/main.yml"
INITIAL_SETUP = "ansible/initial_setup.yml"


def _with_routing(tick, routing):
    return dataclasses.replace(tick.tools, setup_routing=routing)


def test_plan_tick_routes_by_origins_tree_for_this_host(tick, settings, state):
    asked = []

    def routing(repo, ref, host):
        asked.append((repo, ref, host))
        return checkout_routing()

    # Production's starting state: the deployer's directory cannot derive routing itself, so
    # only what plan_tick installs can make the role applyable.
    deploy_setup_roles.use_routing({})
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
def test_a_failed_derivation_routes_no_role(tick, settings, state, capsys, error):
    """A guessed `--tags` exits 0 having applied nothing (PR #702), so nothing is guessed."""

    def routing(_repo, _ref, _host):
        raise error

    deploy_phases.adopt_setup_routing(_with_routing(tick, routing), settings, "a" * 40)
    assert not tick_applies_setup_role("gitops_deploy")
    assert setup_tags_for([GITOPS_TASKS]) == set()
    assert (
        "a range carrying a setup role parks until a tick can route it"
        in capsys.readouterr().out
    )


def test_an_unplaced_role_is_not_applied_and_its_neighbours_are(tick, settings, capsys):
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


def test_an_idle_tick_routes_before_it_names_a_pending_role(
    gitops_deploy, tick, capsys, state
):
    """The deployer's directory cannot derive routing, so a tick starts with none installed.

    `log_pending` runs on every tick, ahead of `plan_tick`, and an idle tick never reaches
    `plan_tick` at all.
    """
    deploy_setup_roles.use_routing({})
    playbook = "ansible/initial_setup.yml"
    state.record_manual_plane("e" * 40, playbook, "optimize_pi", 1000.0, "daniel-pi")
    tick.paths = []
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert "--tags optimize_pi -e target=daniel-pi" in capsys.readouterr().out


def test_an_unrouted_role_is_named_as_unrouted_not_as_playbookless():
    """`common` is applied by no playbook; a role the routing lost is not `common`."""
    import deploy_remediation

    deploy_setup_roles.use_routing({})
    (cmd,) = deploy_remediation._setup_commands({"gitops_deploy"})
    assert "could not be routed from the playbooks" in cmd
    assert "applied by no playbook" not in cmd


def test_a_range_the_tick_cannot_route_parks_and_records_nothing(
    gitops_deploy, tick, capsys, state
):
    """A recorded guess keys `chezmoi_setup` where `clear-owed` cannot match it, and writes
    `NO_PLAYBOOK`, which reads as `common` and never clears on a later apply."""

    def routing(_repo, _ref, _host):
        raise subprocess.TimeoutExpired(["uv"], 30)

    tick.paths = ["ansible/roles/setup/chezmoi_setup/tasks/main.yml"]
    tools = _with_routing(tick, routing)
    assert gitops_deploy.main(tools, tick.config, state) == 0
    assert tick.merges == [] and tick.playbooks == []
    assert state.manual_plane_pending() == []
    out = capsys.readouterr().out
    assert "parked, nothing merged" in out
    assert "routing failed, so it could not place chezmoi_setup" in out


def _routing_without(role, why):
    routes, _ = checkout_routing()
    routes = {r: v for r, v in routes.items() if r != role}
    unplaced = {role: why} if why else {}
    return lambda _repo, _ref, _host: (routes, unplaced)


def test_a_range_deleting_a_setup_role_fast_forwards_and_records_nothing(
    gitops_deploy, tick, capsys, state
):
    """#4326: the routing never places a deleted role, so this range parked on every tick."""
    tick.paths = ["ansible/roles/setup/fake_remux/tasks/main.yml"]
    tick.tree_listing = "ansible/roles/setup/gitops_deploy\nansible/roles/setup/k3s\n"
    tools = _with_routing(tick, _routing_without("fake_remux", None))
    assert gitops_deploy.main(tools, tick.config, state) == 0
    assert tick.merges == [tick.origin] and tick.playbooks == []
    assert state.manual_plane_pending() == []
    out = capsys.readouterr().out
    assert "fake_remux: setup role directory deleted" in out
    assert "parked" not in out


def test_a_range_carrying_an_unplaced_role_fast_forwards_and_records_it(
    gitops_deploy, tick, capsys, state
):
    """#4326: an unplaced role is a deterministic answer, so no retry would clear a park."""
    tick.paths = ["ansible/roles/setup/chezmoi_setup/tasks/main.yml"]
    tools = _with_routing(tick, _routing_without("chezmoi_setup", "two tags"))
    assert gitops_deploy.main(tools, tick.config, state) == 0
    assert tick.merges == [tick.origin] and tick.playbooks == []
    (entry,) = state.manual_plane_pending()
    assert (entry.role, entry.playbook) == ("chezmoi_setup", UNROUTED_PLAYBOOK)
    out = capsys.readouterr().out
    assert "parked" not in out
    assert "`chezmoi_setup` could not be routed from the playbooks" in out


# ── a pending line for a setup role a later range deleted is dropped (#4334) ─────────────
_SETUP_LISTING = "ansible/roles/setup/chezmoi_setup\nansible/roles/setup/k3s\n"


@pytest.mark.parametrize(
    ("listing", "owed"),
    [
        (_SETUP_LISTING, []),
        # The red half: the role is still on disk, so its line stays for a hand.
        (_SETUP_LISTING + "ansible/roles/setup/fake_remux\n", ["fake_remux"]),
        ("", ["fake_remux"]),
    ],
)
def test_a_manual_plane_line_for_a_role_deleted_since_is_dropped_on_the_next_tick(
    gitops_deploy, tick, state, listing, owed
):
    state.record_manual_plane("e" * 40, INITIAL_SETUP, "fake_remux", 1000.0)
    tick.paths = []
    tick.tree_listing = listing
    tools = _with_routing(tick, _routing_without("fake_remux", None))
    assert gitops_deploy.main(tools, tick.config, state) == 0
    assert [e.role for e in state.manual_plane_pending()] == owed


def test_a_line_keyed_by_a_live_roles_tag_is_kept(gitops_deploy, tick, state):
    """`chezmoi` names no directory; `chezmoi_setup`'s route maps it back to a live role."""
    state.record_manual_plane("e" * 40, INITIAL_SETUP, "chezmoi", 1000.0)
    tick.paths = []
    tick.tree_listing = _SETUP_LISTING
    tools = _with_routing(tick, _routing_without("fake_remux", None))
    assert gitops_deploy.main(tools, tick.config, state) == 0
    assert [e.role for e in state.manual_plane_pending()] == ["chezmoi"]


def test_a_failed_routing_drops_no_manual_plane_line(gitops_deploy, tick, state):
    """With no routes every tag key reads as deleted, so a failure must keep them all."""

    def routing(_repo, _ref, _host):
        raise subprocess.TimeoutExpired(["uv"], 30)

    state.record_manual_plane("e" * 40, INITIAL_SETUP, "chezmoi", 1000.0)
    tick.paths = []
    tick.tree_listing = _SETUP_LISTING
    assert gitops_deploy.main(_with_routing(tick, routing), tick.config, state) == 0
    assert [e.role for e in state.manual_plane_pending()] == ["chezmoi"]


# ── clear_applied keys on the --tags values the apply ran ─────────────────────────────────
def test_a_chezmoi_apply_clearing_chezmoi_setups_line_is_clean(state):
    """`chezmoi_setup` is keyed and applied as `chezmoi`, which no role directory is named."""
    state.record_manual_plane("e" * 40, INITIAL_SETUP, "chezmoi", 1000.0)
    deploy_defer.clear_applied(state, INITIAL_SETUP, ["chezmoi"])
    assert state.manual_plane_pending() == []


def test_an_off_host_tag_clearing_its_line_is_flagged(state):
    """`--tags optimize_pi` on daniel-box skips the role, so its line stays (#3933)."""
    state.record_manual_plane(
        "e" * 40, INITIAL_SETUP, "optimize_pi", 1000.0, "daniel-pi"
    )
    deploy_defer.clear_applied(state, INITIAL_SETUP, ["optimize_pi"])
    assert [e.role for e in state.manual_plane_pending()] == ["optimize_pi"]


# ── an unplaced role's line is not `common`'s, and it clears ──────────────────────────────
def test_a_role_no_playbook_applies_records_no_playbook_is_clean():
    assert deploy_defer._ledger_playbook("common") == NO_PLAYBOOK
    assert deploy_defer._ledger_playbook("k3s") == "ansible/k3s-bringup.yml"


def test_an_unplaced_roles_line_is_flagged_unrouted_and_clears_once_routed(state):
    """`NO_PLAYBOOK` read as `common` in three printers, and no later apply cleared it."""
    routes, _ = checkout_routing()
    deploy_setup_roles.use_routing(
        {r: v for r, v in routes.items() if r != "chezmoi_setup"}
    )
    assert deploy_defer._ledger_playbook("chezmoi_setup") == UNROUTED_PLAYBOOK
    assert "could not route it" in (by_hand(UNROUTED_PLAYBOOK) or "")
    assert by_hand(UNROUTED_PLAYBOOK) != by_hand(NO_PLAYBOOK)
    state.record_manual_plane("e" * 40, UNROUTED_PLAYBOOK, "chezmoi_setup", 1000.0)
    deploy_setup_roles.use_routing(routes)
    deploy_defer.clear_applied(state, INITIAL_SETUP, ["chezmoi"])
    assert state.manual_plane_pending() == []


# ── common's remediation names its consumers from the routing (#4316) ─────────────────────
def test_commons_consumer_commands_follow_the_routing_not_literals():
    """#4316: a consumer that moves host changes the printed command with it."""
    routes, _ = checkout_routing()
    moved = routes["optimize_pi"]._replace(host="daniel-elsewhere")
    deploy_setup_roles.use_routing({**routes, "optimize_pi": moved})
    cmd = broad_remediation(False, True, {"common"})
    assert "--tags optimize_pi -e target=daniel-elsewhere`" in cmd
    assert "daniel-pi" not in cmd


def test_commons_consumers_come_from_the_adopted_cross_role_table():
    """A third role rendering `resolv.conf.j2` is named once origin's table carries it."""
    path = "ansible/roles/setup/common/templates/resolv.conf.j2"
    saved = deploy_cross_role.current_tables()
    deploy_cross_role.use_tables(
        {
            **saved,
            "SETUP_FILES_ROUTED_TO_OWNER": {
                path: frozenset({"k3s", "optimize_pi", "chezmoi_setup"})
            },
        }
    )
    try:
        cmd = broad_remediation(False, True, {"common"})
    finally:
        deploy_cross_role.use_tables(saved)
    assert "`ansible-playbook ansible/initial_setup.yml --tags chezmoi`" in cmd
