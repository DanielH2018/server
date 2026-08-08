import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from state_push import main, read_state, verdict  # noqa: E402

HOUR = 3600.0


def test_clean_recent_run_is_up():
    ok, msg = verdict({"ok": True, "msg": "library clean"}, 2 * HOUR, 26 * HOUR, "scan")
    assert ok
    assert "scan ok 2.0h ago: library clean" in msg


def test_failed_run_is_down_with_its_own_message():
    ok, msg = verdict(
        {"ok": False, "msg": "Sonarr unreachable"}, 1 * HOUR, 26 * HOUR, "scan"
    )
    assert not ok
    assert "Sonarr unreachable" in msg


def test_a_failed_run_reports_the_failure_not_the_staleness():
    # Order matters: the run's own message names the cause, "40h ago" does not.
    ok, msg = verdict(
        {"ok": False, "msg": "blast valve tripped"}, 40 * HOUR, 26 * HOUR, "scan"
    )
    assert not ok
    assert "blast valve" in msg
    assert "ago" not in msg


def test_stale_success_is_down():
    ok, msg = verdict({"ok": True, "msg": "clean"}, 40 * HOUR, 26 * HOUR, "scan")
    assert not ok
    assert "40.0h ago" in msg


def test_missing_state_file_is_a_failure_not_a_skip(tmp_path):
    state, reason = read_state(str(tmp_path / "nope.json"))
    assert state is None
    assert "never ran" in reason


def test_unparseable_state_file_is_a_failure(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    state, reason = read_state(str(p))
    assert state is None
    assert "unreadable" in reason


def test_state_without_a_timestamp_is_a_failure(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"ok": True, "msg": "x"}))
    state, reason = read_state(str(p))
    assert state is None
    assert "no usable ts" in reason


def test_main_emits_one_line_per_triple(tmp_path, capsys):
    good = tmp_path / "a.json"
    good.write_text(json.dumps({"ts": int(time.time()), "ok": True, "msg": "fine"}))
    main(
        [
            "state_push.py",
            "scan",
            str(good),
            "26",
            "replace",
            str(tmp_path / "missing.json"),
            "1.2",
        ]
    )
    lines = capsys.readouterr().out.strip().split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("up\t")
    assert lines[1].startswith("down\t")


def test_main_survives_a_malformed_invocation(capsys):
    # A wrapper bug must still produce a pushable line rather than silence.
    assert main(["state_push.py", "only-one-arg"]) == 0
    assert capsys.readouterr().out.startswith("down\t")
