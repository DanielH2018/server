"""The flux-born-key census row of target slice H1.7b: no host file reads a key Flux's roles define.

A key defined in the defaults of a Flux-born role reaches the cluster through Flux. A host cron,
script or template that reads the same key bakes the value at the last hand apply, and a flip of
the key then promotes to the cluster while the host keeps the old value. A stale disarm leaves the
backup monitor permanently red, and a stale arm leaves a new target unchecked (ADR-0007). The plan
is `docs/target-architecture/host/h1-cluster-config-to-flux.md`, *Where the Longhorn variables go*.
The row sits in its own module because `test_census_rows_roles.py`, which imports it into `ROWS`,
is at the cap for a test module.

One subject per top-level key of a Flux-born role's `defaults/main.yml`. A key offends when a task,
handler or template under `ansible/roles/setup/` names it outside a YAML comment. A mirrored pair
is the way out for a key a host file needs: the host role defines its own twin under another name
(`k3s_etcd_s3_region` for `k3s_longhorn_r2_region`) and a test holds the two equal.
"""

import re
from functools import cache

from _row_table import Census, Subject, tracked

_K8S = "ansible/roles/k8s/"
# The six Flux-born roles of H1 (*Six Flux-born tags*). A role not yet in the tree is skipped, so
# the row follows the migration: it covers `coredns` from the PR that adds the role.
FLUX_BORN = (
    "cluster-base",
    "metallb",
    "coredns",
    "longhorn",
    "longhorn-backup",
    "longhorn-reconcile",
)
# The keys every k8s role defines under one name, as a convention the tooling reads in each role
# alike. A setup template that names `k8s_autodeploy` (the release-staleness check, the Renovate
# prompt) reads the convention across all roles, not a value one Flux-born role governs.
# DECIDED: exempt by name, because the census exists for the keys whose value a flip changes, and
# a flip of these changes a stance and no host-visible state.
CONVENTION_KEYS = frozenset({"k8s_autodeploy", "k8s_autodeploy_reason"})
_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):", re.MULTILINE)
_HOST_FILES = (
    "ansible/roles/setup/*/tasks/*",
    "ansible/roles/setup/*/handlers/*",
    "ansible/roles/setup/*/templates/*",
)


@cache
def _host_corpus() -> dict[str, str]:
    """`{path: text}` of every task, handler and template under `ansible/roles/setup/`."""
    return {rel: Subject(rel).text for rel in tracked(*_HOST_FILES)}


def _code(rel: str, text: str) -> str:
    """`text` without its YAML comment lines, so a comment that names a key is not a read.

    A Jinja template keeps every line: `#` opens a shell comment there, and the shell scripts
    those templates render read their keys in code.
    """
    if not rel.endswith((".yml", ".yaml")):
        return text
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


class FluxBornKey(Subject):
    """One key of a Flux-born role's defaults, with the host files that could read it."""

    def __init__(self, role: str, key: str, corpus: dict[str, str] | None = None):
        super().__init__(f"{_K8S}{role}/defaults/main.yml#{key}", text="")
        self.key = key
        self._corpus = corpus

    @property
    def corpus(self) -> dict[str, str]:
        return _host_corpus() if self._corpus is None else self._corpus


def _subjects() -> list[FluxBornKey]:
    found = []
    for role in FLUX_BORN:
        rel = f"{_K8S}{role}/defaults/main.yml"
        if not tracked(rel):
            continue
        keys = dict.fromkeys(_KEY.findall(Subject(rel).text))
        keys = {key: None for key in keys if key not in CONVENTION_KEYS}
        found += [FluxBornKey(role, key) for key in keys]
    return found


def host_reads(subject: Subject) -> list[str]:
    """Each host file that names the key, as a report line."""
    if not isinstance(subject, FluxBornKey):
        return []
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(subject.key)}(?![A-Za-z0-9_])")
    return [
        f"{rel} reads {subject.key}, which a Flux-born role defines: read a mirrored twin "
        "instead, or move the reader to the cluster"
        for rel, text in sorted(subject.corpus.items())
        if pattern.search(_code(rel, text))
    ]


_TEMPLATE = "ansible/roles/setup/k3s/templates/offbox.sh.j2"
_TASKS = "ansible/roles/setup/k3s/tasks/main.yml"

FLUX_BORN_KEYS = Census(
    name="no-flux-born-key-on-the-host-plane",
    reason=(
        "A key a Flux-born role defines reaches the cluster through Flux. A host file that reads "
        "it keeps the value of the last hand apply, so a flip of the key promotes to the cluster "
        "and leaves the host stale: a stale disarm makes the backup monitor permanently red, a "
        "stale arm leaves a new target unchecked. Make the host reader derive the value from the "
        "live object (the backup-health reader does), or define a mirrored twin in the host "
        "role under another name and hold the two equal with a test "
        "(k3s_etcd_s3_region and k3s_longhorn_r2_region)."
    ),
    files=_subjects,
    offence=host_reads,
    red=(
        # The named fixture: the off-box script reading the R2 region from longhorn-backup.
        FluxBornKey(
            "longhorn-backup",
            "k3s_longhorn_r2_region",
            {_TEMPLATE: '  --s3-region "{{ k3s_longhorn_r2_region }}" \\\n'},
        ),
        # A task reading an arm flag.
        FluxBornKey(
            "longhorn-backup",
            "k3s_longhorn_backup_armed",
            {_TASKS: "  when: k3s_longhorn_backup_armed | bool\n"},
        ),
    ),
    green=(
        # The mirrored twin is another name.
        FluxBornKey(
            "longhorn-backup",
            "k3s_longhorn_r2_region",
            {_TEMPLATE: '  --s3-region "{{ k3s_etcd_s3_region }}" \\\n'},
        ),
        # A longer key is not a read of the shorter one.
        FluxBornKey(
            "longhorn-backup",
            "k3s_longhorn_backup_cron",
            {_TASKS: "  when: k3s_longhorn_backup_cron_hour | bool\n"},
        ),
        # A YAML comment that names the key is not a read.
        FluxBornKey(
            "longhorn-backup",
            "k3s_longhorn_backup_armed",
            {_TASKS: "# k3s_longhorn_backup_armed moved to longhorn-backup\n"},
        ),
    ),
    # 50 keys across the five roles in the tree when the row landed (2026-10-22).
    min_matches=45,
    must_find=frozenset(
        {
            f"{_K8S}longhorn-backup/defaults/main.yml#k3s_longhorn_backup_armed",
            f"{_K8S}longhorn-backup/defaults/main.yml#k3s_longhorn_backup_cron",
        }
    ),
)
