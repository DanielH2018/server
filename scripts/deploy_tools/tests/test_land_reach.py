#!/usr/bin/env python3
"""The host reach of a setup-role change: `land_reach.py`.

Every narrowing here has a reject half, because the failure this guards is a note that
falls silent for a host still running the old file (issue #1009) -- and the accept half
guards the inverse, a note that sends an operator to hosts that never install it (#1254).

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach.py
"""

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import yaml_fast

import land_reach


@pytest.fixture
def _synthetic_setup_inventory(tmp_path):
    """A synthetic initial_setup.yml + group_vars/host_vars.

    Pins the derivation tests below the way this file's own module docstring already
    requires for `_DECLARED` ("A fixture, not live inventory... reading containers_list
    would make them fail whenever a service is retired"). `setup_role_hosts` had no
    injection point for that until now; this mirrors `deploy_tags.py`'s own
    `host_vars: Path = HOST_VARS` pattern.
    """
    playbook = tmp_path / "initial_setup.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "x",
                    "roles": [
                        {"role": "config_files"},  # no when: -> every host
                        {"role": "gitops_deploy", "when": "has_gitops"},
                        {
                            "role": "nut_host",
                            "when": "inventory_hostname == ups_host or "
                            "nut_host_secondary_armed | bool",
                        },
                        {
                            "role": "optimize_pi",
                            "when": "inventory_hostname == 'daniel-pi'",
                        },
                    ],
                }
            ]
        )
    )
    all_vars = tmp_path / "all.yml"
    all_vars.write_text(
        yaml.safe_dump(
            {
                "has_gitops": False,
                "ups_host": "daniel-server",
                "nut_host_secondary_armed": False,
            }
        )
    )
    host_vars_dir = tmp_path / "host_vars"
    host_vars_dir.mkdir()
    (host_vars_dir / "daniel-box.yml").write_text(
        yaml.safe_dump({"has_gitops": True, "nut_host_secondary_armed": True})
    )
    return playbook, all_vars, host_vars_dir


def test_setup_role_hosts_reads_the_when_gate_per_host(_synthetic_setup_inventory):
    """The derivation itself, pinned against a synthetic tree rather than live vars that can
    be armed/disarmed (nut_host_secondary_armed, has_gitops) for reasons unrelated to this
    logic."""
    playbook, all_vars, host_vars_dir = _synthetic_setup_inventory
    assert land_reach.setup_role_hosts(
        "config_files", playbook, all_vars, host_vars_dir
    ) == frozenset({"daniel-box", "daniel-server", "daniel-pi"})
    assert land_reach.setup_role_hosts(
        "gitops_deploy", playbook, all_vars, host_vars_dir
    ) == frozenset({"daniel-box"})
    # nut_host: `inventory_hostname == ups_host or nut_host_secondary_armed | bool` --
    # daniel-server matches the first clause, daniel-box the second (armed there), daniel-pi
    # neither.
    assert land_reach.setup_role_hosts(
        "nut_host", playbook, all_vars, host_vars_dir
    ) == frozenset({"daniel-box", "daniel-server"})
    assert land_reach.setup_role_hosts(
        "optimize_pi", playbook, all_vars, host_vars_dir
    ) == frozenset({"daniel-pi"})


def test_setup_role_hosts_unknown_role_is_empty(_synthetic_setup_inventory):
    """The reject half: a role not in the playbook's own `roles:` list is `plane_note`'s
    `unroutable` territory, not this function's to guess at."""
    playbook, all_vars, host_vars_dir = _synthetic_setup_inventory
    assert (
        land_reach.setup_role_hosts(
            "not_a_real_role", playbook, all_vars, host_vars_dir
        )
        == frozenset()
    )


def test_eval_when_does_not_crash_on_a_list_or_a_bool():
    """`when:` as a YAML list is Ansible's implicit AND, and `when: true` is a bare bool --
    both are legal shapes `initial_setup.yml` could carry tomorrow even though no role uses
    them today. Neither may raise past the documented fail-open contract.

    The accept half: a list whose clauses are all true evaluates true. The reject half: one
    false clause makes the whole list false -- proving this isn't just swallowing every
    input into True.
    """
    assert land_reach._eval_when(["1 == 1", "2 == 2"], "daniel-box") is True
    assert land_reach._eval_when(["1 == 1", "1 == 2"], "daniel-box") is False
    # A non-string, non-list value (e.g. a bare YAML bool) cannot be evaluated at all, so it
    # fails open rather than raising -- true or false, both read as "reached".
    assert land_reach._eval_when(True, "daniel-box") is True
    assert land_reach._eval_when(False, "daniel-box") is True


def test_setup_role_hosts_census_is_not_vacuous():
    """The parse of initial_setup.yml's `roles:` list must find a known set of names, AND
    the parse's discriminating half -- which roles carry a real `when:` -- must be non-empty
    too.

    A vacuous parse (an empty dict, a wrong key) would make every remaining-hosts note
    silently empty -- the same shape nine guards broke across PRs #838/#846/#852/#858 and the
    monitor-bridge move, caught only by an assertion like this one. A parse that found every
    NAME but read every `when:` as None would be a subtler version of the same failure:
    `setup_role_hosts` would then reach ALL THREE hosts for every role, silently re-breaking
    #723 with every test above still green (they use a synthetic tree, not this parse).
    """
    roles = land_reach._initial_setup_roles()
    assert {
        "config_files",
        "initial_setup",
        "sops_setup",
        "docker_install",
        "hypervisor",
        "gitops_deploy",
        "github_cli",
        "chezmoi_setup",
        "claude_code",
        "renovate_notify",
        "renovate_agent",
        "nut_host",
        "optimize_pi",
        "fake_remux",
    } <= set(roles)
    assert roles["initial_setup"] is None
    assert roles["gitops_deploy"]  # a real `when:` gate, not None


def test_remaining_setup_hosts_note_flags_pr_1002():
    """PR #1002's real shape: `initial_setup` reaches all three hosts, the tick converges on
    daniel-box alone, and the other two are owed a hand-run (issue #1009)."""
    files = ["ansible/roles/setup/initial_setup/files/kuma-push-lib.sh"]
    note = land_reach.remaining_setup_hosts_note(files, "daniel-box")
    assert "daniel-server" in note
    assert "daniel-pi" in note
    assert "initial_setup" in note


def test_remaining_setup_hosts_note_stays_empty_for_pr_723():
    """The reject half. gitops_deploy and renovate_notify both reach daniel-box alone, so
    flagging them here would re-break the fix #723 exists to protect -- `plane_note`'s own
    docstring records land.sh exiting 1 with `needs-manual-apply` for this same file list
    while the very next tick applied it (2026-09-01)."""
    files = [
        "ansible/roles/setup/gitops_deploy/files/deploy_logic.py",
        "ansible/roles/setup/renovate_notify/files/renovate_notify.py",
    ]
    assert land_reach.remaining_setup_hosts_note(files, "daniel-box") == ""


def test_remaining_setup_hosts_note_is_relative_to_the_given_host():
    """A role reaching only the host asked about has nothing remaining, whichever host that
    is -- the note is relative to `local_host`, not hardcoded to daniel-box."""
    files = ["ansible/roles/setup/optimize_pi/tasks/main.yml"]
    assert land_reach.remaining_setup_hosts_note(files, "daniel-pi") == ""
    assert "daniel-pi" in land_reach.remaining_setup_hosts_note(files, "daniel-box")


def test_an_unroutable_setup_role_has_no_remaining_hosts():
    """`common` reaches no host through initial_setup.yml at all -- that's `plane_note`'s
    `unroutable` case, not this function's to guess at."""
    files = ["ansible/roles/setup/common/templates/resolv.conf.j2"]
    assert land_reach.remaining_setup_hosts_note(files, "daniel-box") == ""


@pytest.fixture
def _synthetic_setup_role_tree(_synthetic_setup_inventory, tmp_path):
    """`config_files` (no role gate) with tasks that ship three files under three gates:
    one box-only task, one whose gate sits on the `import_tasks` above it, one ungated."""
    playbook, all_vars, host_vars_dir = _synthetic_setup_inventory
    roles_dir = tmp_path / "roles"
    tasks = roles_dir / "config_files" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        yaml.safe_dump(
            [
                {"name": "crons", "ansible.builtin.import_tasks": "crons.yml"},
                {
                    "name": "box tools",
                    "ansible.builtin.import_tasks": "box.yml",
                    "when": "has_gitops",
                },
            ]
        )
    )
    (tasks / "crons.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Install the docs refresh script (box only)",
                    "when": "inventory_hostname == 'daniel-box'",
                    "ansible.builtin.template": {
                        "src": "docs-refresh.sh.j2",
                        "dest": "/usr/local/bin/docs-refresh.sh",
                    },
                },
                {
                    "name": "Install the shared push library",
                    "ansible.builtin.copy": {
                        "src": "kuma-push-lib.sh",
                        "dest": "/usr/local/lib/kuma-push-lib.sh",
                    },
                },
            ]
        )
    )
    (tasks / "box.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Install the looped scripts",
                    "ansible.builtin.copy": {"src": "{{ item }}", "dest": "/opt/"},
                    "loop": ["reap.py", "drill.py"],
                }
            ]
        )
    )
    return playbook, all_vars, host_vars_dir, roles_dir


def test_setup_file_hosts_reads_the_gate_on_the_task_that_ships_the_file(
    _synthetic_setup_role_tree,
):
    """The role has no gate, so `setup_role_hosts` says every host. The task that ships the
    template is box-only, and that gate is the one that decides where the FILE lands."""
    playbook, all_vars, host_vars_dir, roles_dir = _synthetic_setup_role_tree
    kw = dict(
        playbook=playbook,
        all_vars=all_vars,
        host_vars_dir=host_vars_dir,
        roles_dir=roles_dir,
    )
    assert land_reach.setup_file_hosts(
        "config_files",
        "ansible/roles/setup/config_files/templates/docs-refresh.sh.j2",
        **kw,
    ) == frozenset({"daniel-box"})
    # The accept half: an ungated task reaches wherever the role does (the #1009 shape).
    assert land_reach.setup_file_hosts(
        "config_files", "ansible/roles/setup/config_files/files/kuma-push-lib.sh", **kw
    ) == frozenset({"daniel-box", "daniel-server", "daniel-pi"})


def test_setup_file_hosts_inherits_the_gate_on_the_import_and_reads_a_loop(
    _synthetic_setup_role_tree,
):
    """A `when:` on `import_tasks` applies to every imported task, and a file named only in
    a `loop:` behind `src: "{{ item }}"` is still found."""
    playbook, all_vars, host_vars_dir, roles_dir = _synthetic_setup_role_tree
    assert land_reach.setup_file_hosts(
        "config_files",
        "ansible/roles/setup/config_files/files/reap.py",
        playbook=playbook,
        all_vars=all_vars,
        host_vars_dir=host_vars_dir,
        roles_dir=roles_dir,
    ) == frozenset({"daniel-box"})


def test_setup_file_hosts_falls_back_to_the_role_when_no_task_names_the_file(
    _synthetic_setup_role_tree,
):
    """The reject half of the narrowing: a file no task references, or a path that is not
    a shipped file at all (a tasks file), keeps the role-level answer. Narrower than the
    truth is the failure `_eval_when`'s docstring names; unknown must stay wide."""
    playbook, all_vars, host_vars_dir, roles_dir = _synthetic_setup_role_tree
    kw = dict(
        playbook=playbook,
        all_vars=all_vars,
        host_vars_dir=host_vars_dir,
        roles_dir=roles_dir,
    )
    everywhere = frozenset({"daniel-box", "daniel-server", "daniel-pi"})
    assert (
        land_reach.setup_file_hosts(
            "config_files", "ansible/roles/setup/config_files/files/orphan.sh", **kw
        )
        == everywhere
    )
    assert (
        land_reach.setup_file_hosts(
            "config_files", "ansible/roles/setup/config_files/tasks/crons.yml", **kw
        )
        == everywhere
    )


def test_remaining_setup_hosts_note_stays_empty_for_pr_1241():
    """PR #1241's real shape, against this repo's own tree: two `initial_setup` cron
    templates that `tasks/crons.yml` installs on daniel-box alone. The role-level reach
    said all three hosts and the landing exited 1 with `needs-manual-apply`, prescribing
    two playbook runs that change nothing -- 7 of the 18 such verdicts in the two days to
    2026-09-05 had this shape."""
    files = [
        "ansible/roles/setup/initial_setup/templates/docs-refresh.sh.j2",
        "ansible/roles/setup/initial_setup/templates/eval-run.sh.j2",
    ]
    assert land_reach.remaining_setup_hosts_note(files, "daniel-box") == ""


def test_setup_file_hosts_reads_a_role_markdown_as_shipped_nowhere(
    _synthetic_setup_role_tree,
):
    """The role CLAUDE.md is a doc: no setup task names a `.md`, so it reaches no host.
    Guarded against the real tree too, so a task that starts shipping one fails here."""
    playbook, all_vars, host_vars_dir, roles_dir = _synthetic_setup_role_tree
    assert (
        land_reach.setup_file_hosts(
            "config_files",
            "ansible/roles/setup/config_files/CLAUDE.md",
            playbook=playbook,
            all_vars=all_vars,
            host_vars_dir=host_vars_dir,
            roles_dir=roles_dir,
        )
        == frozenset()
    )
    real_tasks = (land_reach._SETUP_ROLES_DIR).glob("*/tasks/*.yml")
    shipping_md = [
        t
        for t in real_tasks
        if re.search(r"\.md\"", json.dumps(yaml_fast.safe_load(t.read_text())))
    ]
    assert shipping_md == [], "a setup task now names a .md file; drop the docs rule"
