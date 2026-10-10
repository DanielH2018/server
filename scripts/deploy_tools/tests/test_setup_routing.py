"""`setup_routing.routes` reads each setup role's playbook, tag and placement off the playbooks.

A synthetic tree carries every shape the live playbooks use, and each shape the derivation
must refuse. The live tree pins the members a rewrite must still find.
"""

from pathlib import Path

import pytest
import yaml

import setup_routing
from lib.repo_paths import REPO

INITIAL, BRINGUP, BOOTSTRAP = setup_routing.PLAYBOOKS


def _tree(tmp_path: Path, playbooks: dict[str, list], roles=(), host_vars=None) -> Path:
    """A tree with `playbooks`' `roles:` lists, a directory per role, and box/pi vars."""
    for rel, entries in playbooks.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(yaml.safe_dump([{"hosts": "x", "roles": entries}]))
    named = {e["role"] for es in playbooks.values() for e in es if isinstance(e, dict)}
    for role in named | set(roles):
        (tmp_path / "ansible/roles/setup" / role / "tasks").mkdir(parents=True)
        (tmp_path / "ansible/roles/setup" / role / "tasks/main.yml").write_text("[]\n")
    inventory = tmp_path / "ansible/inventory"
    (inventory / "host_vars").mkdir(parents=True)
    (inventory / "group_vars").mkdir()
    (inventory / "group_vars/all.yml").write_text(
        "has_gitops: false\nups_host: daniel-server\nnut_host_secondary_armed: false\n"
        "optimize_pi_host: daniel-pi\n"
    )
    for host, text in {
        "daniel-box": "has_gitops: true\nnut_host_secondary_armed: true\n",
        **(host_vars or {}),
    }.items():
        (inventory / "host_vars" / f"{host}.yml").write_text(text)
    return tmp_path


def _route(tmp_path, playbooks, **kw) -> dict:
    return setup_routing.routes(_tree(tmp_path, playbooks, **kw))


def test_an_unconditional_role_is_the_ticks_under_its_own_tag(tmp_path):
    out = _route(
        tmp_path, {INITIAL: [{"role": "docker_install", "tags": ["docker_install"]}]}
    )
    assert out["routes"]["docker_install"] == {
        "playbook": INITIAL,
        "tag": "docker_install",
        "on_tick_host": True,
        "host": None,
    }


def test_a_role_tagged_other_than_its_name_routes_by_the_tag(tmp_path):
    out = _route(tmp_path, {INITIAL: [{"role": "chezmoi_setup", "tags": ["chezmoi"]}]})
    assert out["routes"]["chezmoi_setup"]["tag"] == "chezmoi"


def test_a_bring_up_role_routes_to_its_playbook_and_not_the_tick(tmp_path):
    out = _route(tmp_path, {INITIAL: [], BRINGUP: [{"role": "k3s", "tags": ["k3s"]}]})
    assert out["routes"]["k3s"]["playbook"] == BRINGUP
    assert out["routes"]["k3s"]["on_tick_host"] is False


def test_initial_setup_wins_over_bootstrap(tmp_path):
    entry = {"role": "sops_setup", "tags": ["sops_setup"]}
    out = _route(tmp_path, {INITIAL: [entry], BOOTSTRAP: [entry]})
    assert out["routes"]["sops_setup"]["playbook"] == INITIAL


def test_a_role_no_playbook_lists_routes_to_no_playbook(tmp_path):
    out = _route(tmp_path, {INITIAL: []}, roles=["common"])
    assert out["routes"]["common"]["playbook"] is None


def test_a_role_gated_onto_another_host_names_that_host(tmp_path):
    gate = "inventory_hostname == optimize_pi_host"
    out = _route(
        tmp_path,
        {INITIAL: [{"role": "optimize_pi", "tags": ["optimize_pi"], "when": gate}]},
    )
    assert out["routes"]["optimize_pi"]["on_tick_host"] is False
    assert out["routes"]["optimize_pi"]["host"] == "daniel-pi"


def test_an_or_gate_admits_the_tick_host_through_its_second_arm(tmp_path):
    gate = "inventory_hostname == ups_host or nut_host_secondary_armed | bool"
    out = _route(
        tmp_path, {INITIAL: [{"role": "nut_host", "tags": ["nut_host"], "when": gate}]}
    )
    assert out["routes"]["nut_host"]["on_tick_host"] is True


@pytest.mark.parametrize(
    ("playbooks", "why"),
    [
        ({INITIAL: [{"role": "crony"}]}, "not one tag"),
        ({INITIAL: [{"role": "crony", "tags": ["a", "b"]}]}, "not one tag"),
        (
            {INITIAL: [{"role": "crony", "tags": ["crony"], "when": "no_such_var"}]},
            "gate cannot be read",
        ),
        (
            {
                INITIAL: [],
                BRINGUP: [{"role": "crony", "tags": ["crony"]}],
                BOOTSTRAP: [{"role": "crony", "tags": ["crony"]}],
            },
            "listed in",
        ),
    ],
    ids=["untagged", "two-tags", "undefined-gate-var", "two-other-playbooks"],
)
def test_a_role_it_cannot_place_is_unplaced_not_guessed(tmp_path, playbooks, why):
    out = _route(tmp_path, playbooks)
    assert "crony" not in out["routes"]
    assert why in out["unplaced"]["crony"]


def test_a_gate_var_still_holding_jinja_is_unplaced(tmp_path):
    """Compared as its unrendered text the gate would read False, a silent wrong answer."""
    gate = "inventory_hostname == crony_host"
    jinja = "crony_host: \"{{ groups['x'][0] }}\"\n"
    out = _route(
        tmp_path,
        {INITIAL: [{"role": "crony", "tags": ["crony"], "when": gate}]},
        host_vars={
            **dict.fromkeys(("daniel-server", "daniel-pi"), jinja),
            "daniel-box": "has_gitops: true\n" + jinja,
        },
    )
    assert "Jinja" in out["unplaced"]["crony"]


def test_the_live_tree_routes_the_shapes_the_hand_tables_held():
    """The members the removed tables named, so a rewrite that loses one fails by name."""
    out = setup_routing.routes(REPO)
    routes = out["routes"]
    assert out["unplaced"] == {}
    assert routes["chezmoi_setup"]["tag"] == "chezmoi"
    assert routes["k3s"]["playbook"] == BRINGUP
    assert routes["common"]["playbook"] is None
    assert routes["optimize_pi"]["host"] == "daniel-pi"
    assert routes["gitops_deploy"]["on_tick_host"] is True
    assert routes["sops_setup"]["playbook"] == INITIAL


def test_an_unreadable_ref_exits_1(capsys):
    assert setup_routing.main(["--ref", "0" * 40, "--host", "daniel-box"]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_routed_by_places_roles_for_the_has_gitops_host_and_restores(tmp_path):
    """A landing on daniel-server must still see a box-only role as the tick's to apply."""
    from deploy_changes import tick_applies_setup_role
    import deploy_setup_roles

    tree = _tree(
        tmp_path,
        {INITIAL: [{"role": "box_only", "tags": ["box_only"], "when": "has_gitops"}]},
    )
    saved = deploy_setup_roles.current_routing()
    with setup_routing.routed_by(tree):
        assert tick_applies_setup_role("box_only")
    assert deploy_setup_roles.current_routing() is saved
