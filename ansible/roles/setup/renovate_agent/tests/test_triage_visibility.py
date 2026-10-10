"""How a comment-only triage reaches `runs.jsonl` and the digest.

A session that comments on or labels a Renovate PR and leaves it open moves nothing out of
the open set, so the before/after census sees no delta at all. The one field that does move is
`updatedAt`, and these tests hold both halves of reading it: it fires on a moved timestamp,
and it stays silent on an unchanged or unreadable one.

Run: uv run pytest ansible/roles/setup/renovate_agent/tests/test_triage_visibility.py
"""

import json

import agent_logic as al
import renovate_agent

T0 = "2026-09-30T06:00:00Z"
T1 = "2026-09-30T06:40:00Z"

_LISTING = json.dumps(
    [
        {
            "number": 60,
            "title": "r",
            "headRefName": "renovate/x",
            "updatedAt": T0,
            "author": {"login": "app/renovate"},
        }
    ]
)


def _pr(number: int, updated: str = "") -> al.OpenPR:
    return al.OpenPR(number=number, title=f"Update dep {number}", updated_at=updated)


def _result() -> str:
    return json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": "commented on #1",
            "total_cost_usd": 4.5,
            "num_turns": 30,
            "permission_denials": [],
            "terminal_reason": "completed",
        }
    )


class TestTouched:
    def test_a_left_open_pr_whose_timestamp_moved_is_measured(self) -> None:
        moved = al.delta([_pr(1, T0)], [_pr(1, T1)], {})
        assert moved.touched == (1,)
        assert moved.remaining == (1,), "a touched PR is still an open one"

    def test_an_unchanged_timestamp_measures_nothing(self) -> None:
        assert al.delta([_pr(1, T0)], [_pr(1, T0)], {}).touched == ()

    def test_an_unreadable_timestamp_measures_nothing(self) -> None:
        """An empty timestamp is a census that could not read it, so it is no evidence."""
        assert al.delta([_pr(1, "")], [_pr(1, T1)], {}).touched == ()
        assert al.delta([_pr(1, T0)], [_pr(1, "")], {}).touched == ()

    def test_a_merged_pr_is_not_reported_as_touched(self) -> None:
        """`touched` is a subset of the PRs still open, never of the ones that left."""
        moved = al.delta([_pr(1, T0)], [], {1: "MERGED"})
        assert moved.touched == ()
        assert moved.resolved == (1,)


class TestTheCensusAsksForTheField:
    def test_the_listing_requests_updated_at(self) -> None:
        """Without `updatedAt` in the listing, `touched` can never fire."""
        asked: list[str] = []

        def run(argv, cwd=None, timeout=120):
            asked.extend(argv)
            return 0, _LISTING

        prs = renovate_agent.open_prs("o/r", renovate_agent.AgentTools(run=run))
        assert any("updatedAt" in arg for arg in asked)
        assert prs[0].updated_at == T0


class TestTheRecordAndTheDigest:
    def test_a_triaged_pr_is_recorded(self) -> None:
        """The record is the session's whole trace when nothing left the open set."""
        rec = json.loads(
            al.run_record(100, "ran", "", al.delta([_pr(1, T0)], [_pr(1, T1)], {}))
        )
        assert rec["touched"] == [1] and rec["left_open"] == [1]
        assert rec["merged"] == []

    def test_a_triaged_pr_left_open_is_named_with_its_caveat(self) -> None:
        """The digest says the PR changed, never that the session is what changed it."""
        text = al.render_digest(
            al.parse_run(_result(), 0, False),
            al.delta([_pr(1, T0)], [_pr(1, T1)], {}),
            "daniel-box",
            "/log",
        )
        assert "still open: #1" in text
        assert "left open but updated during the run" in text
        assert "Renovate rebase" in text

    def test_an_untouched_pr_gets_no_updated_line(self) -> None:
        text = al.render_digest(
            al.parse_run(_result(), 0, False),
            al.delta([_pr(1, T0)], [_pr(1, T0)], {}),
            "daniel-box",
            "/log",
        )
        assert "left open but updated during the run" not in text
