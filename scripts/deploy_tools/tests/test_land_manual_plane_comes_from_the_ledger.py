"""The owed ledger decides which setup role a hand owes; the PR's paths only nominate roles (#7906).

These cases drive `land.main` with the REAL classifier. The verdict comes from the path list,
the PR's own file statuses, the merge commit's tree and the owed ledger, which the fakes answer
as `Fakes.state["owed.jsonl"]` (or `Fakes.owed_error` for a read that fails).

Run: uv run pytest scripts/deploy_tools/tests/test_land_manual_plane_comes_from_the_ledger.py
"""

import json

import pytest

from deploy_tools.land_lib import land_tags, land_tags_cli
from deploy_tools.land_lib.tools import Classifier
from deploy_tools.promote_lib.wait import WaitResult
from _land_fakes import Fakes

# Files H7.63 deleted from `common`, a setup role no playbook applies.
_DELETED = [
    "ansible/roles/setup/common/tasks/check_timer.yml",
    "ansible/roles/setup/common/tasks/install_host_lib.yml",
    "ansible/roles/setup/common/templates/check-timer.service.j2",
]
# A task file of `common` that the PR leaves in place.
_COMMON_KEPT = "ansible/roles/setup/common/tasks/main.yml"
_SERVICE = "ansible/roles/k8s/sonarr/defaults/main.yml"
# A k3s role change is applied by hand through `k3s-bringup.yml`.
_K3S = "ansible/roles/setup/k3s/tasks/node.yml"
_BRINGUP = "ansible/k3s-bringup.yml"


def _owed_line(role: str) -> str:
    """One `manual_plane` line of the owed ledger, as the deployer writes it."""
    return json.dumps(
        {"class": "manual_plane", "subject": role, "origin": "abc123def4567890"}
    )


def _fakes(
    removed: list[str],
    modified: list[str],
    owed: str = "",
    tags=(),
    kept: tuple[str, ...] = (),
) -> Fakes:
    """A merged PR deleting `removed` and editing `modified`, with this owed ledger.

    Every surface that can say a file was deleted says it: the REST listing's `removed`
    status, the view's `DELETED` change type with no additions, and a merge-commit tree that
    lacks the file. `kept` names files the PR leaves untouched in that tree.
    """
    paths = [*removed, *modified]
    view_files = [
        {"path": p, "changeType": "DELETED", "additions": 0, "deletions": 40}
        for p in removed
    ] + [
        {"path": p, "changeType": "MODIFIED", "additions": 1, "deletions": 1}
        for p in modified
    ]
    fakes = Fakes(
        gh_views={
            "files,changedFiles": {"files": view_files, "changedFiles": len(paths)}
        },
        pr_files=[{"status": "removed", "filename": p} for p in removed]
        + [{"status": "modified", "filename": p} for p in modified],
        head_tree={p: "100644" for p in [*modified, *kept]},
        derived=(list(tags), "pr"),
        state={"owed.jsonl": owed},
    )
    if tags:
        # The gate reads the tags land-wait's changed set names (`WaitResult.tags`), not the
        # tags the paths derived, so the promotion must name each service it applied.
        fakes.land_wait = [
            WaitResult(verdict="settled", detail="1 applied", tags=tuple(tags))
        ]
    return fakes


@pytest.fixture
def land(land_run):
    """Run a landing over `_fakes`; returns (verdict word, rc, stdout)."""

    def run(f: Fakes) -> tuple[str, int, str]:
        rc, out, _err, _calls, _log = land_run([], f, classifier=Classifier())
        line = next(ln for ln in out.splitlines() if ln.startswith("VERDICT: "))
        return line.removeprefix("VERDICT: ").split(" ", 1)[0], rc, out

    return run


# -- a modified setup role: the ledger decides ----------------------------------------------


def test_a_modified_role_with_an_empty_ledger_lands_without_a_hand(land):
    verdict, rc, out = land(_fakes([], [_K3S]))
    assert verdict in {"settled", "nothing-to-deploy"}, out
    assert rc == 0
    assert "k3s-bringup.yml" not in out
    assert "clear-owed manual_plane k3s" not in out


def test_a_modified_role_with_a_matching_ledger_line_needs_a_hand(land):
    verdict, rc, out = land(_fakes([], [_K3S], owed=_owed_line("k3s")))
    assert verdict == "needs-manual-apply", out
    assert rc == 1
    assert "k3s-bringup.yml" in out
    assert "clear-owed manual_plane k3s" in out


def test_a_ledger_line_for_a_role_the_pr_does_not_touch_owes_this_pr_nothing(land):
    """The paths name which lines this PR could have created; another role's line is not one."""
    verdict, rc, out = land(_fakes([], [_K3S], owed=_owed_line("common")))
    assert verdict in {"settled", "nothing-to-deploy"}, out
    assert rc == 0


def test_a_bringup_playbook_with_an_empty_ledger_still_needs_a_hand(land):
    """The tick parked on a bring-up playbook and wrote no line, so a ledger test would mute it."""
    verdict, rc, out = land(_fakes([], [_BRINGUP]))
    assert verdict == "needs-manual-apply", out
    assert rc == 1
    assert "k3s-bringup.yml" in out


# -- deletions: quiet only when the role's whole task set goes ------------------------------


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


def test_a_real_owed_line_still_needs_a_hand_beside_a_deleted_role(land):
    """The owed `k3s` line is real; the deleted `common` files owe nothing."""
    verdict, rc, out = land(_fakes(_DELETED, [_K3S], owed=_owed_line("k3s")))
    assert verdict == "needs-manual-apply", out
    assert rc == 1
    assert "clear-owed manual_plane k3s" in out
    assert "clear-owed manual_plane common" not in out


def test_a_partial_deletion_still_reports_the_role_the_ledger_holds(land):
    """One task file of `common` goes and another stays: the role keeps doing something."""
    verdict, rc, out = land(
        _fakes(
            _DELETED[:1],
            [_SERVICE],
            owed=_owed_line("common"),
            tags=["sonarr"],
            kept=(_COMMON_KEPT,),
        )
    )
    assert verdict == "needs-manual-apply", out
    assert rc == 1
    assert "clear-owed manual_plane common" in out


def test_a_partial_deletion_beside_an_edit_of_the_role_still_reports_it(land):
    verdict, rc, out = land(
        _fakes(_DELETED[:1], [_COMMON_KEPT], owed=_owed_line("common"))
    )
    assert verdict == "needs-manual-apply", out
    assert rc == 1


# -- an unreadable ledger ends deferred ------------------------------------------------------


def test_an_unreadable_ledger_ends_deferred_naming_the_read_error(land):
    f = _fakes([], [_K3S])
    f.owed_error = PermissionError("owed.jsonl: denied")
    verdict, rc, out = land(f)
    assert verdict == "deferred", out
    assert rc == 75
    assert "PermissionError: owed.jsonl: denied" in out
    assert "k3s" in out
    # #7969: `land_probe` reads the give-up from this field, not from the note's wording.
    assert "LAND-RESULT: verdict=deferred cause=ledger-unreadable " in out


def test_an_unreadable_ledger_defers_no_pr_that_touches_no_setup_role(land):
    f = _fakes([], [_SERVICE], tags=["sonarr"])
    f.owed_error = PermissionError("owed.jsonl: denied")
    verdict, rc, out = land(f)
    assert verdict == "settled", out
    assert rc == 0


# -- the plane CLI reads the same ledger -----------------------------------------------------


def _plane_cli(tmp_path, capsys, ledger: str | None):
    if ledger is not None:
        (tmp_path / "owed.jsonl").write_text(ledger)
    payload = json.dumps({"files": [{"path": _K3S}], "changedFiles": 1})
    argv = ["--json", payload, "--plane", "--deployer-state", str(tmp_path)]
    rc = land_tags_cli.main(argv)
    return rc, capsys.readouterr()


def test_the_plane_cli_names_a_role_only_when_the_ledger_holds_it(tmp_path, capsys):
    rc, seen = _plane_cli(tmp_path, capsys, _owed_line("k3s") + "\n")
    assert rc == 0
    assert "clear-owed manual_plane k3s" in seen.out


def test_the_plane_cli_prints_nothing_for_an_empty_ledger(tmp_path, capsys):
    rc, seen = _plane_cli(tmp_path, capsys, "")
    assert rc == 0
    assert seen.out.strip() == ""


def test_the_plane_cli_exits_75_naming_an_unreadable_ledger(tmp_path, capsys):
    (tmp_path / "owed.jsonl").mkdir()
    rc, seen = _plane_cli(tmp_path, capsys, None)
    assert rc == 75
    assert "could not be read" in seen.err
    assert land_tags.MARKERS["owed"] in seen.err
