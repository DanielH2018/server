"""Behaviour of the triage runner that claude-triage@<episode>.service starts.

The runner is stdlib-only and runs under the host's pinned Python, so these tests drive it
with a fake `claude` binary instead of a real session. A fake `start` stands in for systemd: it
runs the session script in-process against the same config, as the session unit would.
"""

import json
import os
import stat
import time

import claude_triage_run as run
import claude_triage_session as session
import pytest
from host_lib import UnitEnd

# 2026-10-15 18:30 UTC: the cap tests date their records against this, never the live clock.
T0 = 1792089000.0
HOSTILE = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'


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
                f"EXCHANGE_DIR={state}/session",
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


def _start(config: str):
    """systemd's part: run the session unit's script, then report how the unit ended."""

    def start(unit: str, timeout: int) -> UnitEnd:
        assert unit == run.SESSION_UNIT
        session.main(["claude_triage_session.py"], config)
        return UnitEnd("success", 0)

    return start


def test_a_run_with_no_diagnosis_still_leaves_a_record_a_report_and_a_page(
    tmp_path,
) -> None:
    pages = []
    body = json.dumps({"subtype": "error_max_budget_usd", "session_id": "s1"})
    config, state, reports = _setup(tmp_path, body)

    assert (
        run.main(
            ["claude_triage_run.py", "drill"],
            config,
            lambda e, s: pages.append((e, s)),
            start=_start(config),
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


def test_a_failed_session_says_why_in_the_record_and_the_report(tmp_path) -> None:
    # The shape of the 2026-10-17 drill: a rejected credential, no diagnosis.
    body = json.dumps(
        {
            "is_error": True,
            "subtype": "success",
            "result": "Not logged in · Please run /login",
            "api_error_status": 401,
        }
    )
    config, _, reports = _setup(tmp_path, body)

    assert (
        run.main(
            ["claude_triage_run.py", "drill"],
            config,
            lambda e, s: None,
            start=_start(config),
        )
        == 0
    )

    record = json.loads((tmp_path / "state" / "runs" / "drill.json").read_text())
    assert record["state"] == "failed"
    assert record["error"] == "Not logged in · Please run /login (API status 401)"
    with open(os.path.join(reports, "triage-drill.html")) as f:
        assert "Why it stopped" in f.read()


def test_a_diagnosed_session_reports_no_stop_reason() -> None:
    result = {"subtype": "success", "structured_output": {"confidence": "high"}}
    assert run.stop_reason(result, False) == ""


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


def test_the_daily_cap_stops_a_run_before_any_session_starts(tmp_path) -> None:
    # The fake would leave a diagnosed record if a session ran.
    body = json.dumps(
        {"subtype": "success", "structured_output": {"confidence": "high"}}
    )
    config, _, _ = _setup(tmp_path, body)
    with open(config, "a") as f:
        f.write("\nDAILY_CAP=2\n")
    runs = tmp_path / "state" / "runs"
    runs.mkdir()
    today = time.strftime("%Y-%m-%d 00:01 UTC", time.gmtime(T0))
    for name, state in (("a", "diagnosed"), ("b", "failed"), ("c", "capped")):
        (runs / f"{name}.json").write_text(
            json.dumps({"started": today, "state": state})
        )
    pages = []

    assert (
        run.main(
            ["claude_triage_run.py", "drill"],
            config,
            lambda e, s: pages.append(s),
            lambda: T0,
        )
        == 0
    )

    record = json.loads((runs / "drill.json").read_text())
    assert record["state"] == "capped"
    assert "daily cap of 2" in record["error"]
    assert pages == ["capped"]


def test_a_capped_record_and_yesterdays_runs_do_not_count_toward_the_cap(
    tmp_path,
) -> None:
    now = T0
    today = time.strftime("%Y-%m-%d 09:00 UTC", time.gmtime(now))
    yesterday = time.strftime("%Y-%m-%d 09:00 UTC", time.gmtime(now - 86400))
    for name, started, state in (
        ("a", today, "diagnosed"),
        ("b", today, "capped"),
        ("c", yesterday, "diagnosed"),
    ):
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"started": started, "state": state})
        )
    (tmp_path / "torn.json").write_text("{")
    assert run.runs_today(str(tmp_path), now) == 1


def test_a_session_the_unit_stopped_at_its_wall_clock_is_recorded_as_timed_out(
    tmp_path,
) -> None:
    config, _, _ = _setup(tmp_path, "{}")
    pages = []

    run.main(
        ["claude_triage_run.py", "drill"],
        config,
        lambda e, s: pages.append(s),
        start=lambda unit, timeout: UnitEnd("timeout", -1),
    )

    record = json.loads((tmp_path / "state" / "runs" / "drill.json").read_text())
    assert record["state"] == "timeout"
    assert pages == ["timeout"]


def test_a_result_the_session_planted_as_a_symlink_is_not_followed(tmp_path) -> None:
    """The session could point its result at a file it cannot read; the runner reads nothing."""
    config, state, _ = _setup(tmp_path, "{}")
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"structured_output": {"summary": "write-sentinel"}}))

    def start(unit: str, timeout: int) -> UnitEnd:
        os.symlink(secret, os.path.join(state, "session", "out", session.RESULT_NAME))
        return UnitEnd("success", 0)

    run.main(["claude_triage_run.py", "drill"], config, lambda e, s: None, start=start)

    record = json.loads((tmp_path / "state" / "runs" / "drill.json").read_text())
    assert record["state"] == "failed"
    assert "write-sentinel" not in json.dumps(record)
