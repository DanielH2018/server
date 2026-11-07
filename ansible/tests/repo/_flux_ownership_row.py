"""The flux-ownership census rows: every k8s entry is flux-owned, and every k8s role is reachable
from an entry.

The row sits in its own module because `test_census_rows_roles.py`, which imports it into `ROWS`,
is at the cap for a test module.
"""

from _role_census import role_dirs
from _row_table import Subject, Census
from lib.k8s_roles import k8s_entries, role_callers

_K8S = "ansible/roles/k8s/"


class RoleSubject(Subject):
    """One k8s entry's role and its `owner`."""

    def __init__(self, role: str, owner: str | None):
        super().__init__(_K8S + role, text="")
        self.owner = owner


def _entry_subjects() -> list[RoleSubject]:
    return [
        RoleSubject(name, entry.get("owner"))
        for name, entry in sorted(k8s_entries().items())
    ]


def _not_flux_owned(subject: Subject) -> list[str]:
    owner = subject.owner if isinstance(subject, RoleSubject) else None
    return [] if owner == "flux" else [f"owner is {owner!r}, not flux"]


K8S_ENTRIES_ARE_FLUX_OWNED = Census(
    name="every-k8s-entry-is-flux-owned",
    reason=(
        "`deploy.sh` refuses every k8s tag and the tick applies no k8s role, so a k8s "
        "`containers_list` entry without `owner: flux` has no writer once `promote` stops "
        "publishing it, and the default deletion policy then deletes its objects. "
        "A handback is not a rollback since D6.12: fix a bad role forward (D6's *The deletion "
        "policy, and the end of the handback*)."
    ),
    files=_entry_subjects,
    offence=_not_flux_owned,
    red=(RoleSubject("sonarr", None), RoleSubject("sonarr", "ansible")),
    green=(RoleSubject("sonarr", "flux"),),
    # The count D5 ended with. A selector that stops matching fails here, not on nothing.
    min_matches=66,
    must_find=frozenset({_K8S + "bento-pdf", _K8S + "flux"}),
)


class ReachSubject(Subject):
    """One directory under `roles/k8s/`, with the entry names and caller map it is judged by.

    The subject carries those inputs, not a verdict, so a fixture exercises `_unreached`'s own
    reachability logic.
    """

    def __init__(self, role: str, named: set[str], callers: dict[str, set[str]]):
        super().__init__(_K8S + role, text="")
        self.role = role
        self.named = named
        self.callers = callers


def _reach_subjects() -> list[ReachSubject]:
    entries = k8s_entries()
    named = set(entries) | {e["role"] for e in entries.values() if e.get("role")}
    callers = role_callers()
    return [ReachSubject(d.name, named, callers) for d in role_dirs()]


def _unreached(subject: Subject) -> list[str]:
    if isinstance(subject, ReachSubject) and (
        subject.role in subject.named or subject.callers.get(subject.role)
    ):
        return []
    return ["no containers_list entry names it and no role includes it"]


K8S_ROLES_ARE_REACHABLE = Census(
    name="every-k8s-role-has-an-entry-or-a-caller",
    reason=(
        "`promote` renders a role through its `containers_list` entry or through a role that "
        "includes it. A role with neither renders nothing, and its landing has no tag and no "
        "note to name it, so the change reads as landed. Add the entry, or delete the role."
    ),
    files=_reach_subjects,
    offence=_unreached,
    # Red: another entry and another role's caller exist, but nothing reaches `newsvc`.
    red=(ReachSubject("newsvc", {"sonarr"}, {"manifests": {"sonarr"}}),),
    green=(
        ReachSubject("newsvc", {"newsvc"}, {}),
        ReachSubject("newsvc", set(), {"newsvc": {"sonarr"}}),
    ),
    allow={
        _K8S
        + "volume-revert": "the operator's revert runbook, `ansible/volume-revert.yml`",
    },
    min_matches=70,
    must_find=frozenset({_K8S + "bento-pdf", _K8S + "longhorn-api"}),
)
