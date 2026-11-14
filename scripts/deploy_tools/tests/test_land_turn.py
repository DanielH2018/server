"""With LAND_QUEUE, --await-merge holds the landing turn from the PR's CI run to its merge.

Run: uv run pytest scripts/deploy_tools/tests/test_land_turn.py
"""

import os

import pytest

from _land_fakes import Fakes
from deploy_tools.land_lib import merge, options, turn
from deploy_tools.land_lib.outcome import Outcome
from lib.exit_codes import CI_RED

HEAD = "a" * 40
OPEN = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": HEAD}
MERGED = {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": HEAD}
# One unfinished master run above a cap of 1: pacing holds a landing that reads it.
BACKLOG = [{"status": "queued", "created_at": "1970-01-12T13:40:00Z"}]


def _landing(landing, draft: bool = True, mergeable=None, **fakes_or_opts):
    """A landing with LAND_QUEUE whose head reads green on its first poll."""
    fakes = {
        k: fakes_or_opts.pop(k)
        for k in ("turn", "await_ci", "gh_merge_rc", "master_runs")
        if k in fakes_or_opts
    }
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": [OPEN, MERGED],
                "isDraft": {"isDraft": draft},
                "mergeable": mergeable or {"mergeable": "MERGEABLE"},
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
        if name == "gh":
            names.append("merge" if "PUT" in args else " ".join(args[:2]))
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


def test_marking_ready_waits_for_github_to_settle_whether_the_pr_merges(landing):
    unknown = {"mergeable": "UNKNOWN"}
    ln, calls = _landing(
        landing, mergeable=[unknown, unknown, {"mergeable": "MERGEABLE"}]
    )
    _land(ln)
    names = [c[0] if c[0] != "gh" else " ".join(c[1][:2]) for c in calls]
    ready = names.index("pr ready")
    assert names[:ready].count("gh:mergeable") == 3
    assert [c[1] for c in calls[:ready] if c[0] == "sleep"] == [
        (turn.MERGEABLE_POLL_S,)
    ] * 2


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
            "isDraft": {"isDraft": True},
            "mergeable": {"mergeable": "MERGEABLE"},
        },
        master_runs=[BACKLOG * 2],
    )
    rc, out, _, calls, _ = land_run(["--arm-merge", "--await-merge"], fakes)
    assert rc == 0, out
    # No read of the master backlog: a landing that holds the turn is not paced.
    assert _order(calls)[:4] == ["try_turn", "pr ready", "merge", "release_turn"]


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
