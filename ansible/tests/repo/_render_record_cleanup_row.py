"""The census row holding a role task that removes a release record to remove the render record.

The k8s/manifests role writes two records per service on the host: the release record in
`k8s-releases.d/<name>.json` and the render record in `k8s-renders.d/<name>.json`. A rename's
cleanup task that drops only the first leaves a render record with no release, which the
deployer's narrow shadow counts as `unknown: no release record` on every tick (#5162).
ansible/tasks/k8s_retire.yml removes all three files for a whole-service retirement. This row
sits in its own module because `test_census_rows_roles.py`, which imports it into `ROWS`, is
near the cap for a test module.
"""

import re

from _helpers import walk_tasks
from _row_table import Census, Subject, tracked
from lib import yaml_fast

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


def _released(subject: Subject) -> list[tuple[str, bool]]:
    """`(service, render record removed too)` for every release record `subject` removes."""
    found = []
    for task in walk_tasks(yaml_fast.safe_load(subject.text) or []):
        paths = _absent_paths(task)
        found += [
            (m[1], _RENDER.format(m[1]) in paths)
            for path in paths
            if (m := _RELEASE.search(path))
        ]
    return found


_RENAMED = (
    "- name: Remove the records a rename left behind\n"
    "  ansible.builtin.file:\n"
    "    path: {path}\n"
    "    state: absent\n"
    "  loop:\n"
)
_FULL = _RENAMED.format(path='"{{ item }}"') + (
    "    - /var/lib/homelab/k8s-releases.d/old-name.json\n"
    "    - /var/lib/homelab/k8s-releases.d/old-name.previous.json\n"
)
_BARE = _RENAMED.format(path='"/var/lib/homelab/k8s-releases.d/{{ item }}"') + (
    "    - old-name.json\n    - old-name.previous.json\n"
)

RENDER_RECORD_CLEANUP = Census(
    name="release-record-cleanup-removes-the-render-record",
    reason=(
        "A role task that removes a service's release record from k8s-releases.d leaves its "
        "render record in k8s-renders.d unless it names that too, and the deployer's narrow "
        "shadow then counts the service as `unknown: no release record` on every tick "
        "(#5162). Add `/var/lib/homelab/k8s-renders.d/<name>.json` to the same task."
    ),
    files=lambda: tracked("ansible/roles/*/*/tasks/*.yml"),
    offence=lambda s: [
        f"removes the release record of {name} but not its render record"
        for name, both in _released(s)
        if not both
    ],
    count=lambda s: len(_released(s)),
    red=(
        Subject("widget/tasks/main.yml", _FULL),
        Subject("widget/tasks/main.yml", _BARE),
    ),
    green=(
        Subject(
            "widget/tasks/main.yml",
            _FULL + "    - /var/lib/homelab/k8s-renders.d/old-name.json\n",
        ),
    ),
    # claude-otel, terraria-stats and valheim-stats when the row landed (2026-10-16).
    min_matches=3,
    must_find=frozenset(
        {
            "ansible/roles/k8s/game-stats/tasks/main.yml",
            "ansible/roles/k8s/observability/tasks/main.yml",
        }
    ),
)
