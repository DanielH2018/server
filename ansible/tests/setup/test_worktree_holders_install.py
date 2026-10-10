"""The worktree sweep's root helper is installed where the sudoers rule and the caller look.

`lib.worktrees.privileged_holders` checks execute permission on `WORKTREE_HOLDERS` before it
asks sudo, and the sudoers rule names the same path. If the three drift apart, the sweep
either falls back to the unprivileged scan without saying why, or sudo refuses it every run.
The agent user's grant (#4021) adds a fourth path, the root map both the helper and the
caller read, and a second way to execute the helper, an ACL entry.

Run: uv run pytest ansible/tests/setup/test_worktree_holders_install.py
"""

import json
import re
from pathlib import PurePosixPath

import pytest
from _helpers import SETUP_ROLES, load_tasks, render_expr, task_named
from lib.worktrees import WORKTREE_HOLDER_ROOTS, WORKTREE_HOLDERS

CRONS = SETUP_ROLES / "initial_setup" / "tasks" / "crons.yml"
RULE = "Allow sys_user and each agent user to run the worktree holder scan"
ROOT_MAP = "Map each user of the worktree holder scan"
USERS = "List the worktree holder scan's users"
ACL = "Let each agent user execute the worktree holder scan"


def _sudoers_rules(content: str) -> list[tuple[str, str]]:
    """(user, command) for every rule line. Each line must be an argument-free NOPASSWD rule."""
    rules = []
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"(\S+) ALL=\(root\) NOPASSWD: (\S+) \"\"", line.strip())
        assert match, f"not an argument-free NOPASSWD rule: {line!r}"
        rules.append((match.group(1), match.group(2)))
    return rules


def _rendered_rule(tasks: list[dict], users: list[str]) -> str:
    rule = task_named(tasks, RULE)
    return render_expr(
        rule["ansible.builtin.copy"]["content"],
        initial_setup_worktree_holders_users=users,
    )


def test_install_dest_sudoers_rule_and_caller_name_one_path_is_clean():
    tasks = load_tasks(CRONS)
    install = task_named(tasks, "Install the root-run worktree holder scan")

    assert install["ansible.builtin.copy"]["dest"] == WORKTREE_HOLDERS
    assert {cmd for _, cmd in _sudoers_rules(_rendered_rule(tasks, ["ubuntu"]))} == {
        WORKTREE_HOLDERS
    }


def test_every_granted_user_gets_one_argument_free_rule_is_clean():
    # #4021: the agent user gets the same grant as sys_user, one line each.
    rules = _sudoers_rules(_rendered_rule(load_tasks(CRONS), ["ubuntu", "claude"]))

    assert rules == [("ubuntu", WORKTREE_HOLDERS), ("claude", WORKTREE_HOLDERS)]


def test_a_rule_that_accepts_arguments_is_flagged():
    # Without `""`, sudo lets the caller pass any argument, and a later helper that read one
    # would let the caller ask root about any directory. The second line is checked too.
    with pytest.raises(AssertionError, match="not an argument-free NOPASSWD rule"):
        _sudoers_rules(
            f'ubuntu ALL=(root) NOPASSWD: {WORKTREE_HOLDERS} ""\n'
            f"claude ALL=(root) NOPASSWD: {WORKTREE_HOLDERS}\n"
        )


def _parent_created_before_install(tasks: list[dict], dest: str) -> bool:
    parent = str(PurePosixPath(dest).parent)
    for task in tasks:
        if task.get("ansible.builtin.copy", {}).get("dest") == dest:
            return False
        made = task.get("ansible.builtin.file", {})
        if made.get("path") == parent and made.get("state") == "directory":
            return True
    raise AssertionError(f"no task installs {dest}")


def test_the_install_directory_is_created_before_the_copy_is_clean():
    # `copy` does not create a missing parent, and Ubuntu ships no /usr/local/libexec. The first
    # apply failed on daniel-box for exactly that and held the initial_setup plane.
    assert _parent_created_before_install(load_tasks(CRONS), WORKTREE_HOLDERS)


def test_an_install_with_no_directory_task_before_it_is_flagged():
    install = {"ansible.builtin.copy": {"dest": WORKTREE_HOLDERS}}
    late_dir = {
        "ansible.builtin.file": {
            "path": str(PurePosixPath(WORKTREE_HOLDERS).parent),
            "state": "directory",
        }
    }
    assert not _parent_created_before_install([install, late_dir], WORKTREE_HOLDERS)


def _rendered_roots(tasks: list[dict], users: list[str], checkouts: list[str]):
    task = task_named(tasks, ROOT_MAP)
    text = render_expr(
        task["ansible.builtin.copy"]["content"],
        initial_setup_worktree_holders_users=users,
        initial_setup_worktree_holders_checkouts=checkouts,
    )
    return json.loads(text) if isinstance(text, str) else text


def test_the_root_map_sends_each_user_to_its_own_checkouts_worktrees_is_clean():
    # The helper and lib.worktrees.holder_root both read this file, so it sits where they look,
    # and only root may write it.
    tasks = load_tasks(CRONS)
    copy = task_named(tasks, ROOT_MAP)["ansible.builtin.copy"]

    assert (copy["dest"], copy["owner"]) == (WORKTREE_HOLDER_ROOTS, "root")
    assert _rendered_roots(
        tasks, ["ubuntu", "claude"], ["/home/ubuntu/server", "/var/lib/claude/server"]
    ) == {
        "ubuntu": "/home/ubuntu/server/.claude/worktrees",
        "claude": "/var/lib/claude/server/.claude/worktrees",
    }


def _checkouts_come_from_role_variables(tasks: list[dict]) -> bool:
    """The operator's checkout and each agent's clone are claude_code's own variables."""
    agents = task_named(tasks, "Name the agent user granted")[
        "ansible.builtin.set_fact"
    ]
    checkouts = task_named(tasks, USERS)["ansible.builtin.set_fact"][
        "initial_setup_worktree_holders_checkouts"
    ]
    return (
        "claude_code_agent_user_enabled"
        in agents["initial_setup_worktree_holders_agents"]
        and "claude_code_operator_checkout" in checkouts
        and "claude_code_agent_user_clone_dir" in checkouts
        and "/" not in checkouts
    )


def test_the_checkouts_come_from_role_variables_is_clean():
    assert _checkouts_come_from_role_variables(load_tasks(CRONS))


def test_a_literal_checkout_path_is_flagged():
    tasks = load_tasks(CRONS)
    users = task_named(tasks, USERS)
    literal = {
        **users,
        "ansible.builtin.set_fact": {
            **users["ansible.builtin.set_fact"],
            "initial_setup_worktree_holders_checkouts": "{{ ['/home/ubuntu/server'] + "
            "([claude_code_agent_user_clone_dir] if initial_setup_worktree_holders_agents else []) }}",
        },
    }
    with_literal = [literal if task is users else task for task in tasks]

    assert not _checkouts_come_from_role_variables(with_literal)


def _grants_follow_one_list(tasks: list[dict]) -> bool:
    """The ACL loops over the agents the sudoers users come from, after both copies."""
    names = [task.get("name", "") for task in tasks]
    agents = "initial_setup_worktree_holders_agents"
    order = [
        names.index(task_named(tasks, fragment)["name"])
        for fragment in ("Install the root-run worktree holder scan", RULE, ACL)
    ]
    return (
        task_named(tasks, ACL).get("loop") == "{{ " + agents + " }}"
        and agents
        in task_named(tasks, USERS)["ansible.builtin.set_fact"][
            "initial_setup_worktree_holders_users"
        ]
        and order == sorted(order)
    )


def test_the_acl_and_the_sudoers_rule_grant_the_same_agents_is_clean():
    # The caller reads execute permission as "the sudoers rule is mine". An agent with one and
    # not the other either falls back silently or makes sudo mail an incident. The ACL comes
    # after the copy, because a copy that changes the helper replaces the inode and drops it.
    assert _grants_follow_one_list(load_tasks(CRONS))


def test_an_acl_over_another_list_or_before_the_copy_is_flagged():
    tasks = load_tasks(CRONS)
    acl = task_named(tasks, ACL)
    other_list = [
        {**task, "loop": "{{ some_other_agents }}"} if task is acl else task
        for task in tasks
    ]
    acl_first = [acl] + [task for task in tasks if task is not acl]

    assert not _grants_follow_one_list(other_list)
    assert not _grants_follow_one_list(acl_first)
