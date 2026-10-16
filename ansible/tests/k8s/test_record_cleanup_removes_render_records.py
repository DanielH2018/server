"""A role task that removes a service's release record removes its render record too.

The k8s/manifests role writes two records per service on the host: the release record in
`k8s-releases.d/<name>.json` and the render record in `k8s-renders.d/<name>.json`. A rename's
cleanup task that drops only the first leaves a render record with no release, which the
deployer's narrow shadow counts as `unknown: no release record` on every tick (#5162).
ansible/tasks/k8s_retire.yml already removes all three files for a whole-service retirement.

Run: uv run pytest ansible/tests/k8s/test_record_cleanup_removes_render_records.py
"""

import re

from _helpers import ANSIBLE, load_tasks, walk_tasks
from _role_census import task_files

_RELEASE = re.compile(r"/var/lib/homelab/k8s-releases\.d/([A-Za-z0-9_-]+)\.json$")
_RENDER = "/var/lib/homelab/k8s-renders.d/{}.json"


def _absent_paths(task: dict) -> list[str]:
    """The literal paths a `file: state=absent` task removes, from its path or its loop."""
    module = task.get("ansible.builtin.file") or task.get("file")
    if not isinstance(module, dict) or module.get("state") != "absent":
        return []
    path, loop = module.get("path"), task.get("loop")
    if not isinstance(path, str):
        return []
    # Both loop shapes occur: full paths over `{{ item }}`, and bare file names under a fixed
    # directory, which is how the claude-otel cleanup hid its missing render record (#5162).
    if isinstance(loop, list) and "{{ item }}" in path:
        return [path.replace("{{ item }}", i) for i in loop if isinstance(i, str)]
    return [path]


def _orphaned_renders(tasks: list[dict]) -> list[str]:
    """Each service whose release record a task removes while leaving its render record."""
    missing = []
    for task in walk_tasks(tasks):
        paths = _absent_paths(task)
        for path in paths:
            match = _RELEASE.search(path)
            if match and _RENDER.format(match[1]) not in paths:
                missing.append(match[1])
    return missing


def test_every_role_task_removing_a_release_record_removes_the_render_record():
    removed = {}
    orphaned = []
    for path in task_files():
        tasks = load_tasks(path)
        for task in walk_tasks(tasks):
            for p in _absent_paths(task):
                if m := _RELEASE.search(p):
                    removed[m[1]] = path
        orphaned += [
            f"{path.relative_to(ANSIBLE)}: {n}" for n in _orphaned_renders(tasks)
        ]
    # The named members: the two renames #5162 found, so the scan cannot pass on an empty read
    # or on a task whose paths it no longer parses.
    assert {"claude-otel", "terraria-stats", "valheim-stats"} <= removed.keys()
    assert orphaned == []


def test_a_release_record_removed_without_its_render_record_is_reported():
    task = {
        "name": "Remove the release records a rename left behind",
        "ansible.builtin.file": {"path": "{{ item }}", "state": "absent"},
        "loop": [
            "/var/lib/homelab/k8s-releases.d/old-name.json",
            "/var/lib/homelab/k8s-releases.d/old-name.previous.json",
        ],
    }
    assert _orphaned_renders([task]) == ["old-name"]
    task["loop"].append("/var/lib/homelab/k8s-renders.d/old-name.json")
    assert _orphaned_renders([task]) == []


def test_bare_names_under_a_fixed_release_directory_are_read_too():
    task = {
        "name": "Remove the release records a rename left behind",
        "ansible.builtin.file": {
            "path": "/var/lib/homelab/k8s-releases.d/{{ item }}",
            "state": "absent",
        },
        "loop": ["old-name.json", "old-name.previous.json"],
    }
    assert _orphaned_renders([task]) == ["old-name"]
