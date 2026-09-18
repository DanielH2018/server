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
import pytest
import renovate_agent


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


class TestConfigGuard:
    """`main()` indexes config keys directly, so a missing one must name itself.

    Without the guard the first armed tick dies on a bare `KeyError: 'REPO_DIR'` before any
    Discord post exists to carry the reason.
    """

    def test_a_complete_config_is_clean(self, tmp_path) -> None:
        cfg = tmp_path / "config.env"
        cfg.write_text("REPO=o/r\nREPO_DIR=/repo\nPROMPT_FILE=/p.txt\n")
        assert renovate_agent.main(_tools(_FakeHost(prs=[])), str(cfg)) == 0

    def test_a_missing_key_is_flagged_by_name(self, tmp_path) -> None:
        cfg = tmp_path / "config.env"
        cfg.write_text("REPO=o/r\n")
        with pytest.raises(RuntimeError, match="REPO_DIR, PROMPT_FILE"):
            renovate_agent.main(config_path=str(cfg))


class _FakeHost:
    """Answers every process `main()` reaches before it would spend a session.

    `prs` is what `gh pr list` returns; `ahead` is what `rev-list --count` says the run
    branch holds beyond origin/master, and `contained` whether `merge-tree` reproduces
    master's tree for it. The run worktree is always registered and clean.
    """

    def __init__(self, prs: list[int], ahead: int = 0, contained: bool = False) -> None:
        self.prs = prs
        self.ahead = ahead
        self.contained = contained
        self.posts: list[str] = []

    def run(self, argv, cwd=None, timeout=120):
        if argv[0] == "gh":
            return 0, json.dumps(
                [
                    {"number": n, "title": f"Update dep {n}", "url": f"u/{n}"}
                    for n in self.prs
                ]
            )
        if "list" in argv and "--porcelain" in argv:
            return (
                0,
                f"worktree {argv[2]}/.claude/worktrees/renovate-auto\nHEAD abc\n\n",
            )
        if "status" in argv:
            return 0, ""
        if "rev-list" in argv:
            return 0, f"{self.ahead}\n"
        if "rev-parse" in argv:
            return 0, "0123abcd\n"
        if "merge-tree" in argv:
            return 0, "0123abcd\n" if self.contained else "89abcdef\n"
        raise AssertionError(
            f"main() reached a process this test does not answer: {argv}"
        )

    def discord_post(self, webhook, text, ua, log=None):
        self.posts.append(text)
        return True


def _tools(host: _FakeHost) -> renovate_agent.AgentTools:
    return renovate_agent.AgentTools(
        run=host.run, discord_post=host.discord_post, read_file=lambda path: ""
    )


class TestSkipExitCodes:
    """The Alive beat is the unit's ExecStartPost, so it fires on any exit 0 — including a
    skip that means the tree is stuck. That laundered five daily skips behind a green tile
    from 2026-09-14 (#2014). A blocked tree exits non-zero: no beat, and OnFailure pages. The
    quiet no-PRs skip is the healthy steady state and keeps beating.
    """

    def _cfg(self, tmp_path) -> str:
        (tmp_path / ".claude" / "worktrees" / "renovate-auto").mkdir(parents=True)
        cfg = tmp_path / "config.env"
        cfg.write_text(f"REPO=o/r\nREPO_DIR={tmp_path}\nPROMPT_FILE=/p.txt\n")
        return str(cfg)

    def test_a_worktree_blocked_skip_is_flagged(self, tmp_path) -> None:
        host = _FakeHost(prs=[1], ahead=1, contained=False)

        rc = renovate_agent.main(_tools(host), self._cfg(tmp_path))

        assert rc == renovate_agent.EXIT_WORKTREE_BLOCKED != 0
        assert any("holds 1 commit(s) not on origin/master" in p for p in host.posts)

    def test_an_empty_backlog_skip_is_clean(self, tmp_path) -> None:
        host = _FakeHost(prs=[])

        assert renovate_agent.main(_tools(host), self._cfg(tmp_path)) == 0
        assert host.posts == []
