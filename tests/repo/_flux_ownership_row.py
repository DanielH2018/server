"""The census row that every k8s role is reachable from a `containers_list` entry.

The row sits in its own module because `test_census_rows_roles.py`, which imports it into `ROWS`,
is at the cap for a test module.
"""

from _role_census import role_dirs
from _row_table import Subject, Census
from lib.k8s_roles import k8s_entries, role_callers

_K8S = "k8s/roles/"


class ReachSubject(Subject):
    """One directory under `k8s/roles/`, with the entry names and caller map it is judged by.

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
    # 70 before H7.55 deleted the two runbook roles, longhorn-api and volume-revert.
    min_matches=69,
    must_find=frozenset({_K8S + "bento-pdf", _K8S + "arr-notification"}),
)
