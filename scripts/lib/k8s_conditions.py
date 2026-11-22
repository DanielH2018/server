"""A Kubernetes object's `.status.conditions`, read into typed records.

A condition's status is the string `True`, `False` or `Unknown`, and a compare against a
misspelt literal reads as a condition that never holds. A reader parses the conditions here and
compares the status with `is ConditionStatus.TRUE`.

The module imports the stdlib alone and no first-party module, because it ships beside the scripts
that read it: the soak Job carries it next to `flux_soak.py`, the host packages install it under
a `lib/` beside `k3s_gates.py`, the backup-health reader and the restart broker, and configarr's
health CronJob mounts it. `lib.k8s_objects` cannot ride there: it imports `lib.longhorn_lib`,
which reaches `host_lib`.

Typical usage example:

    ready = parse_conditions(item).get("Ready")
    if ready is not None and ready.status is ConditionStatus.TRUE:
        ...
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class ConditionStatus(StrEnum):
    """A condition's `status`, which Kubernetes spells with a capital letter."""

    TRUE = "True"
    FALSE = "False"
    UNKNOWN = "Unknown"

    @classmethod
    def _missing_(cls, value: object) -> "ConditionStatus":
        return cls.UNKNOWN


@dataclass(frozen=True)
class Condition:
    """One entry of `.status.conditions`, without its `type`, which keys it.

    Attributes:
        status: `status`.
        reason: `reason`, or "".
        message: `message`, or "".
        last_transition: `lastTransitionTime`, unparsed RFC3339, or "".
        last_heartbeat: `lastHeartbeatTime`, unparsed RFC3339, or "". Only a Node's conditions
            carry one.
        observed_generation: `observedGeneration`, or None when the entry has no integer one.
    """

    status: ConditionStatus
    reason: str
    message: str
    last_transition: str
    last_heartbeat: str
    observed_generation: int | None


NO_CONDITION = Condition(ConditionStatus.UNKNOWN, "", "", "", "", None)


def parse_conditions(item: Mapping) -> dict[str, Condition]:
    """`item`'s `.status.conditions` keyed by `type`, in the order the object lists them.

    When two entries share a type, the first wins, as a scan for the first match would pick it.
    A `status` that is not an object, a `conditions` that is not a list and an entry that is not
    an object each contribute nothing, because the object comes from the cluster.
    """
    status = item.get("status")
    raw_conditions = status.get("conditions") if isinstance(status, Mapping) else None
    conditions: dict[str, Condition] = {}
    for raw in raw_conditions if isinstance(raw_conditions, list) else []:
        if not isinstance(raw, Mapping):
            continue
        generation = raw.get("observedGeneration")
        conditions.setdefault(
            str(raw.get("type") or ""),
            Condition(
                status=ConditionStatus(raw.get("status")),
                reason=str(raw.get("reason") or ""),
                message=str(raw.get("message") or ""),
                last_transition=str(raw.get("lastTransitionTime") or ""),
                last_heartbeat=str(raw.get("lastHeartbeatTime") or ""),
                observed_generation=generation
                if isinstance(generation, int) and not isinstance(generation, bool)
                else None,
            ),
        )
    return conditions
