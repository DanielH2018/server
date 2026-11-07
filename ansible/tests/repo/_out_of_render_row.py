"""The census row of what the shadow render does not cover: role tasks that write the cluster.

Target slice R1's exit criterion 3. The renderer renders manifests,
and nothing a role task applies, creates, patches, runs or writes through `exec` itself. This
row names every task file that does, with the slice that owns moving it. It sits in its own
module because `test_census_rows_roles.py`, which imports it into `ROWS`, is near the cap
for a test module.

A task writes when `_k8s_guards._chunk_mutates` matches its chunk, the rule the dry-run guard
census in `ansible/tests/deploy/test_k8s_dry_run.py` reads too. Chunks keep their comments,
so a task whose comment quotes `kubectl apply` counts; that over-count is the rule's, and the
rule errs that way on purpose. `count` is the number of writing chunks in a file, so a file
that stops writing stops being a member, and its `allow` entry turns stale.
"""

import re

from _k8s_guards import _chunk_mutates, _text_chunks
from _row_table import Census, Subject, tracked

_TASK_NAME = re.compile(r"-\s*name:\s*(.*?)(?=\s+[\w.]+:|$)")


def _writing_chunks(subject: Subject) -> list[str]:
    """The name of every task in `subject` that writes a cluster object itself."""
    return [
        m.group(1) if (m := _TASK_NAME.match(chunk)) else chunk[:60]
        for chunk in _text_chunks(subject.text)
        if _chunk_mutates(chunk)
    ]


def _role_task_files() -> list[str]:
    return tracked("ansible/roles/k8s/*/tasks/*.yml")


_K8S = "ansible/roles/k8s/"

# Each writing task file, with the slice whose document moves what it writes. Two owners where
# the file's tasks split between slices.
ALLOW = {
    _K8S + "volume-revert/tasks/claim.yml": (
        "H7.16b: a task-free command replaces the revert runbook"
    ),
}

_APPLY = (
    "- name: Apply the widget's extra objects\n"
    "  ansible.builtin.command:\n"
    "    cmd: k3s kubectl -n {{ k8s_namespace }} apply -f /tmp/widget/\n"
)

OUT_OF_RENDER = Census(
    name="render-does-not-cover-role-writes",
    reason=(
        "homelab-render renders manifests, and nothing a role task "
        "writes to the cluster itself (target slice R1, exit criterion 3). Every task file "
        "that writes is listed here with the slice that owns its move. A new writing file is "
        "a new object outside the render: add it to `ALLOW` in "
        "ansible/tests/repo/_out_of_render_row.py with its owning slice. A file that stopped "
        "writing leaves the list."
    ),
    files=_role_task_files,
    offence=lambda s: [f"writes outside the render: {n}" for n in _writing_chunks(s)],
    count=lambda s: len(_writing_chunks(s)),
    red=(Subject("widget/tasks/main.yml", _APPLY),),
    green=(Subject("widget/tasks/main.yml", _APPLY.replace(" apply -f", " get -f")),),
    min_matches=1,
    must_find=frozenset(
        {
            _K8S + "volume-revert/tasks/claim.yml",
        }
    ),
    allow=ALLOW,
)
