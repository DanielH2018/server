"""Every initial_setup task answers to a tag narrower than the role and narrower than `crons`.

WHY. `initial_setup` is the largest setup role, and its CLAUDE.md promises that every task
carries a block tag, so one slice runs with `--tags <block>` instead of the whole role. Until
2026-10-01, 16 cron tasks broke that promise. They carried `crons` and nothing else, so the
secret-rotation wrappers, the weekly restart, the ansible.log truncation, the worktree sweep and
the apt-hygiene crons could only be reached by a run that also reapplied every other cron on the
host. Two render stamps were worse: `--tags docs` and `--tags evals` installed their wrappers and
skipped the stamp that records them, because the stamps carried `crons` alone.

`crons` stays on every cron task as the umbrella. This check refuses only a task whose effective
tags are empty or are `crons` alone. A `block:` wrapper's tags count for its children, the way
Ansible applies them.

Run: uv run pytest ansible/tests/setup/test_initial_setup_tasks_have_a_subject_tag.py
"""

from pathlib import Path

from lib import yaml_fast

from _helpers import ROLES

TASKS = ROLES / "setup" / "initial_setup" / "tasks"

# Tags that select too much to count as the task's own: the umbrella over every cron.
_UMBRELLA = frozenset({"crons"})

_NESTING_KEYS = ("block", "rescue", "always")


def _tags_of(task: dict) -> set[str]:
    tags = task.get("tags")
    if isinstance(tags, str):
        return {tags}
    if isinstance(tags, list):
        return {str(t) for t in tags}
    return set()


def _leaves_with_tags(tasks, inherited: frozenset[str] = frozenset()):
    """Yield (name, effective tags) for every task that runs something."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        tags = inherited | _tags_of(task)
        nested = [task[key] for key in _NESTING_KEYS if task.get(key)]
        if nested:
            for section in nested:
                yield from _leaves_with_tags(section, frozenset(tags))
        else:
            yield str(task.get("name", "<unnamed>")), tags


def untagged_or_umbrella_only(tasks) -> list[str]:
    """Names of tasks with no tag, or with no tag but `crons`."""
    return [name for name, tags in _leaves_with_tags(tasks) if not tags - _UMBRELLA]


def _task_files() -> list[Path]:
    # main.yml holds only the import_tasks lines; the tasks they import are walked directly.
    return sorted(p for p in TASKS.glob("*.yml") if p.name != "main.yml")


def test_every_initial_setup_task_has_a_subject_tag() -> None:
    offenders = []
    seen: dict[str, set[str]] = {}
    for path in _task_files():
        doc = yaml_fast.safe_load(path.read_text())
        seen.update(dict(_leaves_with_tags(doc)))
        offenders += [f"{path.name}: {name}" for name in untagged_or_umbrella_only(doc)]

    # A walk that silently matched nothing would pass. This task is one of the 16 that read
    # `crons` alone before 2026-10-01, so finding it with its subject tag proves the walk reached
    # crons.yml and read the tags this check is about.
    assert "weekly-restart" in seen.get(
        "Schedule sudo crontab for weekly restart", set()
    )

    assert not offenders, (
        "these initial_setup tasks are reachable only by a whole-role run or by `--tags crons`. "
        "Give each a subject tag beside `crons`, and give any task whose output another block "
        "reads every consumer's tag too:\n  " + "\n  ".join(offenders)
    )


def test_a_task_tagged_only_crons_is_flagged() -> None:
    doc = yaml_fast.safe_load(
        """
        - name: Schedule the weekly thing
          tags: [crons]
          ansible.builtin.cron:
            name: thing
        - name: Schedule the subject-tagged thing
          tags: [crons, thing]
          ansible.builtin.cron:
            name: thing
        """
    )
    assert untagged_or_umbrella_only(doc) == ["Schedule the weekly thing"]


def test_an_untagged_task_is_flagged() -> None:
    doc = yaml_fast.safe_load(
        """
        - name: Install the thing
          ansible.builtin.apt:
            name: thing
        """
    )
    assert untagged_or_umbrella_only(doc) == ["Install the thing"]


def test_a_block_wrapper_tag_covers_its_children() -> None:
    """The TLS cert-expiry retirement carries its tag on the block, not on each child."""
    doc = yaml_fast.safe_load(
        """
        - name: Retire the thing
          tags: [crons, thing]
          block:
            - name: Remove the cron
              ansible.builtin.cron:
                name: thing
                state: absent
        """
    )
    assert untagged_or_umbrella_only(doc) == []
