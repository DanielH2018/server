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
_CONFIGMAP = "D5: the imperative script ConfigMap becomes a rendered ConfigMap"
_PROBE = "D5: the probe Job becomes a Job in `<tag>-verify`"
_VERIFY = "D5: the verify task's exec or pod check is converted or granted"

# Each writing task file, with the slice whose document moves what it writes. Two owners where
# the file's tasks split between slices.
ALLOW = {
    _K8S + "authelia/tasks/main.yml": (
        "R3 removes the `kubectl run` hashing pods; D5 makes the TOTP seed an initContainer"
    ),
    _K8S + "cronjob-gate/tasks/main.yml": (
        "D5: the gate Job becomes a Job in `<tag>-verify` copied from the jobTemplate"
    ),
    _K8S + "crowdsec/tasks/main.yml": (
        "D5: machine registration and the two allowlists become an initContainer"
    ),
    _K8S + "crowdsec/tasks/verify.yml": (
        "D5: the ban-and-lift check becomes the `crowdsec-verify` Job"
    ),
    _K8S + "flux/tasks/main.yml": (
        "D5's flux PR: the sync objects come from the artifact, and the drill delete goes"
    ),
    _K8S + "game-stats/tasks/terraria.yml": _CONFIGMAP,
    _K8S + "game-stats/tasks/valheim.yml": _CONFIGMAP,
    _K8S + "headlamp/tasks/main.yml": (
        "D4 makes the deferred apply `headlamp-deferred`; " + _PROBE
    ),
    _K8S + "image-builder/tasks/main.yml": (
        "D5.4: CI builds and pushes each image by its content tag"
    ),
    _K8S + "janitorr/tasks/verify.yml": _VERIFY,
    _K8S + "jellyfin/tasks/main.yml": "D5: the snapshot-size patch becomes a Job",
    _K8S + "media-volume/tasks/main.yml": _PROBE,
    _K8S + "monitor-bridge/tasks/main.yml": _CONFIGMAP,
    _K8S + "n8n/tasks/main.yml": "D5: `n8n-netpol-probe` becomes `n8n-verify`",
    _K8S + "netpol-baseline/tasks/main.yml": (
        "D4 makes the deferred apply `netpol-baseline-deferred`; " + _PROBE
    ),
    _K8S + "observability/tasks/dashboards.yml": (
        "D5: the dashboard ConfigMaps become rendered ConfigMaps"
    ),
    _K8S + "observability/tasks/main.yml": (
        "D5.1e: a restart annotation replaces the rollout restart"
    ),
    _K8S + "pihole/tasks/apply_instance_2.yml": (
        "D4 makes instance 2 `pihole-deferred`"
    ),
    _K8S + "pihole/tasks/main.yml": (
        "D5: the gravity reconcile becomes a Job or an initContainer"
    ),
    _K8S + "pihole/tasks/roll_one.yml": (
        "D5.1e: a restart annotation replaces the rollout restart"
    ),
    _K8S + "prowlarr/tasks/main.yml": (
        _PROBE + "; the one-shot PVC delete goes with prowlarr's flip"
    ),
    _K8S + "qbittorrent/tasks/verify.yml": _VERIFY,
    _K8S + "registry/tasks/main.yml": (
        "D5: the self-test and netpol probe Jobs become `registry-verify`"
    ),
    _K8S + "tdarr/tasks/verify.yml": _VERIFY,
    _K8S + "traefik/tasks/main.yml": (
        "D5: the CRDs get an in-tree copy, and the cluster RBAC moves with the role"
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
    # 58 writing tasks in 28 files of 23 roles when the row landed (2026-10-13).
    min_matches=50,
    must_find=frozenset(
        {
            _K8S + "monitor-bridge/tasks/main.yml",
            _K8S + "cronjob-gate/tasks/main.yml",
            _K8S + "qbittorrent/tasks/verify.yml",
        }
    ),
    allow=ALLOW,
)
