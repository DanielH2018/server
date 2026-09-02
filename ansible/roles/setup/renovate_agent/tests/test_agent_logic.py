"""Decision tests for the unattended Renovate agent's pure logic.

Every rule is a `..._is_clean` / `..._is_flagged` pair: a guard that fires on everything and a
guard that fires on nothing are indistinguishable from the passing side alone.

Run: uv run pytest ansible/roles/setup/renovate_agent/tests/test_agent_logic.py
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import agent_logic as al


def _pr(number: int) -> al.OpenPR:
    return al.OpenPR(number=number, title=f"Update dep {number}", url=f"u/{number}")


def _result(**over) -> str:
    obj = {
        "type": "result",
        "is_error": False,
        "result": "landed #1",
        "total_cost_usd": 4.5,
        "num_turns": 30,
        "permission_denials": [],
        "terminal_reason": "completed",
    }
    obj.update(over)
    return json.dumps(obj)


class TestDecide:
    def test_a_backlog_with_no_hold_is_clean(self) -> None:
        gate = al.decide([_pr(1), _pr(2)], "", "")
        assert gate.run
        assert "2 open" in gate.reason

    def test_an_empty_backlog_is_flagged_quietly(self) -> None:
        """The steady state must not post — a daily 'nothing to do' trains the channel away."""
        gate = al.decide([], "", "")
        assert not gate.run
        assert gate.quiet

    def test_a_hold_is_flagged_loudly(self) -> None:
        gate = al.decide([_pr(1)], "deadbeefcafe\n", "")
        assert not gate.run
        assert not gate.quiet
        assert "deadbeef" in gate.reason

    def test_a_hold_outranks_an_empty_backlog(self) -> None:
        """A held host is a condition to clear even when there is nothing else to do."""
        assert not al.decide([], "deadbeefcafe", "").quiet

    def test_a_hold_plane_names_the_playbook(self) -> None:
        gate = al.decide([_pr(1)], "deadbeefcafe", "k3s-bringup.yml")
        assert "k3s-bringup.yml" in gate.reason


class TestParseRun:
    def test_a_clean_result_object_is_clean(self) -> None:
        out = al.parse_run(_result(), 0, False)
        assert out.ok
        assert out.summary == "landed #1"
        assert out.cost_usd == 4.5
        assert out.turns == 30

    def test_warnings_before_the_result_object_are_clean(self) -> None:
        """Claude Code prints a stdin warning before the JSON; the parse scans, not loads."""
        noisy = "Warning: no stdin data received in 3s\n" + _result()
        assert al.parse_run(noisy, 0, False).ok

    def test_an_error_result_is_flagged(self) -> None:
        out = al.parse_run(
            _result(is_error=True, terminal_reason="budget_exceeded"), 0, False
        )
        assert not out.ok
        assert out.error == "budget_exceeded"

    def test_a_nonzero_exit_is_flagged_even_with_a_clean_object(self) -> None:
        assert not al.parse_run(_result(), 1, False).ok

    def test_a_timeout_is_flagged_without_parsing(self) -> None:
        out = al.parse_run(_result(), 0, True)
        assert not out.ok
        assert "timeout" in out.error

    def test_missing_json_is_flagged(self) -> None:
        out = al.parse_run("claude: command not found\n", 127, False)
        assert not out.ok
        assert "no result JSON" in out.error

    def test_denials_are_carried_through(self) -> None:
        """A denied write is the failure the whole design rests on not happening."""
        out = al.parse_run(
            _result(permission_denials=[{"tool_name": "Bash"}]), 0, False
        )
        assert out.denials == ("Bash",)


class TestDelta:
    def test_a_resolved_pr_is_measured(self) -> None:
        moved = al.delta([_pr(1), _pr(2)], [_pr(2)])
        assert moved.resolved == (1,)
        assert moved.remaining == (2,)

    def test_an_unchanged_set_measures_nothing_resolved(self) -> None:
        moved = al.delta([_pr(1)], [_pr(1)])
        assert moved.resolved == ()
        assert moved.remaining == (1,)

    def test_a_pr_opened_during_the_run_is_not_counted_as_remaining(self) -> None:
        moved = al.delta([_pr(1)], [_pr(1), _pr(9)])
        assert moved.opened == (9,)
        assert moved.remaining == (1,)


class TestRenderDigest:
    def test_a_run_that_moved_nothing_is_flagged(self) -> None:
        """`is_error: false` means the process ended, not that any PR moved."""
        text = al.render_digest(
            al.parse_run(_result(result="I reviewed everything carefully."), 0, False),
            al.delta([_pr(1)], [_pr(1)]),
            "daniel-box",
            "/var/lib/renovate-agent/last_session.json",
        )
        assert "no Renovate PR changed state" in text
        assert "⚠️" in text

    def test_a_run_that_resolved_a_pr_is_clean(self) -> None:
        text = al.render_digest(
            al.parse_run(_result(), 0, False),
            al.delta([_pr(1), _pr(2)], [_pr(2)]),
            "daniel-box",
            "/log",
        )
        assert "resolved #1" in text
        assert "still open: #2" in text

    def test_a_failed_run_leads_with_the_failure(self) -> None:
        text = al.render_digest(
            al.parse_run("", 0, True),
            al.delta([_pr(1)], [_pr(1)]),
            "daniel-box",
            "/log",
        )
        assert text.startswith("🚨")

    def test_denials_reach_the_digest(self) -> None:
        text = al.render_digest(
            al.parse_run(_result(permission_denials=[{"tool_name": "Edit"}]), 0, False),
            al.delta([_pr(1)], []),
            "daniel-box",
            "/log",
        )
        assert "permission denials: Edit" in text

    def test_the_digest_fits_discord(self) -> None:
        """host_lib.discord_post truncates at 1900 chars; the delta lines must survive."""
        text = al.render_digest(
            al.parse_run(_result(result="x" * 5000), 0, False),
            al.delta([_pr(1)], []),
            "daniel-box",
            "/log",
        )
        assert len(text) < 1900
        assert "resolved #1" in text
