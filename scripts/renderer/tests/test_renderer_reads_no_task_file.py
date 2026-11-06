"""A render takes no value from a role's `tasks/`: it is the same with those directories gone.

`render-diff` cannot see a reader added back, because the files exist until R4.7 deletes them.
Each role here held a value in its tasks: a deferred pair, the file lists, two image builds and
a task-made fact. traefik-crds is the tag only its vendored file admits.

Run: uv run pytest scripts/renderer/tests/test_renderer_reads_no_task_file.py
"""

import shutil

from lib.repo_paths import REPO
from validate import render_diff_gates as gate

ROLES = ("pihole", "authelia", "n8n", "janitorr", "traefik-crds")


def test_a_render_is_the_same_without_any_role_task_file(tmp_path):
    for top in ("ansible", "scripts", "hosts"):
        shutil.copytree(
            REPO / top, tmp_path / top, ignore=shutil.ignore_patterns("__pycache__")
        )
    with_tasks = gate.snapshot(tmp_path, ROLES)
    removed = list((tmp_path / "ansible/roles/k8s").glob("*/tasks"))
    assert len(removed) >= 60
    for tasks in removed:
        shutil.rmtree(tasks)
    without = gate.snapshot(tmp_path, ROLES)
    assert set(ROLES) <= with_tasks.renders.keys()
    assert without.renders == with_tasks.renders
    assert without.digests == with_tasks.digests
