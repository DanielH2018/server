#!/usr/bin/env python3
"""The agent user's memory store is seeded from the operator's once, with repo-relative links.

`roles/setup/claude_code/tasks/agent_memory_seed.yml` runs when `claude-rc.service` moves to the
agent (slice 4d of docs/claude-agent-user.md). Three legs decide whether the move keeps the
operator's memories or quietly loses them:

1. The guard. The copy must run only while the agent's own MEMORY.md is absent. A leg that runs
   on every apply replaces what the agent wrote with the operator's older copy.
2. The link rewrite. The operator's notes link into `/home/ubuntu/server/...`, a path the
   agent's sessions cannot read. The prefix must go from every copied note and nothing else.
3. The copy itself: owner, group and modes the operator's read depends on, and a source left
   exactly as it was.

None of the three fails a deploy when it slips; the agent just starts with the wrong memory.

Run: uv run pytest ansible/tests/setup/test_claude_agent_memory_seed.py
"""

import grp
import os
import pwd
import re
import shutil
from pathlib import Path

from _helpers import ANSIBLE
from _setup_render import role_context
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from lib import yaml_fast
from lib.proc_testing import run

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
SEED = ROLE / "tasks" / "agent_memory_seed.yml"
BLOCK = "Copy the operator's memory store into the agent's, once"


def evaluate(expression, variables: dict):
    """An Ansible expression evaluated by Ansible's own Templar, so its filters are real."""
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(expression))


def holds(conditions: list[str], variables: dict) -> bool:
    return all(evaluate("{{ " + c + " }}", variables) for c in conditions)


def seed_tasks() -> list[dict]:
    return yaml_fast.safe_load(SEED.read_text())


def named(task_list: list[dict], name: str) -> dict:
    found = [t for t in task_list if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, found {len(found)}"
    return found[0]


def block_tasks() -> list[dict]:
    return named(seed_tasks(), BLOCK)["block"]


def stats(agent_has_index: bool, operator_has_index: bool) -> dict:
    return {
        "claude_code_agent_memory_index": {"stat": {"exists": agent_has_index}},
        "claude_code_operator_memory_index": {"stat": {"exists": operator_has_index}},
    }


def test_the_copy_runs_only_while_the_agent_has_no_memory_index() -> None:
    conditions = named(seed_tasks(), BLOCK)["when"]
    assert holds(conditions, stats(agent_has_index=False, operator_has_index=True))
    assert not holds(
        conditions, stats(agent_has_index=True, operator_has_index=True)
    ), "a later apply must never copy over the agent's own memories"
    assert not holds(
        conditions, stats(agent_has_index=False, operator_has_index=False)
    ), "without an operator index there is nothing to seed from"


def test_the_guard_reads_the_agent_index_in_the_agent_memory_dir() -> None:
    stat = named(seed_tasks(), "Look for the agent's memory index")
    ctx = role_context(ROLE, {})
    path = evaluate(stat["ansible.builtin.stat"]["path"], ctx)
    assert path == f"{ctx['claude_code_agent_memory_dir']}/MEMORY.md"
    assert path == (
        "/var/lib/claude/.claude/projects/-var-lib-claude-server/memory/MEMORY.md"
    )


def test_the_import_runs_only_for_an_enabled_agent_that_is_the_hosts_user() -> None:
    task = named(
        yaml_fast.safe_load((ROLE / "tasks" / "agent.yml").read_text()),
        "Copy the operator's memory store into the agent's, the first time the agent is the host's user",
    )
    assert task["ansible.builtin.import_tasks"] == "agent_memory_seed.yml"
    base = role_context(ROLE, {"claude_code_agent_user_enabled": True})
    assert not holds(task["when"], base), "the default user is the operator"
    agent = base | {"claude_code_user": "claude"}
    assert holds(task["when"], agent)
    assert not holds(task["when"], agent | {"claude_code_agent_user_enabled": False})
    assert not holds(task["when"], agent | {"claude_code_agent_memory_seed": False})


def test_the_rewrite_strips_the_operator_checkout_from_links_and_nothing_else() -> None:
    task = named(block_tasks(), "Make the copied memory notes' links repo-relative")
    args = task["ansible.builtin.replace"]
    ctx = role_context(ROLE, {})
    regexp = evaluate(args["regexp"], ctx)
    replace = evaluate(args["replace"], ctx)
    note = "\n".join(
        [
            "- [Deploy notes](/home/ubuntu/server/docs/deploying.md) and the "
            "[ADR](/home/ubuntu/server/docs/adr/0012-x.md).",
            "- [Sibling note](deploy-time.md) stays as written.",
            "- [Skill](/home/ubuntu/server/.claude/skills/worktree-cleanup/SKILL.md)",
            "- Another checkout: /home/ubuntu/server-old/docs/y.md",
            "- No trailing slash: /home/ubuntu/server",
            "- Operator store: /home/ubuntu/.claude/projects/-home-ubuntu-server/memory",
            "- The agent's clone: /var/lib/claude/server/docs/y.md",
        ]
    )
    rewritten = re.sub(regexp, replace, note, flags=re.MULTILINE)
    assert rewritten == "\n".join(
        [
            "- [Deploy notes](docs/deploying.md) and the [ADR](docs/adr/0012-x.md).",
            "- [Sibling note](deploy-time.md) stays as written.",
            "- [Skill](.claude/skills/worktree-cleanup/SKILL.md)",
            "- Another checkout: /home/ubuntu/server-old/docs/y.md",
            "- No trailing slash: /home/ubuntu/server",
            "- Operator store: /home/ubuntu/.claude/projects/-home-ubuntu-server/memory",
            "- The agent's clone: /var/lib/claude/server/docs/y.md",
        ]
    )
    moved = role_context(ROLE, {"claude_code_operator_checkout": "/srv/x"})
    assert evaluate(args["regexp"], moved) == re.escape("/srv/x/"), (
        "the prefix must follow the operator checkout variable"
    )


def test_the_rewrite_covers_every_note_the_find_returns_and_keeps_the_modes() -> None:
    tasks = block_tasks()
    find = named(tasks, "List the copied memory notes")["ansible.builtin.find"]
    assert find["patterns"] == "*.md" and find["recurse"] is True
    replace = named(tasks, "Make the copied memory notes' links repo-relative")
    assert replace["loop"] == "{{ claude_code_agent_memory_notes.files }}"
    args = replace["ansible.builtin.replace"]
    assert (args["owner"], args["group"], args["mode"]) == (
        "{{ claude_code_agent_user }}",
        "{{ sys_user }}",
        "0640",
    )
    assert replace["diff"] is False, "--diff would print the operator's notes"


def test_a_failed_copy_reopens_the_guard() -> None:
    rescue = named(seed_tasks(), BLOCK)["rescue"]
    removal = named(rescue, "Reopen the copy after a failure")["ansible.builtin.file"]
    assert removal["state"] == "absent"
    assert removal["path"] == "{{ claude_code_agent_memory_dir }}/MEMORY.md"
    assert any("ansible.builtin.fail" in t for t in rescue), (
        "the apply must still fail after the guard is reopened"
    )


def _copy_argv(src: Path, dst: Path, check_mode: bool) -> list[str]:
    """The task's rsync argv, with the agent and group set to the user running the test."""
    task = named(block_tasks(), "Copy the operator's memory files")
    group = grp.getgrgid(os.getgid()).gr_name
    variables = role_context(ROLE, {}) | {
        "claude_code_agent_user": pwd.getpwuid(os.getuid()).pw_name,
        "sys_user": group,
        "claude_code_operator_memory_dir": str(src),
        "claude_code_agent_memory_dir": str(dst),
        "ansible_check_mode": check_mode,
    }
    argv = evaluate(task["ansible.builtin.command"]["argv"], variables)
    assert argv[0] == "rsync"
    return [_real_rsync(), *argv[1:]]


def _real_rsync() -> str:
    """The installed rsync: leakguard's recording shim sits first on PATH and copies nothing.

    This copy stays inside `tmp_path`, so nothing leaves the process.
    """
    path = os.pathsep.join(
        entry
        for entry in os.environ["PATH"].split(os.pathsep)
        if not Path(entry).name.startswith("leakguard-")
    )
    found = shutil.which("rsync", path=path)
    assert found, "rsync is not installed; the copy task cannot run here either"
    return found


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_the_copy_keeps_modes_for_the_operators_read_and_leaves_the_source_alone(
    tmp_path: Path,
) -> None:
    src, dst = tmp_path / "operator", tmp_path / "agent"
    (src / "archive").mkdir(parents=True)
    dst.mkdir()
    (src / "MEMORY.md").write_text("- [x](/home/ubuntu/server/docs/x.md)\n")
    (src / "note.md").write_text("body\n")
    (src / "archive" / "old.md").write_text("old\n")
    for path in src.rglob("*"):
        path.chmod(0o600 if path.is_file() else 0o700)
    before = _tree(src)
    mtimes = {p: p.stat().st_mtime for p in src.rglob("*")}

    out = run(_copy_argv(src, dst, check_mode=False), check=True)

    assert _tree(dst) == before, "every file, nested ones included, must be copied"
    assert out.stdout.strip(), "a copy that changed files must print them"
    for path in dst.rglob("*"):
        mode = path.stat().st_mode & 0o7777
        assert mode == (0o640 if path.is_file() else 0o2750), (
            f"{path.relative_to(dst)} has mode {mode:o}"
        )
    assert (
        _tree(src) == before
        and {p: p.stat().st_mtime for p in src.rglob("*")} == mtimes
    ), "the operator's store must be left untouched"

    again = run(_copy_argv(src, dst, check_mode=False), check=True)
    assert not again.stdout.strip(), "a second run must report no change"


def test_check_mode_copies_nothing(tmp_path: Path) -> None:
    src, dst = tmp_path / "operator", tmp_path / "agent"
    src.mkdir()
    dst.mkdir()
    (src / "MEMORY.md").write_text("index\n")
    out = run(_copy_argv(src, dst, check_mode=True), check=True)
    assert "MEMORY.md" in out.stdout, "the dry run must name what it would copy"
    assert not list(dst.iterdir())
