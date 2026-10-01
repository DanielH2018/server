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
        moved = al.delta([_pr(1), _pr(2)], [_pr(2)], {1: "MERGED"})
        assert moved.resolved == (1,)
        assert moved.remaining == (2,)

    def test_an_unchanged_set_measures_nothing_resolved(self) -> None:
        moved = al.delta([_pr(1)], [_pr(1)], {})
        assert moved.resolved == ()
        assert moved.remaining == (1,)

    def test_a_pr_closed_without_merging_is_not_resolved(self) -> None:
        moved = al.delta([_pr(1), _pr(2)], [], {1: "MERGED", 2: "CLOSED"})
        assert moved.resolved == (1,)
        assert moved.closed == (2,)

    def test_a_pr_whose_state_was_not_read_is_not_resolved(self) -> None:
        moved = al.delta([_pr(1)], [], {})
        assert moved.resolved == ()
        assert moved.unread == (1,)

    def test_a_pr_opened_during_the_run_is_not_counted_as_remaining(self) -> None:
        moved = al.delta([_pr(1)], [_pr(1), _pr(9)], {})
        assert moved.opened == (9,)
        assert moved.remaining == (1,)


def _own_pr() -> al.OpenPR:
    """The one PR in `_LISTING` authored by the account the agent runs as."""
    return al.OpenPR(
        number=50, title="t", branch="b-1", updated_at="2026-09-30T06:10:00Z"
    )


def _own(number: int, branch: str) -> al.OpenPR:
    return al.OpenPR(number=number, title="Finish bump", branch=branch)


class TestHandedOff:
    """The superseding PR is the session account's, so app/renovate's census misses it."""

    RUN = "worktree-renovate-auto"

    def test_a_pr_opened_on_a_run_branch_is_listed(self) -> None:
        after = [_own(50, f"{self.RUN}-2744"), _own(51, self.RUN)]
        assert al.handed_off([], after, self.RUN) == (50, 51)

    def test_a_pr_open_before_the_run_is_not_listed(self) -> None:
        earlier = [_own(50, f"{self.RUN}-2744")]
        assert al.handed_off(earlier, earlier, self.RUN) == ()

    def test_an_interactive_sessions_pr_is_not_listed(self) -> None:
        """Interactive sessions share the account and the `worktree-renovate-` prefix."""
        after = [_own(60, "worktree-renovate-n8n-2-40-7"), _own(61, f"{self.RUN}x")]
        assert al.handed_off([], after, self.RUN) == ()

    def test_a_failed_census_is_flagged_not_empty(self) -> None:
        assert al.handed_off(None, [], self.RUN) is None
        assert al.handed_off([], None, self.RUN) is None


class TestRenderDigest:
    def test_a_run_that_moved_nothing_is_flagged(self) -> None:
        """`is_error: false` means the process ended, not that any PR moved."""
        text = al.render_digest(
            al.parse_run(_result(result="I reviewed everything carefully."), 0, False),
            al.delta([_pr(1)], [_pr(1)], {}),
            "daniel-box",
            "/var/lib/renovate-agent/last_session.json",
        )
        assert "no Renovate PR changed state" in text
        assert "⚠️" in text

    def test_a_run_that_resolved_a_pr_is_clean(self) -> None:
        text = al.render_digest(
            al.parse_run(_result(), 0, False),
            al.delta([_pr(1), _pr(2)], [_pr(2)], {1: "MERGED"}),
            "daniel-box",
            "/log",
        )
        assert "resolved #1" in text
        assert "still open: #2" in text

    def test_a_superseded_pr_is_flagged_not_resolved(self) -> None:
        """Closing a `manual —` bump in favour of a hand-off PR lands nothing."""
        text = al.render_digest(
            al.parse_run(_result(result="superseded #1 with #50"), 0, False),
            al.delta([_pr(1)], [], {1: "CLOSED"}),
            "daniel-box",
            "/log",
        )
        head = text.splitlines()[0]
        assert "resolved" not in head
        assert head.startswith("⚠️")
        assert "closed without merging (superseded or dropped): #1" in text

    def test_a_handed_off_pr_is_named_from_the_census(self) -> None:
        text = al.render_digest(
            al.parse_run(_result(result="done"), 0, False),
            al.delta([_pr(1)], [], {1: "CLOSED"}, (50,)),
            "daniel-box",
            "/log",
        )
        assert "handed off, open for a person to land: #50" in text

    def test_an_unreadable_handoff_census_is_flagged(self) -> None:
        text = al.render_digest(
            al.parse_run(_result(), 0, False),
            al.delta([_pr(1)], [], {1: "CLOSED"}, None),
            "daniel-box",
            "/log",
        )
        assert "hand-off census unreadable" in text
        assert "handed off, open" not in text

    def test_an_unread_state_is_flagged_not_resolved(self) -> None:
        text = al.render_digest(
            al.parse_run(_result(), 0, False),
            al.delta([_pr(1)], [], {}),
            "daniel-box",
            "/log",
        )
        assert "resolved" not in text.splitlines()[0]
        assert "state unreadable: #1" in text

    def test_a_failed_run_leads_with_the_failure(self) -> None:
        text = al.render_digest(
            al.parse_run("", 0, True),
            al.delta([_pr(1)], [_pr(1)], {}),
            "daniel-box",
            "/log",
        )
        assert text.startswith("🚨")

    def test_denials_reach_the_digest(self) -> None:
        text = al.render_digest(
            al.parse_run(_result(permission_denials=[{"tool_name": "Edit"}]), 0, False),
            al.delta([_pr(1)], [], {1: "MERGED"}),
            "daniel-box",
            "/log",
        )
        assert "permission denials: Edit" in text

    def test_the_digest_fits_discord(self) -> None:
        """host_lib.discord_post truncates at 1900 chars; the delta lines must survive."""
        text = al.render_digest(
            al.parse_run(_result(result="x" * 5000), 0, False),
            al.delta([_pr(1)], [], {1: "MERGED"}),
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
        cfg.write_text(
            f"REPO=o/r\nREPO_DIR=/repo\nPROMPT_FILE=/p.txt\nSTATE_DIR={tmp_path}\n"
        )
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
        if argv[0] == "gh" and "merged" in argv:
            return 0, "[]"
        if argv[0] == "gh":
            return 0, json.dumps(
                [
                    {
                        "number": n,
                        "title": f"Update dep {n}",
                        "url": f"u/{n}",
                        "author": {"login": "app/renovate"},
                    }
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


class TestPrStates:
    """The digest's merged/closed split rests on this read, so a failed read must not
    come back as a state."""

    def test_each_state_is_read(self) -> None:
        answers = {"1": '{"state":"MERGED"}', "2": '{"state":"CLOSED"}'}

        def run(argv, cwd=None, timeout=120):
            assert argv[:3] == ["gh", "pr", "view"]
            return 0, answers[argv[3]]

        tools = renovate_agent.AgentTools(run=run)
        assert renovate_agent.pr_states("o/r", [1, 2], tools) == {
            1: "MERGED",
            2: "CLOSED",
        }

    def test_a_failed_lookup_is_left_out(self) -> None:
        tools = renovate_agent.AgentTools(run=lambda argv, **kw: (1, "HTTP 502"))
        assert renovate_agent.pr_states("o/r", [1], tools) == {}


_LISTING = json.dumps(
    [
        {
            "number": 60,
            "title": "r",
            "headRefName": "renovate/x",
            "updatedAt": "2026-09-30T06:00:00Z",
            "author": {"login": "app/renovate"},
        },
        {
            "number": 50,
            "title": "t",
            "headRefName": "b-1",
            "updatedAt": "2026-09-30T06:10:00Z",
            "author": {"login": "me"},
        },
        {
            "number": 40,
            "title": "o",
            "headRefName": "b-2",
            "author": {"login": "someone"},
        },
    ]
)


def _listing_tools(login=(0, "me\n")) -> renovate_agent.AgentTools:
    """A gh that answers the repository listing, and refuses the search form."""

    def run(argv, cwd=None, timeout=120):
        if argv[:3] == ["gh", "api", "user"]:
            return login
        assert argv[:3] == ["gh", "pr", "list"]
        assert "--author" not in argv, "--author makes gh read the lagging search index"
        return 0, _LISTING

    return renovate_agent.AgentTools(run=run)


class TestOpenPrs:
    """The Renovate census: every open PR, kept to app/renovate's locally."""

    def test_the_census_keeps_only_renovates_prs(self) -> None:
        prs = renovate_agent.open_prs("o/r", _listing_tools())
        assert prs == [
            al.OpenPR(
                number=60,
                title="r",
                branch="renovate/x",
                updated_at="2026-09-30T06:00:00Z",
            )
        ]

    def test_a_failed_census_raises(self) -> None:
        tools = renovate_agent.AgentTools(run=lambda argv, **kw: (1, "HTTP 502"))
        with pytest.raises(RuntimeError):
            renovate_agent.open_prs("o/r", tools)


class TestOwnPrs:
    """The hand-off census: the session account's open PRs with their head branches."""

    def test_the_census_reads_this_accounts_branches(self) -> None:
        prs = renovate_agent.own_prs("o/r", _listing_tools())
        assert prs == [_own_pr()]

    def test_an_unreadable_login_is_flagged_as_none(self) -> None:
        tools = _listing_tools(login=(1, "HTTP 401"))
        assert renovate_agent.own_prs("o/r", tools) is None

    @pytest.mark.parametrize("answer", [(1, "HTTP 502"), (0, "not json")])
    def test_a_failed_census_is_flagged_as_none(self, answer) -> None:
        tools = renovate_agent.AgentTools(run=lambda argv, **kw: answer)
        assert renovate_agent.own_prs("o/r", tools) is None


def _records(tmp_path) -> list[dict]:
    """The run records main() appended under the config's STATE_DIR."""
    path = tmp_path / "state" / renovate_agent.RUNS_FILE
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestRunRecord:
    """The per-tick line the 30-day usage count reads: measured PRs, not the summary."""

    def test_a_run_records_what_moved_is_clean(self) -> None:
        moved = al.delta(
            [_pr(1), _pr(2), _pr(3)],
            [_pr(3)],
            {1: "MERGED", 2: "CLOSED"},
            handed=(9,),
        )
        outcome = al.parse_run(_result(), 0, False)
        rec = json.loads(al.run_record(100, "ran", "", moved, outcome))
        assert rec["merged"] == [1] and rec["closed"] == [2]
        assert rec["handed_off"] == [9] and rec["left_open"] == [3]
        assert rec["touched"] == []
        assert rec["turns"] == outcome.turns

    def test_a_failed_handoff_census_records_null_not_none_opened_is_flagged(
        self,
    ) -> None:
        moved = al.delta([_pr(1)], [_pr(1)], {}, handed=None)
        rec = json.loads(al.run_record(100, "ran", "", moved))
        assert rec["handed_off"] is None
        assert "cost_usd" not in rec


class TestSkipExitCodes:
    """The Alive beat is the unit's ExecStartPost, so it fires on any exit 0 — including a
    skip that means the tree is stuck. That laundered five daily skips behind a green tile
    from 2026-09-14 (#2014). A blocked tree exits non-zero: no beat, and OnFailure pages. The
    quiet no-PRs skip is the healthy steady state and keeps beating.
    """

    def _cfg(self, tmp_path) -> str:
        (tmp_path / ".claude" / "worktrees" / "renovate-auto").mkdir(parents=True)
        cfg = tmp_path / "config.env"
        cfg.write_text(
            f"REPO=o/r\nREPO_DIR={tmp_path}\nPROMPT_FILE=/p.txt\n"
            f"STATE_DIR={tmp_path}/state\n"
        )
        return str(cfg)

    def test_a_worktree_blocked_skip_is_flagged(self, tmp_path) -> None:
        host = _FakeHost(prs=[1], ahead=1, contained=False)

        rc = renovate_agent.main(_tools(host), self._cfg(tmp_path))

        assert rc == renovate_agent.EXIT_WORKTREE_BLOCKED != 0
        assert any("holds 1 commit(s) not on origin/master" in p for p in host.posts)
        assert _records(tmp_path)[-1]["result"] == "blocked"

    def test_an_empty_backlog_skip_is_clean(self, tmp_path) -> None:
        host = _FakeHost(prs=[])

        assert renovate_agent.main(_tools(host), self._cfg(tmp_path)) == 0
        assert host.posts == []
        (rec,) = _records(tmp_path)
        assert rec["result"] == "skipped" and rec["reason"]
