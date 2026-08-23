#!/usr/bin/env python3
"""Guards against ansible_env.HOME being used to mean "the connecting user's home".

`ansible_env` reports the environment of the user facts were GATHERED as, not the
user a task runs as. initial_setup.yml gathers facts escalated on purpose — its
`setup:` pre_task runs under the play's become: true so that hosts with passworded
sudo don't need --ask-become-pass — which makes ansible_env.HOME `/root` for the
whole play.

So a task can set become: false, run as `ubuntu`, and still resolve
ansible_env.HOME to /root. That is exactly what happened on daniel-box
(2026-08-01):

    TASK [chezmoi_setup : Ensure the per-user directories exist]
    failed: (item=/root/.local/bin) ... Permission denied: b'/root/.local'

The roles that manage a user's home use /home/{{ sys_user }} instead, matching
config_files, which hardcodes the same path for the same reason.

Run: uv run pytest ansible/tests/test_per_user_home_resolution.py
"""

import pytest

from _helpers import SETUP_ROLES, load_tasks, walk_tasks


def _task_files():
    return sorted(SETUP_ROLES.glob("*/tasks/*.yml"))


@pytest.mark.parametrize(
    "path", _task_files(), ids=lambda p: f"{p.parents[1].name}/{p.name}"
)
def test_unescalated_tasks_do_not_use_ansible_env_home(path):
    offenders = [
        task.get("name", "<unnamed>")
        for task in walk_tasks(load_tasks(path))
        if task.get("become") is False and "ansible_env.HOME" in str(task)
    ]

    assert not offenders, (
        f"{path.name}: a become: false task uses ansible_env.HOME. Facts are gathered "
        "escalated in initial_setup.yml, so that resolves to /root even though the task "
        "runs as the connecting user — the task will try to write into root's home and "
        f"fail with EACCES. Use /home/{{{{ sys_user }}}} instead. Offending: {offenders}"
    )


def test_the_guard_actually_inspects_something():
    assert len(_task_files()) >= 5, "expected several setup-role task files"
