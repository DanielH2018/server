"""`status` reads a daniel-server batch's landing from the server lander's log on daniel-box.

Split from test_fanout_status.py, which is at its length cap.

Run: uv run pytest scripts/dev/tests/fanout_status/test_fanout_status_lander.py
"""

import json
from pathlib import Path

import pytest

from diagnostics.probe_lib import lander as probe_lander
from dev.fanout_lib import autoland, operator_waits, wait_probe
from dev.fanout_lib.branch_pr import BranchPrs
from dev.fanout_lib.brief import LANDS
from dev.fanout_lib.manifest import Batch, Manifest, save
from dev.fanout_lib.place import main
from dev.fanout_lib.status import (
    AWAIT_LANDER_S,
    AWAITING_LANDER,
    LANDER_LOG,
    LANDER_LOGS,
    LANDER_UNIT,
    NEEDS_INPUT,
    NEXT_STEP,
    PARKED,
    RULINGS,
    collect,
)
from dev.fanout_lib.status_view import one_line
from lib.exit_codes import FANOUT_PLACE_FAILED, FANOUT_PLACE_OK
from _fanout_fakes import fake_tools, ok
from test_fanout_autoland import _Landings, _pr, _record

PR = "https://github.com/DanielH2018/server/pull/1500"
B = Batch("b", "daniel-server", "/w/b", "worktree-fanout-b", "fanout-b", [1], "t")
STOPPED_AT_ITS_PR = (
    "=== b\nActiveState=inactive\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
    f'{{"type":"result","result":"Opened {PR} for #1."}}\n'
)


def _log(verdict: str, rc: int, cause: str = "-", owner: str = "-") -> str:
    hold = "aaaa" if owner != "-" else "-"
    return (
        f"=== 1500\nLAND-RESULT: verdict={verdict} cause={cause} exit={rc} pr=1500 "
        f"sha=bbbbbbbb tags=1 hold_sha={hold} hold_owner={owner} merge=bbbb\n"
        f"VERDICT: {verdict} (detail)\n"
    )


@pytest.mark.parametrize(
    ("lander", "state", "shown"),
    [
        (_log("settled", 0), "landed", "b on daniel-server: landed"),
        (
            _log("deploy-failed", 1, "promote-held", "other"),
            PARKED,
            "parked: merged, unverified behind DEPLOY_HOLD aaaa",
        ),
        (
            _log("deploy-failed", 1, "promote-held", "own"),
            NEEDS_INPUT,
            "VERDICT: deploy-failed (detail)",
        ),
        ("=== 1500\n", "done", f"done ({AWAITING_LANDER})"),
    ],
    ids=["settled", "held-by-another-pr", "held-by-its-own-service", "no-result-yet"],
)
def test_a_server_lander_batch_shows_the_landers_verdict(lander, state, shown):
    """The batch stopped at its PR, so `status` reads the landing's result in one more call."""
    tools, run = fake_tools(branch_prs={B.branch: [PR]})
    run.answers_by_call = [ok(STOPPED_AT_ITS_PR), ok(lander)]
    rows, tier = collect(Manifest("r", "o", [B]), tools, one_line)
    (row,) = rows
    assert (row["state"], shown in row["line"]) == (state, True), row
    attention = state in (PARKED, NEEDS_INPUT)
    assert tier == (FANOUT_PLACE_FAILED if attention else FANOUT_PLACE_OK)
    assert run.host_calls[1][0] == LANDS


def _pass(tmp_path, ran_as="newest", unit="inactive", record=None, **pr) -> str:
    """The lander's reply for PR 1500 with no landing log: what a real pass printed about it.

    Args:
        tmp_path: the pass's scratch state.
        ran_as: the invocation id the pass's lines carry; the unit's own is `newest`.
        unit: the lander unit's ActiveState.
        record: what the batch's review record differs in from one the lander accepts.
        **pr: what the PR differs in from one the lander would land.
    """
    landings = _Landings(
        [_record(1500, batch="b", **(record or {}))],
        [_pr(1500, headRefName=B.branch, **pr)],
    )
    landings.run(tmp_path, dry_run=True)
    entries = "\n".join(
        json.dumps({"MESSAGE": line, "_SYSTEMD_INVOCATION_ID": ran_as})
        for line in landings.lines
    )
    return (
        f"=== 1500\n=== {RULINGS}\nActiveState={unit}\nInvocationID=newest\n{entries}\n"
    )


SURFACE = {"files": [{"path": ".claude/rules/python-layout.md"}]}
HELD = (
    "the server lander will not land #1500; land it with land.sh after probe.py lander"
)


ENDED_AT = 1_000_000


def _collect(
    *lander: str, held=None, age: float = 0, answered: bool = True
) -> tuple[dict, int]:
    """The batch's row and tier, read `age` seconds after it stopped at its PR.

    Args:
        *lander: the lander's reply on daniel-box, joined.
        held: the PRs GitHub holds for the branch; the open PR by default.
        age: how long ago the batch's report was written.
        answered: False for a read of the branch's PRs that failed.
    """
    tools, run = fake_tools(branch_prs={B.branch: (held or [PR]) if answered else None})
    batch = f"{STOPPED_AT_ITS_PR}--- ended\n{ENDED_AT}\n"
    run.answers_by_call = [ok(batch), ok("".join(lander))]
    (row,), tier = collect(
        Manifest("r", "o", [B]), tools, one_line, lambda: ENDED_AT + age
    )
    return row, tier


def test_a_pr_the_lander_holds_for_a_person_reads_as_the_coordinators(tmp_path):
    row, tier = _collect(_pass(tmp_path, **SURFACE))
    assert (row["state"], tier) == (NEEDS_INPUT, FANOUT_PLACE_FAILED)
    assert (
        f"needs input: {HELD}. Its ruling: it changes the lander's own" in row["line"]
    )


def test_a_ruling_the_lander_clears_itself_still_reads_as_awaiting(tmp_path):
    row, tier = _collect(_pass(tmp_path, isDraft=True))
    assert (row["state"], tier) == ("done", FANOUT_PLACE_OK)
    assert f"done ({AWAITING_LANDER}: the PR is a draft)" in row["line"]
    assert row["awaiting_lander"] is True


@pytest.mark.parametrize(
    ("ruled", "step"),
    [
        ({"headRefOid": "b" * 40}, "read the commits pushed since the review"),
        ({"record": {"ci_gate": "failed: pytest"}}, "read its PR's checks"),
        ({"record": {"review_error": "timed out"}}, "review its diff"),
    ],
    ids=["the-head-moved", "the-ci-gate-did-not-pass", "the-review-did-not-complete"],
)
def test_a_ruling_the_lander_repeats_every_pass_reads_as_the_coordinators(
    tmp_path, ruled, step
):
    row, tier = _collect(_pass(tmp_path, **ruled))
    assert (row["state"], tier) == (NEEDS_INPUT, FANOUT_PLACE_FAILED)
    assert f"the server lander will not land #1500; {step}" in row["line"]
    assert row["awaiting_lander"] is False


@pytest.mark.parametrize(
    "lander",
    ["=== 1500\n", ""],
    ids=["the-lander-has-no-log-of-it", "this-user-reads-no-lander-logs"],
)
def test_a_pr_merged_by_a_hand_run_landing_reads_landed(lander):
    row, tier = _collect(lander, held=BranchPrs(merged=(PR,)), age=AWAIT_LANDER_S + 1)
    assert (row["state"], tier) == ("landed", FANOUT_PLACE_OK)
    assert row["line"] == f"b on daniel-server: landed {PR}"


@pytest.mark.parametrize(
    ("age", "lander", "state"),
    [
        (AWAIT_LANDER_S - 1, "=== 1500\n", "done"),
        (AWAIT_LANDER_S + 1, "=== 1500\n", NEEDS_INPUT),
        (AWAIT_LANDER_S + 1, f"=== 1500\n{LANDER_LOG} land1500-1.log\n", "done"),
    ],
    ids=["inside-the-bound", "past-the-bound", "past-it-with-a-landing-running"],
)
def test_a_batch_awaits_the_lander_only_until_the_bound(age, lander, state):
    row, _tier = _collect(lander, age=age)
    assert (row["state"], row["awaiting_lander"]) == (state, state == "done")
    if state == NEEDS_INPUT:
        assert "the server lander has not taken #1500 in 30 minutes" in row["line"]


def test_a_wait_past_the_bound_stays_open_while_github_cannot_be_asked():
    """An unanswered read cannot tell an old wait from a hand landing."""
    row, tier = _collect("=== 1500\n", age=AWAIT_LANDER_S + 1, answered=False)
    assert (row["state"], row["pr_url"], tier) == ("done", PR, FANOUT_PLACE_OK)
    assert (row["awaiting_lander"], row["pr_unconfirmed"]) == (True, True)


def test_a_landing_log_outranks_the_ruling(tmp_path):
    ruling = _pass(tmp_path, **SURFACE).removeprefix("=== 1500\n")
    row, _tier = _collect(_log("settled", 0), ruling)
    assert row["state"] == "landed"


@pytest.mark.parametrize(
    ("unit", "state"),
    [("inactive", "done"), ("activating", NEEDS_INPUT)],
    ids=["the-newest-pass-ended-without-it", "the-newest-pass-still-runs"],
)
def test_a_ruling_from_an_earlier_pass_holds_only_while_the_newest_runs(
    tmp_path, unit, state
):
    """A PR the newest pass did not name has merged or closed, unless that pass is not done."""
    row, _tier = _collect(_pass(tmp_path, ran_as="earlier", unit=unit, **SURFACE))
    assert row["state"] == state


def test_the_wait_probe_wakes_on_a_pr_the_lander_holds(tmp_path):
    save(Manifest("20260101T000010Z", "o", [B]), root=tmp_path / "runs")
    tools, run = fake_tools(branch_prs={B.branch: [PR]})
    run.answers_by_call = [ok(STOPPED_AT_ITS_PR), ok(_pass(tmp_path, **SURFACE))]

    def status(run_id):
        root = ["--manifest-root", str(tmp_path / "runs")]
        return wait_probe.read_status(run_id, lambda argv: main([*argv, *root], tools))

    reading = wait_probe.read(["20260101T000010Z"], status)
    assert reading["state"] == "needs-attention"
    assert HELD in reading["detail"]


def test_the_wait_probe_stays_open_while_the_runs_last_batch_awaits_the_lander(
    tmp_path,
):
    save(Manifest("20260101T000010Z", "o", [B]), root=tmp_path / "runs")
    tools, run = fake_tools(branch_prs={B.branch: [PR]})
    run.answers_by_call = [ok(STOPPED_AT_ITS_PR), ok("=== 1500\n")]

    def status(run_id):
        root = ["--manifest-root", str(tmp_path / "runs")]
        return wait_probe.read_status(run_id, lambda argv: main([*argv, *root], tools))

    assert wait_probe.read(["20260101T000010Z"], status) == {
        "state": "running",
        "detail": "0 running; 0 finished; 1 awaiting the server lander",
    }


def test_runs_shows_a_pr_the_lander_holds_in_place_of_standing(tmp_path, capsys):
    save(Manifest("20260101T000010Z", "o", [B]), root=tmp_path / "runs")
    tools, run = fake_tools()
    run.answers_by_call = [ok(STOPPED_AT_ITS_PR), ok(_pass(tmp_path, **SURFACE))]
    assert main(["runs", "--manifest-root", str(tmp_path / "runs")], tools) == 0
    count, _run, batch = capsys.readouterr().out.splitlines()
    assert count == "1 needs-input"
    assert f"b on daniel-server: {B.branch} #1 needs input: {HELD}" in batch


def test_every_reason_the_lander_holds_a_pr_for_names_a_next_step():
    assert set(NEXT_STEP) == {
        *operator_waits.REASONS,
        *operator_waits.COORDINATOR_REASONS,
    }


def test_status_reads_the_directory_and_the_unit_the_server_lander_writes_to():
    lander_dir = (autoland.STATE_DIR / "logs").relative_to(Path.home())
    assert LANDER_LOGS == f"$HOME/{lander_dir}"
    assert LANDER_UNIT == probe_lander.UNIT
