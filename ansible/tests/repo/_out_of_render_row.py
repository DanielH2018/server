"""The census row of what the shadow render does not cover: role tasks that write the cluster.

Target slice R1's exit criterion 3. The renderer reproduces what the manifests role renders,
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
    return [
        rel
        for rel in tracked("ansible/roles/k8s/*/tasks/*.yml")
        if not rel.startswith("ansible/roles/k8s/manifests/")
    ]


_K8S = "ansible/roles/k8s/"
_VERIFY = "D5: the verify task's exec or pod check is converted or granted"

# Each writing task file, with the slice whose document moves what it writes. Two owners where
# the file's tasks split between slices.
ALLOW = {
    _K8S + "crowdsec/tasks/verify.yml": (
        "D5: the ban-and-lift check becomes the `crowdsec-verify` Job"
    ),
    _K8S + "flux/tasks/main.yml": (
        "D5's flux PR: the sync objects come from the artifact, and the drill delete goes"
    ),
    _K8S + "image-builder/tasks/main.yml": (
        "D5.4: CI builds and pushes each image by its content tag"
    ),
    _K8S + "volume-revert/tasks/claim.yml": (
        "H7.16b: a task-free command replaces the revert runbook"
    ),
    _K8S + "volume-snapshot/tasks/claim.yml": (
        "D5.2a: the snapshot and its prune become a Job per role revision"
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
        "homelab-render reproduces what the manifests role renders, and nothing a role task "
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
    # 58 writing tasks in 28 files of 23 roles when the row landed (2026-10-13); 49 once
    # n8n's flip deleted its probe tasks (#5546); 44 once media-volume's flip deleted its
    # hardlink probe tasks (#5652); 43 once authelia's TOTP seed became an initContainer
    # (#5647); 41 once the flip of tdarr and qbittorrent deleted their verify tasks; 39 once
    # traefik's flip rendered its CRDs and RBAC (#5707); 37 once crowdsec's machine
    # registration and allowlists became an initContainer (#5741); 33 once observability's
    # flip deleted its dashboard apply and its restart tasks; 30 once the flip of janitorr and
    # jellyfin deleted their verify and cap tasks; 27 once headlamp's flip deleted its
    # observability RBAC apply and its isolation probe; 21 once pihole's flip deleted its
    # roll, apply and gravity tasks; 15 once registry's flip deleted its self-test and netpol
    # probe tasks; 12 once netpol-baseline's flip deleted its probe and apply tasks; 10 once
    # the cronjob-gate role, with its delete and create tasks, was retired (#5716).
    min_matches=10,
    must_find=frozenset(
        {
            _K8S + "image-builder/tasks/main.yml",
        }
    ),
    allow=ALLOW,
)
