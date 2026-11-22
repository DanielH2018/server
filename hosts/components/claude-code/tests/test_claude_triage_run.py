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
import host_lib
from host_lib import UnitEnd

# 2026-10-15 18:30 UTC: the cap tests date their records against this, never the live clock.
T0 = 1792089000.0
# 2026-11-14 12:00 UTC, the day the cap tests count runs on.
CAP_DAY = 1794657600
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
        "session_id": HOSTILE,
    }
    page = run.render_report(record, "2026-10-15 18:00")
    assert "<script>" not in page
    assert "<img" not in page
    assert "&lt;script&gt;" in page
    # The operator ruled out resuming an unattended session in place (2026-11-14).
    assert "claude --resume" not in page


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
    published = tmp_path / "published"
    (published / "current").mkdir(parents=True)
    (published / "lock").touch()
    (published / "current" / "triage.json").write_text("{}")
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
                f"SETTINGS_FILE={published}/current/triage.json",
                "RUN_TIMEOUT_S=30",
                'RESTART_NAMES=["docs"]',
                "RESTART_TIMEOUT_S=900",
                f"RESTART_VERDICT={tmp_path}/verdict",
            ]
        )
    )
    return str(config), str(state), str(reports)


def _start(config: str, broker=None, started: list | None = None):
    """systemd's part: run the session unit's script, or `broker` for a restart unit.

    Each unit started is appended to `started`.
    """

    def start(unit: str, timeout: int) -> UnitEnd:
        if started is not None:
            started.append(unit)
        if unit == run.SESSION_UNIT:
            session.main(["claude_triage_session.py"], config)
            return UnitEnd("success", 0)
        assert broker is not None, unit
        return broker(unit)

    return start


def test_a_run_with_no_diagnosis_still_leaves_a_record_a_report_and_a_page(
    tmp_path,
) -> None:
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    pages = []
    denied = [{"tool_name": "Bash", "tool_input": {"command": "secret-sentinel"}}] * 2
    body = json.dumps(
        {
            "subtype": "error_max_budget_usd",
            "session_id": "s1",
            "permission_denials": denied,
        }
    )
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
    # A count a reader can act on, with nothing the session chose in it.
    assert record["denials"] == 2
    assert "secret-sentinel" not in json.dumps(record)
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
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
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


def _run_with_out(tmp_path, plant) -> tuple[dict, str]:
    """One run whose session only calls `plant(out)`; the record and the state directory."""
    config, state, _ = _setup(tmp_path, "{}")
    out = os.path.join(state, "session", session.OUT_DIR)

    def start(unit: str, timeout: int) -> UnitEnd:
        with open(os.path.join(out, session.RESULT_NAME), "w") as f:
            f.write("{}")
        plant(out)
        return UnitEnd("success", 0)

    run.main(["claude_triage_run.py", "drill"], config, lambda e, s: None, start=start)
    return json.loads((tmp_path / "state" / "runs" / "drill.json").read_text()), state


def test_the_transcript_is_kept_beside_the_record_and_nothing_stays_in_out(
    tmp_path,
) -> None:
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    def plant(out: str) -> None:
        with open(os.path.join(out, host_lib.TRANSCRIPT_NAME), "w") as f:
            f.write('{"type":"user"}\n')

    record, state = _run_with_out(tmp_path, plant)

    kept = os.path.join(state, "runs", "drill.transcript.jsonl")
    with open(kept) as f:
        assert f.read() == '{"type":"user"}\n'
    assert stat.S_IMODE(os.stat(kept).st_mode) == 0o640
    assert (record["transcript_bytes"], record["transcript_cut"]) == (16, False)
    # The session owned what it wrote there; none of it may reach the next run.
    assert not os.path.exists(os.path.join(state, "session", session.OUT_DIR))


def test_a_transcript_the_session_planted_as_a_symlink_is_not_kept(tmp_path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("read-sentinel")

    record, state = _run_with_out(
        tmp_path,
        lambda out: os.symlink(secret, os.path.join(out, host_lib.TRANSCRIPT_NAME)),
    )

    assert record["transcript_bytes"] is None
    assert not os.path.exists(os.path.join(state, "runs", "drill.transcript.jsonl"))


def test_a_kept_transcript_does_not_count_toward_the_daily_cap(tmp_path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    started = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(CAP_DAY))
    (runs / "a.json").write_text(json.dumps({"started": started, "state": "diagnosed"}))
    (runs / "a.transcript.jsonl").write_text(
        json.dumps({"started": started, "state": "diagnosed"})
    )

    assert run.runs_today(str(runs), CAP_DAY) == 1


def _diagnosis(confidence: str, restart: str) -> str:
    return json.dumps(
        {
            "subtype": "success",
            "structured_output": {
                "summary": "docs serves a stale page",
                "root_cause": "x",
                "confidence": confidence,
                "evidence": [],
                "proposed_fix": "none",
                "restart": restart,
            },
        }
    )


def _broker_writes(tmp_path, line: str):
    """The broker as it ends: its verdict line replaces the verdict file."""

    def broker(unit: str) -> UnitEnd:
        host_lib.atomic_write(str(tmp_path / "verdict"), line + "\n")
        return UnitEnd("success", 0)

    return broker


def _run_restart(tmp_path, body: str, broker=None):
    config, _, reports = _setup(tmp_path, body)
    started, pages = [], []
    run.main(
        ["claude_triage_run.py", "drill"],
        config,
        lambda e, s: pages.append(s),
        start=_start(config, broker, started),
    )
    record = json.loads((tmp_path / "state" / "runs" / "drill.json").read_text())
    with open(os.path.join(reports, "triage-drill.html")) as f:
        report = f.read()
    return record, report, started, pages


def test_a_diagnosis_naming_a_listed_workload_starts_the_broker_and_records_its_verdict(
    tmp_path,
) -> None:
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    line = 'RESTART-RESULT: name=docs verdict=restarted cause= detail="deployment/docs rolled"'

    record, report, started, pages = _run_restart(
        tmp_path, _diagnosis("high", "docs"), _broker_writes(tmp_path, line)
    )

    assert started == [run.SESSION_UNIT, "homelab-claude-restart@docs.service"]
    assert record["restart"] == {
        "name": "docs",
        "verdict": "restarted",
        "cause": "",
        "detail": "deployment/docs rolled",
    }
    assert "<code>docs</code>: restarted. deployment/docs rolled" in report
    assert pages == ["diagnosed"]


def test_a_name_off_the_allowlist_starts_nothing_and_is_escaped_in_the_report(
    tmp_path,
) -> None:
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    record, report, started, _ = _run_restart(tmp_path, _diagnosis("high", HOSTILE))

    assert started == [run.SESSION_UNIT]
    assert (record["restart"]["verdict"], record["restart"]["cause"]) == (
        "refused",
        "not-listed",
    )
    assert "<script>" not in report
    assert "&lt;script&gt;" in report


def test_a_low_confidence_diagnosis_cannot_ask_for_a_restart(tmp_path) -> None:
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    record, _, started, _ = _run_restart(tmp_path, _diagnosis("low", "docs"))

    assert started == [run.SESSION_UNIT]
    assert record["state"] == "inconclusive"
    assert record["restart"]["cause"] == "not-diagnosed"


def test_a_verdict_an_earlier_request_left_is_not_read_as_this_ones(tmp_path) -> None:
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    # The broker's lock wait timed out, so the script never ran and the file is the drill's.
    (tmp_path / "verdict").write_text(
        "RESTART-RESULT: name=docs verdict=restarted cause= detail=drill\n"
    )

    record, _, _, _ = _run_restart(
        tmp_path, _diagnosis("medium", "docs"), lambda unit: UnitEnd("exit-code", 1)
    )

    assert (record["restart"]["verdict"], record["restart"]["cause"]) == (
        "unknown",
        "no-verdict",
    )
    assert "exit-code" in record["restart"]["detail"]


def test_a_diagnosis_that_names_no_restart_starts_only_the_session(tmp_path) -> None:
    body = json.dumps(
        {"subtype": "success", "structured_output": {"confidence": "high"}}
    )

    record, report, started, _ = _run_restart(tmp_path, body)

    assert started == [run.SESSION_UNIT]
    assert record["restart"] is None
    assert "<h2>Restart</h2>" not in report
