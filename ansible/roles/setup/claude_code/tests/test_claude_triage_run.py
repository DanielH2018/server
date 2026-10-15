"""Behaviour of the triage runner that claude-triage@<episode>.service starts.

The runner is stdlib-only and runs under the host's pinned Python, so these tests drive it
with a fake `claude` binary instead of a real session.
"""

import json
import os
import stat

import claude_triage_run as run
import pytest

HOSTILE = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'


def test_the_session_is_diagnose_only_capped_and_kept_for_resume() -> None:
    argv = run.build_argv(
        {"CLAUDE_BIN": "claude", "BUDGET_USD": "10", "MAX_TURNS": "60"}, "prompt"
    )
    after = argv[argv.index("--disallowedTools") + 1 :]
    for tool in (
        "Edit",
        "Write",
        "NotebookEdit",
        "Bash(systemctl start claude-land@*)",
    ):
        assert tool in after
    assert argv[argv.index("--max-budget-usd") + 1] == "10"
    assert argv[argv.index("--max-turns") + 1] == "60"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == run.SCHEMA
    # The report's resume command needs the transcript on disk.
    assert "--no-session-persistence" not in argv


def test_the_episode_reaches_the_prompt_as_framed_data() -> None:
    prompt = run.build_prompt("Diagnose it.", {"monitor": "Memory", "message": HOSTILE})
    assert prompt.startswith("Diagnose it.")
    assert "untrusted data" in prompt
    head, framed = prompt.split("<episode>\n", 1)
    assert HOSTILE.replace('"', '\\"') in framed
    assert HOSTILE not in head


@pytest.mark.parametrize(
    ("result", "timed_out", "state"),
    [
        (
            {"subtype": "success", "structured_output": {"confidence": "high"}},
            False,
            "diagnosed",
        ),
        (
            {"subtype": "success", "structured_output": {"confidence": "low"}},
            False,
            "inconclusive",
        ),
        ({"subtype": "error_max_budget_usd", "is_error": False}, False, "inconclusive"),
        ({"subtype": "error_during_execution", "is_error": True}, False, "failed"),
        ({}, False, "failed"),
        ({}, True, "timeout"),
    ],
)
def test_the_state_comes_from_what_the_session_returned(
    result, timed_out, state
) -> None:
    assert run.classify(result, timed_out) == state
    assert state in run.STATES


def test_session_output_is_escaped_in_the_report() -> None:
    record = {
        "episode_id": "drill",
        "episode": {"monitor": HOSTILE},
        "state": "diagnosed",
        "diagnosis": {
            "summary": HOSTILE,
            "root_cause": HOSTILE,
            "confidence": "high",
            "evidence": [{"source": HOSTILE, "finding": HOSTILE}],
            "proposed_fix": HOSTILE,
        },
        "session_id": "abc",
    }
    page = run.render_report(record, "2026-10-15 18:00")
    assert "<script>" not in page
    assert "<img" not in page
    assert "&lt;script&gt;" in page
    assert "cd ~/server &amp;&amp; claude --resume abc" in page


@pytest.mark.parametrize(
    ("episode_id", "valid"),
    [
        ("drill", True),
        ("20261015t1830-memory", True),
        ("Drill", False),
        ("-drill", False),
        ("drill:failed", False),
        ("../etc", False),
        ("a" * 64, False),
    ],
)
def test_the_episode_id_is_one_shape(episode_id, valid) -> None:
    assert bool(run.EPISODE_ID.match(episode_id)) is valid


def _fake_claude(tmp_path, body: str) -> str:
    fake = tmp_path / "claude"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}\nEOF\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return str(fake)


def _setup(tmp_path, claude_body: str) -> tuple[str, str, str]:
    state = tmp_path / "state"
    (state / "inbox").mkdir(parents=True)
    (state / "inbox" / "drill.json").write_text(json.dumps({"monitor": "Memory"}))
    reports = tmp_path / "artifacts"
    reports.mkdir()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Diagnose it.")
    config = tmp_path / "config.env"
    config.write_text(
        "\n".join(
            [
                f"STATE_DIR={state}",
                f"REPORT_DIR={reports}",
                "REPORT_BASE_URL=https://artifacts.example.test/a/daniel-box-claude/",
                f"PROMPT_FILE={prompt}",
                f"WORKDIR={tmp_path}",
                f"CLAUDE_BIN={_fake_claude(tmp_path, claude_body)}",
                "RUN_TIMEOUT_S=30",
            ]
        )
    )
    return str(config), str(state), str(reports)


def test_a_run_with_no_diagnosis_still_leaves_a_record_a_report_and_a_page(
    tmp_path,
) -> None:
    pages = []
    body = json.dumps({"subtype": "error_max_budget_usd", "session_id": "s1"})
    config, state, reports = _setup(tmp_path, body)

    assert (
        run.main(
            ["claude_triage_run.py", "drill"], config, lambda e, s: pages.append((e, s))
        )
        == 0
    )

    record = json.loads((tmp_path / "state" / "runs" / "drill.json").read_text())
    assert record["state"] == "inconclusive"
    assert record["diagnosis"] is None
    assert record["report"].endswith("/a/daniel-box-claude/triage-drill.html")
    report = os.path.join(reports, "triage-drill.html")
    # Readable by the operator's group and the artifacts pod: 0600 would hide it from both.
    assert stat.S_IMODE(os.stat(report).st_mode) == 0o644
    with open(report) as f:
        assert "The session returned no diagnosis." in f.read()
    assert not os.path.exists(os.path.join(state, "inbox", "drill.json"))
    assert pages == [("drill", "inconclusive")]


def test_a_malformed_episode_id_runs_nothing(tmp_path) -> None:
    config, _, _ = _setup(tmp_path, "{}")
    paged = []
    assert (
        run.main(
            ["claude_triage_run.py", "../drill"], config, lambda e, s: paged.append(e)
        )
        == 2
    )
    assert paged == []
    assert not (tmp_path / "state" / "runs").exists()
