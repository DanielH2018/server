"""Typed views of the kubectl objects several readers share.

Three records: any object (`K8sObject`), a Longhorn Volume and a Longhorn BackupTarget.
The module imports with the stdlib alone (`lib/stdlib_only.py`), so a YAML reader parses first:
`renderer/renderer_lib/documents.py:parse_documents` does it for a rendered manifest.

A reader that runs `kubectl get ... -o json` builds one of these right after parsing the body,
then passes the record on. The record owns the field names, the defaults for a missing field and
the one rule each derived value follows. No caller indexes `.metadata`, `.spec`, `.status` or
`.status.conditions` itself.

Every `from_item` takes one parsed object and never raises on a missing field: a missing string
is "", a missing map is empty, and a status or state the enum does not know reads as its
`UNKNOWN` member.

Typical usage example:

    volumes = [LonghornVolume.from_item(item) for item in items]
    attached = {v.name for v in volumes if v.state is VolumeState.ATTACHED}
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from lib.longhorn_lib.longhorn_backups import GROUP_LABEL_PREFIX


class ConditionStatus(StrEnum):
    """A condition's `status`, which Kubernetes spells with a capital letter."""

    TRUE = "True"
    FALSE = "False"
    UNKNOWN = "Unknown"

    @classmethod
    def _missing_(cls, value: object) -> ConditionStatus:
        return cls.UNKNOWN


@dataclass(frozen=True)
class Condition:
    """One entry of `.status.conditions`, without its `type`, which keys it.

    Attributes:
        status: `status`.
        reason: `reason`, or "".
        message: `message`, or "".
        last_transition: `lastTransitionTime`, unparsed RFC3339, or "".
    """

    status: ConditionStatus
    reason: str
    message: str
    last_transition: str


def parse_conditions(item: Mapping) -> dict[str, Condition]:
    """`item`'s `.status.conditions` keyed by `type`, in the order the object lists them.

    When two entries share a type, the first wins, as a scan for the first match would pick it.
    An entry that is not an object is skipped.
    """
    conditions: dict[str, Condition] = {}
    for raw in (item.get("status") or {}).get("conditions") or []:
        if not isinstance(raw, Mapping):
            continue
        conditions.setdefault(
            str(raw.get("type") or ""),
            Condition(
                status=ConditionStatus(raw.get("status")),
                reason=str(raw.get("reason") or ""),
                message=str(raw.get("message") or ""),
                last_transition=str(raw.get("lastTransitionTime") or ""),
            ),
        )
    return conditions


@dataclass(frozen=True)
class K8sObject:
    """The fields every Kubernetes object carries, read once.

    Attributes:
        kind: `.kind`; "" when a single-object read omits it.
        name: `.metadata.name`.
        namespace: `.metadata.namespace`; "" for a cluster-scoped object.
        labels: `.metadata.labels`.
        annotations: `.metadata.annotations`.
        spec: `.spec`, unparsed, for the fields only one kind has.
        status: `.status`, unparsed, for the fields only one kind has.
        conditions: `.status.conditions`, as `parse_conditions` reads them.
    """

    kind: str
    name: str
    namespace: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    spec: Mapping
    status: Mapping
    conditions: dict[str, Condition]

    @classmethod
    def from_item(cls, item: Mapping) -> K8sObject:
        """One parsed object, such as an entry of a list's `.items`."""
        meta = item.get("metadata") or {}
        return cls(
            kind=str(item.get("kind") or ""),
            name=str(meta.get("name") or ""),
            namespace=str(meta.get("namespace") or ""),
            labels=meta.get("labels") or {},
            annotations=meta.get("annotations") or {},
            spec=item.get("spec") or {},
            status=item.get("status") or {},
            conditions=parse_conditions(item),
        )


class VolumeState(StrEnum):
    """A Longhorn Volume's `.status.state`."""

    CREATING = "creating"
    ATTACHING = "attaching"
    ATTACHED = "attached"
    DETACHING = "detaching"
    DETACHED = "detached"
    DELETING = "deleting"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> VolumeState:
        return cls.UNKNOWN


@dataclass(frozen=True)
class LonghornVolume:
    """One `volumes.longhorn.io` object.

    Attributes:
        name: `.metadata.name`, which is also the PV's name.
        labels: `.metadata.labels`.
        groups: every RecurringJob group a `<GROUP_LABEL_PREFIX><group>` label names, in label
            order. kubectl emits keys sorted.
        state: `.status.state`.
    """

    name: str
    labels: Mapping[str, str]
    groups: tuple[str, ...]
    state: VolumeState

    @property
    def group(self) -> str | None:
        """The last of `groups`, which is the greatest name; None when there is none."""
        return self.groups[-1] if self.groups else None

    @classmethod
    def from_item(cls, item: Mapping) -> LonghornVolume:
        """One entry of `kubectl get volumes.longhorn.io -o json`'s `.items`, or a single read."""
        obj = K8sObject.from_item(item)
        return cls(
            name=obj.name,
            labels=obj.labels,
            groups=tuple(
                key.removeprefix(GROUP_LABEL_PREFIX)
                for key in obj.labels
                if key.startswith(GROUP_LABEL_PREFIX)
            ),
            state=VolumeState(obj.status.get("state")),
        )


@dataclass(frozen=True)
class BackupTarget:
    """One `backuptargets.longhorn.io` object.

    Attributes:
        name: `.metadata.name`.
        url: `.spec.backupTargetURL`, or None when it is absent or blank. A blank URL is the
            deliberate disarm (`k3s_longhorn_backup_armed: false`). A reader that could not
            read the target has no `BackupTarget` and must not report it disarmed.
        available: whether `.status.available` is exactly true.
        message: the message of the first condition the target lists, or "".
    """

    name: str
    url: str | None
    available: bool
    message: str

    @classmethod
    def from_item(cls, item: Mapping) -> BackupTarget:
        """One BackupTarget object, from a list's `.items` or a single read."""
        obj = K8sObject.from_item(item)
        url = str(obj.spec.get("backupTargetURL") or "").strip()
        first = next(iter(obj.conditions.values()), None)
        return cls(
            name=obj.name,
            url=url or None,
            available=obj.status.get("available") is True,
            message=first.message if first else "",
        )
