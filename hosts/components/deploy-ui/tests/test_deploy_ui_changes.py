"""The change view: one row per change, joined from the batch, the PR list and the landing.

The fan-out batch comes from the real `fanout.py board` over a manifest on disk, and the
landing from land_lib's own record writer, so each source is its owner's output.
"""

import contextlib
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from deploy_tools.land_lib import line
from deploy_tools.land_lib.core.landing_state import State
from deploy_tools.land_lib.outcome import Next
from deploy_tools.land_lib.transitions import Transitions
from dev.fanout_lib import batch_board
from dev.fanout_lib.manifest import Batch, Manifest, save
from dev.fanout_lib.transport import Tools

import deploy_ui
from deploy_ui_lib import deploy_ui_changes as changes
from deploy_ui_lib import deploy_ui_states as states

NOW = 1_764_300_000.0
BRANCH = "worktree-fanout-10577"
# What the batch's host prints for `board_command`: the pipeline is in its land phase, so the
# unit is active and `.fanout/phase` still names the review that ran before it.
HOST_REPLY = (
    f"--- board 10577\nphase review\nphase_since {int(NOW - 600)}\nresult \n"
    "=== 10577\nActiveState=active\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
)
PR_ROW = {
    "number": 4242,
    "title": "The deploy UI follows a change",
    "headRefName": BRANCH,
    "isDraft": True,
    "statusCheckRollup": [],
    "createdAt": "2025-11-28T00:00:00Z",
}


def _landing(directory, pr, entered):
    clock = SimpleNamespace(t=0.0)
    tools = SimpleNamespace(
        now=lambda: clock.t,
        logger=lambda _text: None,
        hostname=lambda: "daniel-box",
        line=line.here(directory),
    )
    writer = Transitions(pr, tools)
    for state, at in entered:
        clock.t = at
        writer.enter(state)
    return writer


def _board(manifests: Path, reply: str) -> subprocess.CompletedProcess:
    """`fanout.py board` run for real over `manifests`, its host answering `reply`."""
    host = lambda *_a, **_k: subprocess.CompletedProcess([], 0, reply, "")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = batch_board.main(["--manifest-root", str(manifests)], Tools(run=host))
    return subprocess.CompletedProcess([], rc, out.getvalue(), "")


@pytest.fixture
def make_app(tmp_path):
    (tmp_path / "proc_locks").write_text("")
    (tmp_path / "run_lock").mkdir()
    manifests = tmp_path / "fanout"
    manifests.mkdir()
    save(
        Manifest(
            "20251128T000000Z",
            "worktree-orch",
            [
                Batch(
                    "10577",
                    "daniel-box",
                    "/w/fanout-10577",
                    BRANCH,
                    "fanout-10577",
                    [10577],
                    "2025-11-27T23:00:00+00:00",
                )
            ],
        ),
        root=manifests,
    )

    def make(board_fails: bool = False):
        def run(argv, **_kw):
            if argv[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps([PR_ROW]), "")
            if argv[-2:] == ["scripts/dev/fanout.py", "board"]:
                if board_fails:
                    return subprocess.CompletedProcess(argv, 1, "", "manifest locked")
                return _board(manifests, HOST_REPLY)
            return subprocess.CompletedProcess(argv, 0, "", "")

        cfg = deploy_ui.Config(
            repo=tmp_path,
            log_dir=tmp_path / "logs",
            bind="127.0.0.1",
            port=0,
            tree_lock=tmp_path / "tree.lock",
            proc_locks=tmp_path / "proc_locks",
            cli_log_dir=tmp_path / "cli",
            land_locks=tmp_path / "run_lock",
            hostname="daniel-box",
        )
        return deploy_ui.App(cfg, run=run, now=lambda: NOW)

    return make


def _records(tmp_path) -> Path:
    return tmp_path / "run_lock" / states.LINE_DIR_NAME


def test_a_batch_its_pr_and_its_landing_are_one_row_in_the_landing_stage_is_clean(
    tmp_path, make_app
):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Changes
    _landing(
        _records(tmp_path),
        "4242",
        [
            (State.QUEUED, NOW - 300),
            (State.HOLDING, NOW - 200),
            (State.TESTING, NOW - 90),
        ],
    )
    body = make_app().change_view.changes()
    assert body["batches_unavailable"] == ""
    (row,) = body["changes"]
    assert {
        k: row[k] for k in ("key", "title", "stage", "state", "label", "in_state_s")
    } == {
        "key": "pr:4242",
        "title": "The deploy UI follows a change",
        "stage": "landing",
        "state": "testing",
        "label": "Testing",
        "in_state_s": 90,
    }
    assert row["batch"] == {"id": "10577", "host": "daniel-box", "state": "running"}


def test_a_failed_batch_read_is_named_and_leaves_the_pr_row_is_flagged(
    make_app,
):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Changes
    body = make_app(board_fails=True).change_view.changes()
    assert "manifest locked" in body["batches_unavailable"]
    (row,) = body["changes"]
    assert (row["stage"], row["label"], row["batch"]) == (
        "waiting to land",
        "draft, CI none",
        None,
    )


def test_an_ended_landing_names_the_next_action_its_land_result_gives_is_clean(
    tmp_path, make_app
):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Changes
    _landing(_records(tmp_path), "4242", [(State.TESTING, NOW - 400)]).end("pr-ci-red")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "land-20251128-120000.log").write_text(
        "VERDICT: pr-ci-red (PR #4242)\n"
        "LAND-RESULT: verdict=pr-ci-red cause=- exit=1 pr=4242 next=fix-and-rerun\n"
    )
    app = make_app()
    # The batch's board row carries no LAND-RESULT line, so the daemon's log index answers.
    (row,) = app.change_view.changes()["changes"]
    assert (row["stage"], row["state"], row["next"], row["waits_on"]) == (
        "ended",
        "pr-ci-red",
        "fix-and-rerun",
        changes.NEXT_LABELS["fix-and-rerun"],
    )


def test_every_stage_and_state_name_the_page_shows_is_its_owners_is_clean():
    # fact: hosts/components/deploy-ui/CLAUDE.md#Changes
    landing, deploying = changes.LANDING_STATES, changes.DEPLOYING_STATES
    assert not landing & deploying
    assert landing | deploying | {states.VERDICT} == set(State)
    assert set(changes.NEXT_LABELS) == set(Next)
