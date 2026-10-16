#!/usr/bin/env python3
"""optimize_pi stages the two Pi heartbeat push tokens as root-only interim files.

H3.8a of `docs/target-architecture/host/h3-pi-pilot.md`. The homelab-daniel-pi package cannot
carry a secret, so H3.8b's units read each token through `LoadCredential=` from a file Ansible
writes. Each file holds a Kuma push token, which is enough to forge or suppress a heartbeat, so:

1. Each file is owned by root at mode 0600, inside a root 0700 directory.
2. Each task that writes one sets `no_log: true`, or an apply prints the token.
3. Each carries the tag of the heartbeat it serves, the tags the operator's narrowed apply runs.
4. No task writes a `domain` file. `domain` is an inventory value since #5087, and the package
   renders it directly.

Run: uv run pytest ansible/tests/setup/test_pi_heartbeat_interim_credentials.py
"""

import copy

import pytest
from _helpers import SETUP_ROLES, load_tasks, walk_tasks

TASKS = SETUP_ROLES / "optimize_pi" / "tasks" / "main.yml"
INTERIM = "/etc/homelab/interim"

# Each staged token file, and the tag of the heartbeat that reads it.
TOKEN_FILES = {
    f"{INTERIM}/pi_sd_health_push_token": "sd-health",
    f"{INTERIM}/pi_recovery_push_token": "recovery-health",
}


def _tasks_by_destination() -> dict[str, dict]:
    """Every task in the role keyed by the `dest:` or `path:` its module writes."""
    found = {}
    for task in walk_tasks(load_tasks(TASKS)):
        for module in ("ansible.builtin.copy", "ansible.builtin.file"):
            spec = task.get(module)
            if isinstance(spec, dict):
                target = spec.get("dest") or spec.get("path")
                if target:
                    found[str(target)] = task
    return found


def token_task_problems(task: dict, tag: str) -> list[str]:
    """Every way one token-staging task can leak or misplace its token. Empty means correct."""
    spec = task.get("ansible.builtin.copy") or {}
    problems = []
    if spec.get("owner") != "root" or spec.get("group") != "root":
        problems.append(f"owner {spec.get('owner')}:{spec.get('group')}, not root:root")
    if spec.get("mode") != "0600":
        problems.append(f"mode {spec.get('mode')!r}, not '0600'")
    if task.get("no_log") is not True:
        problems.append("no `no_log: true`, so an apply prints the token")
    if tag not in (task.get("tags") or []):
        problems.append(f"tags {task.get('tags')} lack {tag!r}")
    return problems


@pytest.mark.parametrize("dest,tag", TOKEN_FILES.items())
def test_each_token_task_is_clean(dest, tag):
    task = _tasks_by_destination().get(dest)
    assert task is not None, f"no task writes {dest}"
    assert "ansible.builtin.copy" in task, f"{dest} is not written by a copy task"
    assert token_task_problems(task, tag) == []


def test_the_interim_directory_is_root_0700_under_both_tags():
    task = _tasks_by_destination().get(INTERIM)
    assert task is not None, f"no task creates {INTERIM}"
    spec = task["ansible.builtin.file"]
    assert spec.get("state") == "directory"
    assert (spec.get("owner"), spec.get("group"), spec.get("mode")) == (
        "root",
        "root",
        "0700",
    )
    assert {"sd-health", "recovery-health"} <= set(task.get("tags") or [])


def test_no_task_stages_a_domain_file():
    assert not [d for d in _tasks_by_destination() if d.rstrip("/").endswith("/domain")]


@pytest.mark.parametrize(
    "break_it",
    [
        lambda t: t["ansible.builtin.copy"].update(mode="0644"),
        lambda t: t.pop("no_log"),
    ],
    ids=["mode-0644", "no-no_log"],
)
def test_a_token_task_at_0644_or_without_no_log_is_flagged(break_it):
    dest, tag = next(iter(TOKEN_FILES.items()))
    task = copy.deepcopy(_tasks_by_destination()[dest])
    break_it(task)
    assert token_task_problems(task, tag) != []
