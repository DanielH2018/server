"""Each fan-out `claude -p` carries its session kind and batch as OTEL resource attributes.

Run: uv run pytest scripts/dev/tests/test_fanout_review_otel.py
"""

import json
import shlex

from _review_fakes import PR, _finding, _pipeline, _report

from dev.fanout_lib import telemetry
from dev.fanout_lib.launch import systemd_run_command


def _raw_claude_calls(tmp_path, reports, monkeypatch, inherited=None, settings=None):
    # The user's settings are read from here, never from the real home.
    config = tmp_path / "claude-config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps(settings or {}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    if inherited is None:
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    else:
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", inherited)
    pipeline, run = _pipeline(tmp_path, reports)
    raw = []

    def recording(argv, stdin):
        raw.append(argv)
        return run(argv, stdin)

    pipeline.run = recording
    pipeline.run_all()
    return [argv for argv in raw if "claude" in argv]


def test_every_phase_names_its_batch_and_phase(tmp_path, monkeypatch):
    reports = [
        _report(f"Opened {PR}", cost=3.0),
        _report(structured={"summary": "", "findings": [_finding("off by one")]}),
        _report(f"Fixed it. {PR}", cost=4.0),
        _report(structured={"summary": "resolved", "findings": []}),
    ]
    calls = _raw_claude_calls(tmp_path, reports, monkeypatch)
    assert [argv[:2] for argv in calls] == [
        [
            "env",
            "OTEL_RESOURCE_ATTRIBUTES=session.kind=fanout-review,"
            f"fanout.batch=1345,fanout.phase={phase}",
        ]
        for phase in ("implement", "review", "fix", "review-delta")
    ]
    assert all(argv[2] == "claude" for argv in calls)


def test_an_attribute_the_unit_already_carries_is_kept(tmp_path, monkeypatch):
    calls = _raw_claude_calls(
        tmp_path,
        [_report("needs input: CI is red")],
        monkeypatch,
        inherited="process.owner=claude",
    )
    assert calls[0][1] == (
        "OTEL_RESOURCE_ATTRIBUTES=process.owner=claude,session.kind=fanout-review,"
        "fanout.batch=1345,fanout.phase=implement"
    )


def test_the_assignment_has_one_shape_for_every_phase(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    assert telemetry.otel_attributes("5651-5654", "review") == (
        "OTEL_RESOURCE_ATTRIBUTES=session.kind=fanout-review,"
        "fanout.batch=5651-5654,fanout.phase=review"
    )


def test_a_user_settings_value_cannot_displace_the_batch_labels(tmp_path, monkeypatch):
    """Claude Code applies a settings `env` over the process env, replacing the string."""
    calls = _raw_claude_calls(
        tmp_path,
        [_report("needs input: CI is red")],
        monkeypatch,
        settings={"env": {"OTEL_RESOURCE_ATTRIBUTES": "process.owner=claude"}},
    )
    wanted = (
        "process.owner=claude,session.kind=fanout-review,"
        "fanout.batch=1345,fanout.phase=implement"
    )
    assert calls[0][1] == f"OTEL_RESOURCE_ATTRIBUTES={wanted}"
    settings = json.loads(calls[0][calls[0].index("--settings") + 1])
    assert settings["env"]["OTEL_RESOURCE_ATTRIBUTES"] == wanted


def test_a_plain_unit_labels_its_telemetry_as_a_fanout_batch_in_env_and_settings():
    argv = shlex.split(systemd_run_command("5651-5654"))
    wanted = "session.kind=fanout-batch,fanout.batch=5651-5654"
    assert f"OTEL_RESOURCE_ATTRIBUTES={wanted}" in argv
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["env"]["OTEL_RESOURCE_ATTRIBUTES"] == wanted
