#!/usr/bin/env python3
"""Both k3s node roles must refuse to install onto a host that already runs Docker.

WHY THIS IS A TEST AND NOT A COMMENT. The guard existed in tasks/server.yml from the
start, carrying a comment that named the hazard exactly: k3s brings its own containerd
and its iptables rules would land on top of Docker's chains. tasks/agent.yml listed the
same assert as *deliberately* omitted, which was correct while the Docker drain was in
progress and wrong the moment it finished.

Nothing noticed the difference. daniel-server -- the agent -- had docker-ce purged on
2026-08-14 and reinstalled on 2026-08-19 at 22:37, then ran a second container runtime
for eight days. Every repo-side check read green throughout, because the only host the
guard covered was the one that never had Docker.

A guard on one of two symmetric paths is not a guard. This asserts both paths carry it,
so removing either one fails the suite instead of quietly halving the coverage.

Run: uv run pytest ansible/tests/test_k3s_docker_guard.py
"""

import re

import pytest
from _helpers import ANSIBLE


K3S_TASKS = ANSIBLE / "roles" / "setup" / "k3s" / "tasks"

# The two node roles. Named explicitly rather than globbed: a new tasks file in this role
# is not automatically a node-install path, and globbing would make this test fail for
# reasons that have nothing to do with the guard.
NODE_TASK_FILES = ["server.yml", "agent.yml"]


@pytest.mark.parametrize("task_file", NODE_TASK_FILES)
def test_node_role_stats_the_docker_binary(task_file):
    text = (K3S_TASKS / task_file).read_text()
    assert "/usr/bin/docker" in text, (
        f"{task_file} does not stat /usr/bin/docker. Both k3s node roles must refuse to "
        f"install onto a host running Docker -- see this module's docstring for the "
        f"eight days that cost."
    )


@pytest.mark.parametrize("task_file", NODE_TASK_FILES)
def test_node_role_asserts_docker_is_absent(task_file):
    """The stat alone proves nothing -- it is the assert that fails the run."""
    text = (K3S_TASKS / task_file).read_text()
    assert re.search(r"that:\s*not \w*docker\w*\.stat\.exists", text), (
        f"{task_file} stats the Docker binary but does not assert on the result. A "
        f"registered stat with no assert reads like a guard and enforces nothing."
    )
