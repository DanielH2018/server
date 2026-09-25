"""The render-record producer's decisions: which commit it renders, and when its tile is green.

Run: uv run pytest ansible/roles/setup/render_records/tests
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import render_records as rr

TIP, MID, OLD = "a" * 40, "b" * 40, "c" * 40
STARTED = "2026-09-25T23:00:00Z"


def _verdicts(**by_sha):
    asked = []

    def verdict(sha):
        asked.append(sha)
        return by_sha.get(sha, "pending")

    return verdict, asked


def test_a_green_tip_is_rendered_without_walking():
    verdict, asked = _verdicts(**{TIP: "pass", MID: "pass"})
    assert rr.choose_green([TIP, MID, OLD], verdict, 10, True) == TIP
    assert asked == [TIP]


def test_a_pending_tip_walks_past_a_red_ancestor_to_the_newest_green_one():
    verdict, _ = _verdicts(**{TIP: "pending", MID: "fail", OLD: "pass"})
    assert rr.choose_green([TIP, MID, OLD], verdict, 10, True) == OLD


def test_an_unauthenticated_host_does_not_walk():
    verdict, asked = _verdicts(**{TIP: "pending", MID: "pass"})
    assert rr.choose_green([TIP, MID], verdict, 10, False) is None
    assert asked == [TIP]


def _record(**overrides):
    return {
        "commit": TIP,
        "tree_dirty": False,
        "host": "daniel-box",
        "rendered_at": "2026-09-25T23:03:00Z",
        **overrides,
    }


def test_a_record_refreshed_by_this_run_at_the_chosen_commit_is_clean():
    problems = rr.record_problems(
        ["sonarr"], {"sonarr": _record()}, TIP, "daniel-box", STARTED
    )
    assert problems == {}


def test_every_record_this_run_did_not_refresh_is_named():
    records = {
        "missing": None,
        "old-commit": _record(commit=MID),
        "dirty": _record(tree_dirty=True),
        "elsewhere": _record(host="daniel-server"),
        "last-hour": _record(rendered_at="2026-09-25T22:03:00Z"),
    }
    problems = rr.record_problems(sorted(records), records, TIP, "daniel-box", STARTED)
    assert problems == {
        "missing": "no record",
        "old-commit": f"commit {MID[:8]}",
        "dirty": "dirty tree",
        "elsewhere": "host daniel-server",
        "last-hour": "not refreshed by this run",
    }


def test_the_tile_goes_green_only_with_no_problems():
    assert rr.verdict_message(["a", "b"], {}, TIP, 0) == (
        "up",
        f"2 render records at {TIP[:8]}",
    )
    status, message = rr.verdict_message(["a", "b"], {"b": "no record"}, TIP, 0)
    assert status == "down"
    assert "1 of 2" in message and "b (no record)" in message
