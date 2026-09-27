#!/usr/bin/env python3
"""A repo file a setup role ships from the checkout reaches that role's hosts -- issue #2795.

WHAT WENT WRONG. Landing PR #2792 printed `hypervisor` also reaches daniel-pi and nothing
else, and the verdict read `needs-manual-apply`. daniel-server is the one host with
`has_hypervisor: true`, so it is the one host that runs `install.yml` -- and the PR's
`scripts/deploy_tools/staging_gate_remote.sh` is what that half installs as
`/usr/local/bin/staging-gate-run`. `remaining_setup_hosts_note` handed `setup_file_hosts`
only the paths under `ansible/roles/setup/hypervisor/`, so the changed script contributed
nothing and the union came from `tasks/teardown.yml` alone (`not has_hypervisor`: daniel-box
and daniel-pi). An operator following that line applies the teardown half to the Pi and
leaves the gate running the old script. The hand-run on daniel-server took its copy from 0
`ignore-submodules` matches to 3.

The reject half is `scripts/dev/pytest_shard_weights.json`, from the same PR: no task ships
it, so it must contribute nothing. Without that half the fix would "pass" by widening every
changed role to every host it reaches, which is the failure `setup_repo_file_hosts` returns
the empty set on absent evidence to avoid.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_repo_shipped_files.py
"""

from pathlib import Path


import land_reach

REPO_ROOT = Path(__file__).resolve().parents[3]

_ROLE = "hypervisor"
_SHIPPED = "scripts/deploy_tools/staging_gate_remote.sh"
_ALSO_SHIPPED = "scripts/backup/etcd_restore_drill.sh"
_NOT_SHIPPED = "scripts/dev/pytest_shard_weights.json"
_INSTALL = "ansible/roles/setup/hypervisor/tasks/install.yml"
# PR #2792's file list, verbatim (`gh pr view 2792 --json files`).
_PR_2792_PATHS = [
    "ansible/roles/setup/hypervisor/CLAUDE.md",
    "ansible/roles/setup/hypervisor/tasks/teardown.yml",
    "ansible/tests/staging/test_staging_gate_ignores_submodule_gitlink.py",
    _SHIPPED,
    _NOT_SHIPPED,
]


def test_the_out_of_tree_ship_site_still_exists():
    """Non-vacuity: a renamed script or reshaped `src:` must fail here, not read green.

    `setup_repo_file_hosts` finds its subject by matching the path literal in a task, so
    every case below returns the empty set -- and passes -- once that literal moves.
    """
    missing = [
        p for p in (*_PR_2792_PATHS, _ALSO_SHIPPED) if not (REPO_ROOT / p).exists()
    ]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"
    install = (REPO_ROOT / _INSTALL).read_text()
    assert f'src: "{{{{ playbook_dir }}}}/../{_SHIPPED}"' in install, (
        f"{_INSTALL} no longer ships {_SHIPPED} by that path literal"
    )


def test_a_shipped_repo_file_reaches_the_installing_host():
    """The accept half: only daniel-server runs `install.yml`, so only it owes the apply."""
    assert land_reach.setup_repo_file_hosts(_ROLE, _SHIPPED) == frozenset(
        {"daniel-server"}
    )
    assert land_reach.setup_repo_file_hosts(_ROLE, _ALSO_SHIPPED) == frozenset(
        {"daniel-server"}
    )


def test_a_repo_file_no_task_ships_reaches_no_host():
    """The reject half, and the one that keeps the accept half honest."""
    assert land_reach.setup_repo_file_hosts(_ROLE, _NOT_SHIPPED) == frozenset()


def test_another_role_does_not_claim_a_file_it_does_not_ship():
    """Evidence is per role: gitops_deploy ships no staging-gate runner."""
    assert land_reach.setup_repo_file_hosts("gitops_deploy", _SHIPPED) == frozenset()


def test_the_note_for_pr_2792_names_both_owed_hosts():
    """daniel-server for the runner it installs, daniel-pi for the teardown half it changed."""
    note = land_reach.remaining_setup_hosts_note(_PR_2792_PATHS, "daniel-box")
    assert "daniel-server" in note, note
    assert "daniel-pi" in note, note
    assert (
        'ssh daniel-server "cd /home/ubuntu/server && git pull --ff-only && '
        'ansible-playbook ansible/initial_setup.yml --tags hypervisor"' in note
    ), note


def test_the_note_drops_daniel_server_without_the_shipped_script():
    """The same PR minus the one file that lands there owes daniel-server nothing."""
    note = land_reach.remaining_setup_hosts_note(
        [p for p in _PR_2792_PATHS if p != _SHIPPED], "daniel-box"
    )
    assert "daniel-server" not in note, note
    assert "daniel-pi" in note, note
