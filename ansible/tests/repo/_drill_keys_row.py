"""The drill-key census row of target slice H2.3d: no host file reads a key the drill role defines.

The restore drill runs as a CronJob that Flux applies. A key in the `longhorn-restore-drill`
role's defaults reaches the cluster through Flux, so a host cron, script or template that reads it
bakes the value at the last hand apply. The drill's schedule key is the sharpest case: an absent
arm that names it reads a key that no longer exists in `setup/k3s`.

H1.7b's row, `no-flux-born-key-on-the-host-plane`, reads H1's six Flux-born roles only, and H2.3d
does not need H1.7b, so this is a row of its own. It reuses H1.7b's subject class and reader. A key
a host reader still needs stays in `setup/k3s`'s defaults under its own name
(`k3s_longhorn_restore_drill_max_age_days` and the namespace and stamp-directory keys).
The plan is `docs/target-architecture/host/h2-shrink-host-plane.md`, *The drill: state into a
ConfigMap first, then the move*.

Its own module because `test_census_rows_roles.py`, which imports it into `ROWS`, is at the cap for
a test module.
"""

from _flux_born_keys_row import (
    _K8S,
    _KEY,
    _TASKS,
    _TEMPLATE,
    CONVENTION_KEYS,
    FluxBornKey,
    host_reads,
)
from _row_table import Census, Subject, tracked

ROLE = "longhorn-restore-drill"
_DEFAULTS = f"{_K8S}{ROLE}/defaults/main.yml"


def _subjects() -> list[FluxBornKey]:
    if not tracked(_DEFAULTS):
        return []
    keys = dict.fromkeys(_KEY.findall(Subject(_DEFAULTS).text))
    return [FluxBornKey(ROLE, key) for key in keys if key not in CONVENTION_KEYS]


def _offence(subject: Subject) -> list[str]:
    return [
        line.replace(
            "which a Flux-born role defines: read a mirrored twin instead, or move the "
            "reader to the cluster",
            "which the longhorn-restore-drill role defines: a host reader needs a key of "
            "its own in `setup/k3s` defaults",
        )
        for line in host_reads(subject)
    ]


DRILL_KEYS = Census(
    name="drill-keys-off-the-host-plane",
    reason=(
        "A key the longhorn-restore-drill role defines reaches the cluster through Flux. A host "
        "file that reads it keeps the value of the last hand apply, so a flip promotes to the "
        "cluster and leaves the host stale. The drill's absent arm names no schedule key for the "
        "same reason. A key a host reader needs stays in `setup/k3s` defaults under its own name."
    ),
    files=_subjects,
    offence=_offence,
    red=(
        # The named fixture: a setup template that reads the drill's timeout.
        FluxBornKey(
            ROLE,
            "k3s_longhorn_restore_drill_timeout_s",
            {_TEMPLATE: "timeout {{ k3s_longhorn_restore_drill_timeout_s }}\n"},
        ),
        # The schedule key, read by a cron task: what the absent arm must not do.
        FluxBornKey(
            ROLE,
            "k3s_longhorn_restore_drill_cron",
            {
                _TASKS: '    minute: "{{ k3s_longhorn_restore_drill_cron.split()[0] }}"\n'
            },
        ),
    ),
    green=(
        # A key the host keeps is not a key the drill role defines.
        FluxBornKey(
            ROLE,
            "k3s_longhorn_restore_drill_timeout_s",
            {_TASKS: "  when: k3s_longhorn_restore_drill_max_age_days | int > 0\n"},
        ),
        # A YAML comment that names the key is not a read.
        FluxBornKey(
            ROLE,
            "k3s_longhorn_restore_drill_cron",
            {_TASKS: "# k3s_longhorn_restore_drill_cron moved to the drill role\n"},
        ),
    ),
    # 18 keys when the row landed (2026-10-24): the eight moved keys and ten the role defines.
    min_matches=18,
    must_find=frozenset(
        {
            f"{_DEFAULTS}#k3s_longhorn_restore_drill_cron",
            f"{_DEFAULTS}#k3s_longhorn_restore_drill_timeout_s",
        }
    ),
)
