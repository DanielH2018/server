"""A PR that only deletes a setup role's files owes no hand, and a real owed line still does.

These cases drive `land.main` with the REAL classifier. The verdict comes from the path list,
the PR's own file statuses and the owed ledger, which `read_state` answers as its
`owed.jsonl` marker.

Run: uv run pytest scripts/deploy_tools/tests/test_land_deleted_setup_role_owes_no_hand.py
"""

import json

import pytest

from deploy_tools.land_lib.tools import Classifier
from _land_fakes import Fakes

# Files H7.63 deleted from `common`, a setup role no playbook applies.
_DELETED = [
    "ansible/roles/setup/common/tasks/check_timer.yml",
    "ansible/roles/setup/common/tasks/install_host_lib.yml",
    "ansible/roles/setup/common/templates/check-timer.service.j2",
]
_SERVICE = "ansible/roles/k8s/sonarr/defaults/main.yml"
# A k3s role change is applied by hand through `k3s-bringup.yml`.
_K3S = "ansible/roles/setup/k3s/tasks/node.yml"


def _owed_line(role: str) -> str:
    """One `manual_plane` line of the owed ledger, as the deployer writes it."""
    return json.dumps(
        {"class": "manual_plane", "subject": role, "origin": "abc123def4567890"}
    )


def _fakes(removed: list[str], modified: list[str], owed: str = "", tags=()) -> Fakes:
    """A merged PR deleting `removed` and editing `modified`, with this owed ledger.

    Every surface that can say a file was deleted says it: the REST listing's `removed`
    status, the view's `DELETED` change type with no additions, and a PR-head tree that
    lacks the file.
    """
    paths = [*removed, *modified]
    view_files = [
        {"path": p, "changeType": "DELETED", "additions": 0, "deletions": 40}
        for p in removed
    ] + [
        {"path": p, "changeType": "MODIFIED", "additions": 1, "deletions": 1}
        for p in modified
    ]
    return Fakes(
        gh_views={
            "files,changedFiles": {"files": view_files, "changedFiles": len(paths)}
        },
        pr_files=[{"status": "removed", "filename": p} for p in removed]
        + [{"status": "modified", "filename": p} for p in modified],
        derived=(list(tags), "pr"),
        state={"owed.jsonl": owed},
    )


@pytest.fixture
def land(land_run):
    """Run a landing over `_fakes`; returns (verdict word, rc, stdout)."""

    def run(f: Fakes) -> tuple[str, int, str]:
        rc, out, _err, _calls, _log = land_run([], f, classifier=Classifier())
        line = next(ln for ln in out.splitlines() if ln.startswith("VERDICT: "))
        return line.removeprefix("VERDICT: ").split(" ", 1)[0], rc, out

    return run


def test_a_pr_deleting_only_a_setup_roles_files_owes_no_hand(land):
    verdict, rc, out = land(_fakes(_DELETED, []))
    assert verdict in {"settled", "nothing-to-deploy"}, out
    assert rc == 0


def test_a_deleted_setup_role_beside_a_service_lands_settled(land):
    """The shape #7864 had: services deployed, the deleted role's files beside them."""
    verdict, rc, out = land(_fakes(_DELETED, [_SERVICE], tags=["sonarr"]))
    assert verdict == "settled", out
    assert rc == 0


def test_a_deleted_setup_role_is_not_told_to_clear_a_marker_the_host_lacks(land):
    _verdict, _rc, out = land(_fakes(_DELETED, []))
    assert "clear-owed manual_plane common" not in out


def test_an_empty_ledger_names_no_marker_to_clear_for_a_changed_role(land):
    _verdict, _rc, out = land(_fakes([], [_K3S]))
    assert "clear-owed manual_plane k3s" not in out


def test_a_real_owed_line_still_needs_a_hand_beside_a_deleted_role(land):
    """The owed `k3s` line is real; the deleted `common` files owe nothing."""
    verdict, rc, out = land(_fakes(_DELETED, [_K3S], owed=_owed_line("k3s")))
    assert verdict == "needs-manual-apply", out
    assert rc == 1
    assert "clear-owed manual_plane k3s" in out
    assert "clear-owed manual_plane common" not in out


def test_a_changed_role_the_ledger_lacks_still_needs_a_hand(land):
    """The paths decide the verdict; the empty ledger drops only the clear command."""
    verdict, rc, out = land(_fakes([], [_K3S]))
    assert verdict == "needs-manual-apply", out
    assert rc == 1
    assert "k3s-bringup.yml" in out
