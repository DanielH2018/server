"""Tests for the `open` planner and the `open` CLI: what gets filed, touched, reopened, refused.

`plan_open` is pure — it is handed the issue matching a fingerprint and returns an outcome
plus gh argv. The CLI tests around it drive `findings.main` against the fakes, so the label
sync, the issue read and the Project-board retry all run for real.

Run: uv run pytest scripts/dev/tests/test_findings_open.py
"""

import subprocess

from _findings_fakes import Fakes
from dev import findings
from dev.findings_model import LABELS, PROJECT_TITLE, fingerprint, parse_verify_by
from dev.findings_plans import is_project_failure, plan_open, without_project

_LABELS = ["claude", "severity/high", "kind/gap"]


def _open_argv(body):
    return [
        "open",
        "--title",
        "T",
        "--body-file",
        str(body),
        "--severity",
        "low",
        "--kind",
        "gap",
    ]


# --- the planner ------------------------------------------------------------------------------


def test_open_with_no_match_plans_a_create_with_every_label():
    outcome, code, plans = plan_open(
        None, title="T", body="B", labels=_LABELS, fp="f" * 12, source="s"
    )
    assert (outcome, code) == ("created", 0)
    (argv,) = plans
    assert argv[:3] == ["issue", "create", "--title"]
    for lab in _LABELS:
        assert argv[argv.index("--label", argv.index(lab) - 1) + 1] == lab
    body = argv[argv.index("--body") + 1]
    assert (
        body.startswith("B")
        and "Fingerprint: `" + "f" * 12 + "`" in body
        and "Source: s" in body
    )
    assert argv[argv.index("--project") + 1] == PROJECT_TITLE


def test_open_touch_and_reopen_never_pass_project(issue):
    for existing in (issue(3, fp="f" * 12), issue(3, state="CLOSED", fp="f" * 12)):
        _, _, plans = plan_open(
            existing, title="T", body="B", labels=_LABELS, fp="f" * 12, source="s"
        )
        assert all("--project" not in argv for argv in plans)


def test_open_with_an_open_match_touches_instead_of_creating(issue):
    existing = issue(3, fp="f" * 12)
    outcome, code, plans = plan_open(
        existing, title="T", body="B", labels=_LABELS, fp="f" * 12, source="s"
    )
    assert (outcome, code) == ("touched", 0)
    assert all(argv[:2] != ["issue", "create"] for argv in plans)
    assert plans[0][:3] == ["issue", "comment", "3"]


def test_open_with_a_refuted_match_refuses_and_plans_nothing(issue):
    existing = issue(3, state="CLOSED", labels=("refuted",), fp="f" * 12)
    outcome, code, plans = plan_open(
        existing, title="T", body="B", labels=_LABELS, fp="f" * 12, source="s"
    )
    assert (outcome, code, plans) == ("refuted", 3, [])


def test_open_with_a_fixed_match_reopens_then_comments(issue):
    existing = issue(3, state="CLOSED", fp="f" * 12)
    outcome, code, plans = plan_open(
        existing, title="T", body="B", labels=_LABELS, fp="f" * 12, source="s"
    )
    assert (outcome, code) == ("reopened", 0)
    assert plans[0][:3] == ["issue", "reopen", "3"]
    assert plans[1][:3] == ["issue", "comment", "3"]
    assert "regression" in plans[1][plans[1].index("--body") + 1].lower()


def test_open_adds_no_vetted_remediation_and_domain_labels():
    _, _, plans = plan_open(
        None,
        title="T",
        body="B",
        labels=_LABELS + ["domain/network", "no-vetted-remediation"],
        fp="f" * 12,
        source="s",
    )
    assert "no-vetted-remediation" in plans[0] and "domain/network" in plans[0]


def test_open_with_verify_by_stores_it_in_the_created_body():
    _, _, plans = plan_open(
        None,
        title="T",
        body="B",
        labels=_LABELS,
        fp="f" * 12,
        source="s",
        verify_by="uv run pytest scripts/dev",
    )
    body = plans[0][plans[0].index("--body") + 1]
    assert parse_verify_by(body) == "uv run pytest scripts/dev"
    # The section sits before the fingerprint trailer, not after.
    assert body.index("## Verify-by") < body.index("Fingerprint: `")


def test_open_without_verify_by_stores_no_section():
    _, _, plans = plan_open(
        None, title="T", body="B", labels=_LABELS, fp="f" * 12, source="s"
    )
    body = plans[0][plans[0].index("--body") + 1]
    assert parse_verify_by(body) is None


# --- the CLI ----------------------------------------------------------------------------------


def test_open_cli_exits_3_on_refuted(tmp_path, issue, make_tools):
    body = tmp_path / "b.md"
    body.write_text("B")
    fp = fingerprint("T", "a.py:1")
    refuted = issue(3, state="CLOSED", labels=("refuted",), fp=fp)
    tools, calls = make_tools(Fakes(issues=[refuted]))
    argv = [*_open_argv(body), "--file", "a.py:1"]
    assert findings.main(argv, tools) == 3
    assert not calls.gh


def test_open_cli_prints_the_created_number(tmp_path, capsys, make_tools):
    body = tmp_path / "b.md"
    body.write_text("B")
    tools, _ = make_tools()
    assert findings.main(_open_argv(body), tools) == 0
    assert "#42 created" in capsys.readouterr().out


# --- the Project board is best-effort ---------------------------------------------------------


def test_without_project_drops_the_pair_by_position():
    argv = [
        "issue",
        "create",
        "--title",
        "Claude findings",
        "--project",
        "Claude findings",
    ]
    assert without_project(argv) == [
        "issue",
        "create",
        "--title",
        "Claude findings",
    ]


def test_is_project_failure_matches_project_and_scope_only():
    assert is_project_failure("could not resolve to a ProjectV2")
    assert is_project_failure("missing required SCOPES")
    assert not is_project_failure("HTTP 422: label not found")
    assert not is_project_failure(None)


def test_open_retries_without_project_and_warns(tmp_path, capsys, make_tools):
    body = tmp_path / "b.md"
    body.write_text("B")
    board = subprocess.CalledProcessError(
        1, "gh", stderr="could not resolve to a ProjectV2\nmore\n"
    )
    tools, calls = make_tools(Fakes(gh_errors={"issue create": board}))
    assert findings.main(_open_argv(body), tools) == 0
    out = capsys.readouterr()
    assert "#42 created" in out.out
    assert 'not added to Project "Claude findings": could not resolve' in out.err
    assert "--project" in calls.gh[0] and "--project" not in calls.gh[1]


def test_open_propagates_a_non_project_failure(tmp_path, make_tools):
    body = tmp_path / "b.md"
    body.write_text("B")
    other = subprocess.CalledProcessError(1, "gh", stderr="HTTP 422: label not found")
    tools, _ = make_tools(Fakes(gh_errors={"issue create": other}))
    assert findings.main(_open_argv(body), tools) == 1


# --- open creates the labels it is about to use -----------------------------------------------


def test_open_creates_a_missing_label_before_the_issue(tmp_path, make_tools):
    body = tmp_path / "b.md"
    body.write_text("B")
    tools, calls = make_tools(Fakes(labels=set(LABELS) - {"kind/gap"}))
    assert findings.main(_open_argv(body), tools) == 0
    assert calls.gh[0][:3] == ["label", "create", "kind/gap"]
    assert calls.gh[1][:2] == ["issue", "create"]


def test_open_with_every_label_present_creates_none(tmp_path, make_tools):
    body = tmp_path / "b.md"
    body.write_text("B")
    tools, calls = make_tools()
    assert findings.main(_open_argv(body), tools) == 0
    assert all(argv[:2] != ["label", "create"] for argv in calls.gh)


# --- documented exits instead of tracebacks ----------------------------------------------------


def test_open_with_a_missing_body_file_exits_2_without_calling_gh(tmp_path, make_tools):
    tools, calls = make_tools()
    assert findings.main(_open_argv(tmp_path / "absent.md"), tools) == 2
    # Both boundaries: the label sync `cmd_open` runs first is a gh_json call.
    assert calls.none()


def test_a_gh_timeout_exits_1(tmp_path, capsys, make_tools):
    body = tmp_path / "b.md"
    body.write_text("B")
    slow = subprocess.TimeoutExpired("gh", 60)
    tools, calls = make_tools(Fakes(json_errors={"issue list": slow}))
    assert findings.main(_open_argv(body), tools) == 1
    assert "gh failed:" in capsys.readouterr().err
    # Which read timed out: `cmd_open` syncs labels first, and that one answered.
    assert [argv[:2] for argv in calls.gh_json] == [
        ["label", "list"],
        ["issue", "list"],
    ]


# --- a closed issue's date and state -----------------------------------------------------------


def test_open_on_a_refuted_issue_with_a_null_closed_at(
    tmp_path, capsys, issue, make_tools
):
    body = tmp_path / "b.md"
    body.write_text("B")
    existing = issue(3, state="CLOSED", labels=("refuted",), fp=fingerprint("T", None))
    existing["closedAt"] = None
    tools, calls = make_tools(Fakes(issues=[existing]))
    assert findings.main(_open_argv(body), tools) == 3
    assert "refuted" in capsys.readouterr().out
    assert not calls.gh
