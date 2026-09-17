#!/usr/bin/env python3
"""A `block: ... when:` at the top of a role's main.yml narrows the files it ships -- issue #1904.

WHAT WENT WRONG. Landing PR #1901 (merge fdd19ea7) ended `needs-manual-apply`, prescribing
`initial_setup.yml --tags deploy_ui` on daniel-server and daniel-pi. Nothing there needed
applying: deploy_ui's main.yml is one `block:` under `when: has_gitops`, true on daniel-box
alone. The issue read the cause as `_gates_in` following an `include_tasks` gate but not a
block-level `when:`. It was not: `_gates_in` recursed into `block:` bodies with the block's
`when:` inherited since the shipping-task gate shipped (2ec4d882), and `files/deploy_ui.py`
read as daniel-box only on the land_reach that landed #1901. What widened the note was the
PR's three `tests/*.py` beside them, which fell through to the role-level reach -- the same
fall-through issue #1885 named, fixed in 9cd36ae0, which merged minutes before #1901 and
after the landing session's copy of land_reach was loaded. Measured on this tree: the
9cd36ae0^ module over #1901's file list prints the bad note; the current one prints ''.

So this file pins the block shape rather than changes it, and holds the two halves apart:
the accept half is deploy_ui's own files (box only, through the block gate) and the note
over #1901's whole file list (empty); the reject half is an ungated shipping task in a role
with no playbook gate, which must keep reaching every host.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_block_gate.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import land_reach
from lib import yaml_fast

REPO_ROOT = Path(__file__).resolve().parents[3]

_ROLE = "deploy_ui"
_MAIN = "ansible/roles/setup/deploy_ui/tasks/main.yml"
_SHIPPED = (
    "ansible/roles/setup/deploy_ui/files/deploy_ui.py",
    "ansible/roles/setup/deploy_ui/files/deploy_ui_reads.py",
    "ansible/roles/setup/deploy_ui/files/deploy_ui.html",
    "ansible/roles/setup/deploy_ui/templates/deploy-ui.service.j2",
)
# An ungated `copy` under a role initial_setup.yml applies everywhere: PR #1002's file, the
# one issue #1009 was filed over. If this ever narrows, the narrowing lost its evidence.
_UNGATED = "ansible/roles/setup/initial_setup/files/kuma-push-lib.sh"
# PR #1901's file list, verbatim (`git show --name-only fdd19ea7`).
_PR_1901_PATHS = [
    "ansible/roles/setup/deploy_ui/CLAUDE.md",
    "ansible/roles/setup/deploy_ui/files/deploy_ui.html",
    "ansible/roles/setup/deploy_ui/files/deploy_ui.py",
    "ansible/roles/setup/deploy_ui/files/deploy_ui_reads.py",
    "ansible/roles/setup/deploy_ui/files/deploy_ui_writes.py",
    "ansible/roles/setup/deploy_ui/templates/deploy-ui.service.j2",
    "ansible/roles/setup/deploy_ui/tests/test_deploy_ui_app.py",
    "ansible/roles/setup/deploy_ui/tests/test_deploy_ui_reads.py",
    "ansible/roles/setup/deploy_ui/tests/test_deploy_ui_writes.py",
    "ansible/tests/deploy/test_inventory_block_scalars_have_no_comment_shaped_lines.py",
    "docs/deploying.md",
    "scripts/deploy_tools/narrow_broad.py",
]


def test_the_paths_under_test_still_exist():
    """Non-vacuity: every case below reads green over a renamed file or role."""
    named = (_MAIN, _UNGATED, *_SHIPPED, *_PR_1901_PATHS)
    missing = [p for p in named if not (REPO_ROOT / p).exists()]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"


def test_deploy_ui_is_still_one_block_gated_on_has_gitops():
    """The shape this file pins: every top-level task is a `block:` carrying `when:
    has_gitops`, and the role itself has no playbook gate (so the block is the ONLY thing
    keeping its files off the other hosts)."""
    tasks = yaml_fast.safe_load((REPO_ROOT / _MAIN).read_text())
    assert tasks and all(
        "block" in t and t.get("when") == "has_gitops" for t in tasks
    ), [t.get("name") for t in tasks]
    assert land_reach.setup_role_hosts(_ROLE) == frozenset(land_reach._HOSTS)


def test_a_file_shipped_inside_the_gated_block_reaches_the_gitops_host_only():
    """The accept half: the block's `when:` is inherited by the tasks inside it, the copy
    loop and the template alike."""
    for path in _SHIPPED:
        assert land_reach.setup_file_hosts(_ROLE, path) == frozenset({"daniel-box"}), (
            path
        )


def test_an_ungated_shipping_task_still_reaches_every_host():
    """The reject half, so a later narrowing of `_gates_in` cannot go quiet: no block, no
    when, no playbook gate -- all three hosts."""
    assert land_reach.setup_file_hosts("initial_setup", _UNGATED) == frozenset(
        land_reach._HOSTS
    )


def test_pr_1901_owes_no_host_beyond_the_tick():
    """The verdict PR #1901 should have read, and reads on this tree: `settled`."""
    assert land_reach.remaining_setup_hosts_note(_PR_1901_PATHS, "daniel-box") == ""


def test_pr_1901_from_another_host_still_names_the_gitops_host():
    """Per file, not a blanket silence: the same files DO reach daniel-box."""
    note = land_reach.remaining_setup_hosts_note(_PR_1901_PATHS, "daniel-server")
    assert "daniel-box" in note
    assert "daniel-pi" not in note
