#!/usr/bin/env python3
"""A repo file a setup role ships from the checkout reaches that role's hosts -- issue #2795.

WHAT WENT WRONG. Landing PR #2792 printed `hypervisor` also reaches daniel-pi and nothing
else, and the verdict read `needs-manual-apply`. daniel-server is the one host with
`has_hypervisor: true`, so it is the one host that runs `install.yml` -- and the PR's
`scripts/deploy_tools/staging_gate_remote.sh` was what that half installed as
`/usr/local/bin/staging-gate-run`. `remaining_setup_hosts_note` handed `setup_file_hosts`
only the paths under `ansible/roles/setup/hypervisor/`, so the changed script contributed
nothing and the union came from `tasks/teardown.yml` alone (`not has_hypervisor`: daniel-box
and daniel-pi). An operator following that line applies the teardown half to the Pi and
leaves the gate running the old script. The hand-run on daniel-server took its copy from 0
`ignore-submodules` matches to 3.

That gate was retired with the daniel-stage guest (#2941), so the shipped file these cases
drive is now `scripts/backup/etcd_restore_drill.sh` -- the other repo file this role installs
from the checkout, on the same one host, through the same path literal. PR #2792's file list
below carries that substitution and is otherwise verbatim.

The reject half is `scripts/dev/pytest_shard_weights.json`, from the same PR: no task ships
it, so it must contribute nothing. Without that half the fix would "pass" by widening every
changed role to every host it reaches, which is the failure `setup_repo_file_hosts` returns
the empty set on absent evidence to avoid.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_repo_shipped_files.py
"""

import pytest

import land_reach
import land_tags
from _land_fakes import MERGE_SHA
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome

from lib.repo_paths import REPO as REPO_ROOT

_ROLE = "hypervisor"
_SHIPPED = "scripts/backup/etcd_restore_drill.sh"
_NOT_SHIPPED = "scripts/dev/pytest_shard_weights.json"
_INSTALL = "ansible/roles/setup/hypervisor/tasks/etcd_drill.yml"
_PR_2792_SHA = "66ed500637abd09effe00c6bafde538c851914a9"
# PR #2792's file list (`gh pr view 2792 --json files`), with its two retired paths replaced:
# the shipped script by `_SHIPPED` above, and the deleted test by the module that replaced it.
_PR_2792_PATHS = [
    "ansible/roles/setup/hypervisor/CLAUDE.md",
    "ansible/roles/setup/hypervisor/tasks/teardown.yml",
    "ansible/tests/staging/test_staging_tick_arm_retired.py",
    _SHIPPED,
    _NOT_SHIPPED,
]


def test_the_out_of_tree_ship_site_still_exists():
    """Non-vacuity: a renamed script or reshaped `src:` must fail here, not read green.

    `setup_repo_file_hosts` finds its subject by matching the path literal in a task, so
    every case below returns the empty set -- and passes -- once that literal moves.
    """
    missing = [p for p in _PR_2792_PATHS if not (REPO_ROOT / p).exists()]
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


def test_a_repo_file_no_task_ships_reaches_no_host():
    """The reject half, and the one that keeps the accept half honest."""
    assert land_reach.setup_repo_file_hosts(_ROLE, _NOT_SHIPPED) == frozenset()


def test_another_role_does_not_claim_a_file_it_does_not_ship():
    """Evidence is per role: gitops_deploy ships no etcd restore drill."""
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


def test_the_note_survives_the_quiet_set_land_sh_passes():
    """`land_tags.py` calls this with a computed `quiet`, and `repo_files` filters through it.

    Every other case here leaves `quiet` empty, so a `quiet_paths` classification that
    dropped the shipped script would leave them green and the landing unfixed. The quiet set
    for PR #2792's own range is its `CLAUDE.md` alone.
    """
    quiet = land_tags.quiet_paths(_PR_2792_PATHS, f"{_PR_2792_SHA}~1..{_PR_2792_SHA}")
    assert quiet == {"ansible/roles/setup/hypervisor/CLAUDE.md"}, quiet
    note = land_reach.remaining_setup_hosts_note(
        _PR_2792_PATHS, "daniel-box", quiet=quiet
    )
    assert "daniel-server" in note, note


# --- Issue #2798: the PR that puts no role in the change set at all -------------------------


def test_a_repo_file_only_pr_names_the_installing_host():
    """The accept half of #2798: no path under `ansible/roles/setup/hypervisor/` at all.

    `cs.setup_roles` is built by path classification, so this PR leaves it empty and the
    self-applied loop has nothing to iterate. The role is reached through the file it ships.
    """
    note = land_reach.remaining_setup_hosts_note([_SHIPPED], "daniel-box")
    assert "daniel-server" in note, note
    assert "ships a changed repo file" in note, note


def test_a_repo_file_only_pr_of_a_file_no_task_ships_names_nobody():
    """The reject half: widening every setup role to its reach is the failure to avoid."""
    assert land_reach.remaining_setup_hosts_note([_NOT_SHIPPED], "daniel-box") == ""


def test_the_ticks_own_host_is_owed_when_the_role_entered_by_the_file_alone():
    """No `- {local_host}` here: with no role path in the PR the tick applied the role nowhere.

    Landing the same PR ON daniel-server must still name daniel-server, and with the plain
    local command rather than an ssh hop to the machine already running it.
    """
    note = land_reach.remaining_setup_hosts_note([_SHIPPED], "daniel-server")
    assert "daniel-server" in note, note
    assert "ssh daniel-server" not in note, note
    assert "`ansible-playbook ansible/initial_setup.yml --tags hypervisor`" in note, (
        note
    )


def test_a_repo_file_only_pr_ends_needs_manual_apply(landing):
    """The verify-by: the LANDING names it. The note alone was already computed before #2798.

    `no_tag_outcome` read `self_applied` and ended at `nothing-to-deploy` with
    `ln.remaining_setup` populated and never printed.
    """
    ln, _ = landing(None)
    ln.merge_sha = MERGE_SHA
    ln.plane = land_tags.plane_note([_SHIPPED])
    ln.self_applied = land_tags.self_applied([_SHIPPED])
    ln.self_applied_command = land_tags.self_applied_command([_SHIPPED])
    ln.remaining_setup = land_reach.remaining_setup_hosts_note([_SHIPPED], "daniel-box")
    assert (ln.plane, ln.self_applied) == ("", False), "the premise of #2798 moved"
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    assert (exc.value.verdict, exc.value.rc) == ("needs-manual-apply", 1)
