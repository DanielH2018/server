"""With LAND_QUEUE, --await-merge holds the landing turn from the PR's CI run to its merge.

Run: uv run pytest scripts/deploy_tools/tests/test_land_turn.py
"""

import os
from dataclasses import replace

import pytest

from _land_fakes import Fakes
from deploy_tools.land_lib import merge, options, turn
from deploy_tools.land_lib.outcome import Outcome
from lib.exit_codes import CI_RED
from lib.proc_testing import fake_bin, path_with

HEAD = "a" * 40
OPEN = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": HEAD}
MERGED = {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": HEAD}
# The head a branch update makes.
UPDATED = "b" * 40
# The fields `merge._start_run` reads before the first poll.
START_VIEW = "isDraft,state,mergeable,headRefOid"
# Master moved on a path the PR leaves alone, so only the turn's own rule updates the branch.
MASTER_MOVED = [["docs/other.md"]]
# One unfinished master run above a cap of 1: pacing holds a landing that reads it.
BACKLOG = [{"status": "queued", "created_at": "1970-01-12T13:40:00Z"}]


def _landing(landing, polls=(OPEN, MERGED), start=OPEN, **fakes_or_opts):
    """A draft's landing with LAND_QUEUE; its head reads green on each of `polls` that is open.

    `start` is the PR as the landing reads it before its first poll.
    """
    fakes = {
        k: fakes_or_opts.pop(k)
        for k in (
            "turn",
            "await_ci",
            "gh_merge_rc",
            "master_runs",
            "compare",
            "head_runs",
        )
        if k in fakes_or_opts
    }
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": list(polls),
                START_VIEW: {"isDraft": True, **start},
            },
            **fakes,
        ),
        **{"queue": True, **fakes_or_opts},
    )
    ln.direct_merge_subject = "Take turns"
    return ln, calls


def _land(ln) -> None:
    """The merge wait as the pipeline runs it."""
    with turn.held(ln) as in_turn:
        merge.await_merge(ln, in_turn)


def _order(calls) -> list[str]:
    """The calls that matter to the turn, in order, each under a short name."""
    names = []
    for name, args, _ in calls:
        if name == "gh" and "PUT" in args:
            names.append("update" if args[3].endswith("/update-branch") else "merge")
        elif name == "gh":
            names.append(" ".join(args[:2]))
        elif name in ("try_turn", "release_turn", "gh:master-runs"):
            names.append(name)
    return names


def test_a_landing_waits_while_another_holds_the_turn(landing, capsys):
    ln, _ = _landing(landing, turn=[False, False, True], merge_poll=7)
    _land(ln)
    out = capsys.readouterr().out
    assert out.count("waiting for the landing turn") == 1
    assert "took the landing turn after 14s" in out


def test_a_turn_that_never_comes_leaves_a_paced_merge_of_a_ready_pr(landing, capsys):
    ln, calls = _landing(landing, turn=[False], merge_timeout=2, merge_poll=1)
    _land(ln)
    order = _order(calls)
    # The draft is still marked ready, the merge is paced again, and no turn is given up.
    assert order[-3:] == ["pr ready", "gh:master-runs", "merge"]
    assert "release_turn" not in order
    assert (
        "reaching merge_timeout=2s; going on without a turn" in capsys.readouterr().out
    )


def test_a_lock_that_cannot_be_opened_does_not_stop_the_landing(landing, capsys):
    ln, calls = _landing(landing, turn=[PermissionError("denied")])
    _land(ln)
    assert _order(calls)[-1] == "merge"
    assert "(denied); going on without a turn" in capsys.readouterr().out


def test_the_turn_is_given_up_when_the_landing_ends_without_merging(landing):
    ln, calls = _landing(landing, await_ci=[(CI_RED, "CI red")])
    with pytest.raises(Outcome):
        _land(ln)
    assert _order(calls)[-1] == "release_turn"


def test_a_draft_that_cannot_be_marked_ready_ends_the_landing_with_the_remedy(
    landing,
):
    ln, calls = _landing(landing, gh_merge_rc=[1])
    with pytest.raises(Outcome) as exc:
        _land(ln)
    assert exc.value.rc == 1
    assert "could not be marked ready (boom); run `gh pr ready 999`" in exc.value.error
    assert _order(calls)[-1] == "release_turn"


def test_land_queue_in_the_environment_puts_the_whole_landing_in_turn(
    land_run, monkeypatch
):
    monkeypatch.setenv(options.QUEUE_ENV, "1")
    fakes = Fakes(
        gh_views={
            "state,title,body,reviewDecision": {
                "state": "OPEN",
                "title": "Take turns",
                "body": "",
            },
            "headRefOid": {"headRefOid": HEAD},
            "state,mergeable,headRefOid": [OPEN, MERGED],
            START_VIEW: {"isDraft": True, **OPEN},
        },
        master_runs=[BACKLOG * 2],
    )
    rc, out, _, calls, _ = land_run(["--arm-merge", "--await-merge"], fakes)
    assert rc == 0, out
    # No read of the master backlog: a landing that holds the turn is not paced.
    assert _order(calls)[:4] == ["try_turn", "pr ready", "merge", "release_turn"]


def _merged_sha(calls) -> str:
    args = next(
        c[1] for c in calls if c[0] == "gh" and any(a.endswith("/merge") for a in c[1])
    )
    return next(a for a in args if a.startswith("sha=")).removeprefix("sha=")


def test_in_turn_a_head_with_no_ci_run_is_updated_from_master_before_its_run(landing):
    updated = {**OPEN, "headRefOid": UPDATED}
    ln, calls = _landing(
        landing,
        polls=(OPEN, updated, {**MERGED, "headRefOid": UPDATED}),
        compare=MASTER_MOVED,
    )
    _land(ln)
    assert _order(calls) == ["try_turn", "pr ready", "update", "merge", "release_turn"]
    assert _merged_sha(calls) == UPDATED


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"head_runs": [{"id": 2, "status": "in_progress"}]}, id="a-run"),
        pytest.param({"start": {**OPEN, "mergeable": "CONFLICTING"}}, id="a-conflict"),
        pytest.param({"approved_head": HEAD}, id="an-approved-head"),
        pytest.param(
            {"turn": [False], "merge_timeout": 2, "merge_poll": 1}, id="no-turn"
        ),
    ],
)
def test_the_branch_is_left_alone_and_the_head_merges_as_it_would_without_a_turn(
    landing, case
):
    approved = case.pop("approved_head", "")
    ln, calls = _landing(landing, compare=MASTER_MOVED, **case)
    ln.approved_head = approved
    _land(ln)
    assert "update" not in _order(calls)
    assert _merged_sha(calls) == HEAD


def test_a_head_that_moved_after_the_policy_pinned_it_is_refused_and_not_updated(
    landing,
):
    ln, calls = _landing(landing, compare=MASTER_MOVED)
    ln.pinned_head = "d" * 40
    with pytest.raises(Outcome) as exc:
        _land(ln)
    assert "after the landing policy checked it" in exc.value.error
    assert "update" not in _order(calls)


def test_an_update_github_refuses_leaves_the_pr_the_run_it_has(landing, capsys):
    # gh answers: `pr ready`, then the refused update, then the merge.
    ln, calls = _landing(landing, compare=MASTER_MOVED, gh_merge_rc=[0, 1, 0])
    _land(ln)
    assert _order(calls)[-3:] == ["update", "merge", "release_turn"]
    assert _merged_sha(calls) == HEAD
    assert "going on with the run the PR has" in capsys.readouterr().out


def test_a_pr_merged_while_the_landing_waited_for_its_turn_is_not_touched(landing):
    ln, calls = _landing(landing, polls=(MERGED,), start=MERGED, compare=MASTER_MOVED)
    _land(ln)
    assert _order(calls) == ["try_turn", "release_turn"]


@pytest.mark.parametrize(
    ("queue", "subject", "expected"),
    [
        pytest.param(False, "No turn", ["gh:master-runs", "merge"], id="no-land-queue"),
        pytest.param(True, "", [], id="a-merge-made-elsewhere"),
    ],
)
def test_a_landing_takes_no_turn_without_land_queue_or_a_merge_of_its_own(
    landing, queue, subject, expected
):
    ln, calls = landing(
        Fakes(gh_views={"state,mergeable,headRefOid": [OPEN, MERGED]}), queue=queue
    )
    ln.direct_merge_subject = subject
    _land(ln)
    assert _order(calls) == expected


def test_the_lock_admits_one_holder_until_it_gives_the_turn_up(tmp_path):
    lock = tmp_path / "turn.lock"
    give_up = turn.try_take(lock)
    assert give_up is not None
    assert turn.try_take(lock) is None
    give_up()
    again = turn.try_take(lock)
    assert again is not None
    again()


def test_the_lock_file_is_readable_by_every_user_whatever_the_umask(tmp_path):
    lock = tmp_path / "turn.lock"
    old = os.umask(0o077)
    try:
        give_up = turn.try_take(lock)
    finally:
        os.umask(old)
    assert give_up is not None
    give_up()
    assert lock.stat().st_mode & 0o777 == 0o644


@pytest.fixture
def ssh_here(tmp_path, monkeypatch):
    """An `ssh` that runs the command it is given on this host, in place of the turn host."""
    stubs = fake_bin(
        tmp_path / "bin", ssh='#!/usr/bin/env bash\nexec sh -c "${@: -1}"\n'
    )
    monkeypatch.setenv("PATH", path_with(stubs))


def test_a_landing_on_another_host_holds_the_same_lock_over_ssh(tmp_path, ssh_here):
    lock = tmp_path / "turn.lock"
    give_up = turn.try_take_over_ssh(lock)
    assert give_up is not None
    # The lock a landing on the turn host takes is the one the ssh session holds.
    assert turn.try_take(lock) is None
    give_up()
    here = turn.try_take(lock)
    assert here is not None
    assert turn.try_take_over_ssh(lock) is None
    here()


def test_a_turn_host_that_does_not_answer_raises_oserror(tmp_path, monkeypatch):
    stubs = fake_bin(
        tmp_path / "bin",
        ssh="#!/usr/bin/env bash\necho 'Warning: added a key' >&2\necho 'ssh: no route' >&2\nexit 255\n",
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    with pytest.raises(OSError, match="ssh daniel-box: ssh: no route"):
        turn.try_take_over_ssh(tmp_path / "turn.lock")


def test_a_landing_off_the_turn_host_takes_the_turn_over_ssh(
    landing, tmp_path, monkeypatch, capsys
):
    seen = tmp_path / "ssh-args"
    stubs = fake_bin(
        tmp_path / "bin",
        ssh=f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {seen}\necho held\ncat >/dev/null\n",
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    ln, _ = _landing(landing)
    ln.tools = replace(ln.tools, try_turn=None, hostname=lambda: "daniel-server")
    _land(ln)
    assert turn.TURN_HOST in seen.read_text().splitlines()
    assert "took the landing turn after 0s" in capsys.readouterr().out


@pytest.mark.parametrize("hostname", [turn.TURN_HOST, f"{turn.TURN_HOST}.lan"])
def test_a_landing_on_the_turn_host_locks_the_file_itself(hostname):
    # By value, not by effect: taking the turn here would lock the host's real file.
    assert turn._taker(hostname) is turn.try_take
