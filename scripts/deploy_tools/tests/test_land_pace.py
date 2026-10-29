"""--await-merge holds a green head's merge while master CI has a backlog (#5526).

Run: uv run pytest scripts/deploy_tools/tests/test_land_pace.py
"""

import subprocess
from datetime import UTC, datetime


from _land_fakes import Fakes
from deploy_tools.land_lib import merge, pace
from lib.exit_codes import CI_GREEN, CI_PENDING

HEAD = "a" * 40
# The fakes' wall clock.
NOW = 1_000_000.0


def _run(status: str, age_s: int) -> dict[str, str]:
    created = datetime.fromtimestamp(NOW - age_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"status": status, "created_at": created}


# Two master sweeps still running: the 2026-10-24 pile in miniature.
BACKLOG = [_run("queued", 600), _run("in_progress", 1800)]


def _landing(landing, polls: int, master_runs, await_ci=None, **opts):
    """A landing whose head reads green on every one of `polls` polls before MERGED.

    `await_ci` replaces the green answer with one answer per poll.
    """
    views = [{"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": HEAD}] * polls
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": [
                    *views,
                    {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": HEAD},
                ]
            },
            master_runs=master_runs,
            await_ci=await_ci or [(CI_GREEN, "CI green")],
        ),
        **opts,
    )
    ln.direct_merge_subject = "Pace the merges"
    return ln, calls


def _merges(calls) -> int:
    return sum(1 for name, args, _ in calls if name == "gh" and "PUT" in args)


def test_a_master_backlog_holds_the_merge_until_it_drains(landing, capsys):
    ln, calls = _landing(landing, 3, [BACKLOG, BACKLOG, []])
    merge.await_merge(ln)
    names = [c[0] for c in calls]
    # Two held polls, then the merge on the third, after the backlog read that found room.
    assert names.count("gh:master-runs") == 3
    assert _merges(calls) == 1
    assert names.index("gh") > max(
        i for i, n in enumerate(names) if n == "gh:master-runs"
    )
    out = capsys.readouterr().out
    assert out.count("pacing the merge: 2 master CI run(s) unfinished") == 1


def test_completed_and_wedged_runs_do_not_hold_the_merge(landing):
    runs = [
        _run("in_progress", 600),
        _run("completed", 300),
        _run("completed", 900),
        # Unfinished past the 2-hour cutoff: wedged, not queued.
        _run("queued", 3 * 60 * 60),
    ]
    # A cap of 2 admits the one run that counts, so the merge proves the other three are
    # not counted.
    ln, calls = _landing(landing, 1, [runs], pace_master_runs=2)
    merge.await_merge(ln)
    assert _merges(calls) == 1


def test_an_unreadable_backlog_merges_without_pacing(landing, capsys):
    failure = subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")
    ln, calls = _landing(landing, 1, [failure])
    merge.await_merge(ln)
    assert _merges(calls) == 1
    assert "merging without pacing" in capsys.readouterr().out


def test_a_cap_of_zero_turns_pacing_off(landing):
    ln, calls = _landing(landing, 1, [BACKLOG * 3], pace_master_runs=0)
    merge.await_merge(ln)
    assert _merges(calls) == 1
    assert not [c for c in calls if c[0] == "gh:master-runs"]


def test_a_backlog_that_outlasts_merge_timeout_merges_without_pacing(landing, capsys):
    # A backlog that never drains, against a 2s budget: a lander queued behind others (#6466).
    ln, calls = _landing(landing, 3, [BACKLOG], merge_timeout=2, merge_poll=1)
    merge.await_merge(ln)
    assert _merges(calls) == 1
    out = capsys.readouterr().out
    assert "reaching merge_timeout=2s; merging without pacing" in out
    assert "merged after 2s, 2s of it held by pacing" in out


def test_a_head_green_after_ci_spent_the_budget_merges_into_a_backlog(landing, capsys):
    # PR CI pends for the whole budget, then the head goes green behind a master backlog.
    pending = (CI_PENDING, "CI pending")
    ln, calls = _landing(
        landing,
        3,
        [BACKLOG],
        await_ci=[pending, pending, (CI_GREEN, "CI green")],
        merge_timeout=2,
        merge_poll=1,
    )
    merge.await_merge(ln)
    assert _merges(calls) == 1
    assert "merging without pacing" in capsys.readouterr().out


def test_the_cap_rises_by_one_for_each_quarter_hour_held(landing, capsys):
    # Two unfinished runs never drain: a cap of 1 holds, the raised cap of 2 holds, and the
    # cap of 3 reached after half an hour held admits the merge.
    ln, calls = _landing(
        landing, 3, [BACKLOG], merge_poll=pace.RAISE_CAP_EVERY_S, pace_master_runs=1
    )
    merge.await_merge(ln)
    assert _merges(calls) == 1
    out = capsys.readouterr().out
    assert "at or above LAND_PACE_MASTER_RUNS=1;" in out
    assert "at or above 2 (LAND_PACE_MASTER_RUNS=1, raised by 1 after 900s held)" in out


def test_the_cap_reads_from_the_environment(monkeypatch):
    from deploy_tools.land_lib import options

    monkeypatch.setenv(options.PACE_ENV, "0")
    assert options.parse_args(["--pr", "1"], "").pace_master_runs == 0
    monkeypatch.delenv(options.PACE_ENV)
    assert (
        options.parse_args(["--pr", "1"], "").pace_master_runs
        == options.PACE_MASTER_RUNS
    )


def test_by_default_two_unfinished_master_runs_hold_the_merge_and_one_does_not(
    landing, capsys
):
    # The cap of 1 did not raise the share of push runs that skip the sweep (#6309).
    ln, calls = _landing(landing, 2, [BACKLOG, [_run("in_progress", 600)]])
    merge.await_merge(ln)
    assert _merges(calls) == 1
    names = [c[0] for c in calls]
    assert names.count("gh:master-runs") == 2
    out = capsys.readouterr().out
    assert out.count("pacing the merge:") == 1
    assert "pacing the merge: 2 master CI run(s) unfinished" in out
