"""Every guard has an accept case and a reject case; the hold clears as a pair."""

import os
import time

import deploy_ui_writes as w
from deploy_ui_reads import Landing

L = Landing(4321, 10, "1543", "land.py --pr 1543")


def test_write_allowed_header_and_json_is_clean():
    assert (
        w.write_allowed({"X-Deploy-UI": "1", "Content-Type": "application/json"})
        is None
    )


def test_write_allowed_missing_header_is_flagged():
    assert "X-Deploy-UI" in w.write_allowed({"Content-Type": "application/json"})


def test_write_allowed_form_body_is_flagged():
    assert "json" in w.write_allowed(
        {"X-Deploy-UI": "1", "Content-Type": "application/x-www-form-urlencoded"}
    )


def test_guard_land_new_pr_is_clean():
    assert w.guard_land("1550", [L], "") is None


def test_guard_land_duplicate_pr_is_flagged():
    assert "1543" in w.guard_land("1543", [L], "")


def test_guard_land_under_hold_is_flagged():
    assert "hold" in w.guard_land("1550", [], "deadbeef")


def test_guard_deploy_known_tag_is_clean():
    assert w.guard_deploy("homepage", {"homepage", "n8n"}, "") is None


def test_guard_deploy_unknown_tag_is_flagged():
    assert "homepage2" in w.guard_deploy("homepage2", {"homepage"}, "")


def test_guard_deploy_under_hold_is_flagged():
    assert "hold" in w.guard_deploy("homepage", {"homepage"}, "deadbeef")


def test_guard_cancel_listed_pid_is_clean():
    assert w.guard_cancel(4321, {4321}) is None


def test_guard_cancel_unlisted_pid_is_flagged():
    assert "4321" in w.guard_cancel(4321, {9})


def test_clear_hold_matching_sha_removes_both_is_clean(state_dir):
    (state_dir / "hold_sha").write_text("deadbeef\n")
    (state_dir / "hold_plane").write_text("k3s\n")
    assert w.clear_hold(state_dir, "deadbeef") is None
    assert not (state_dir / "hold_sha").exists()
    assert not (state_dir / "hold_plane").exists()


def test_clear_hold_mismatch_touches_nothing_is_flagged(state_dir):
    (state_dir / "hold_sha").write_text("deadbeef\n")
    (state_dir / "hold_plane").write_text("k3s\n")
    assert "deadbeef" in w.clear_hold(state_dir, "cafef00d")
    assert (state_dir / "hold_plane").exists()


def test_set_override_round_trip_is_clean(state_dir):
    assert w.set_override(state_dir, "set") is None
    assert (state_dir / "staging_gate_override").exists()
    assert w.set_override(state_dir, "clear") is None
    assert not (state_dir / "staging_gate_override").exists()


def test_set_override_unknown_action_is_flagged(state_dir):
    assert "action" in w.set_override(state_dir, "toggle")


def test_spawn_logged_writes_output_and_returns_log(tmp_path):
    log = w.spawn_logged(["sh", "-c", "echo hi"], tmp_path, tmp_path / "logs", "land")
    for _ in range(50):
        if log.exists() and log.read_text().strip() == "hi":
            break
        time.sleep(0.05)
    assert log.read_text().strip() == "hi"
    assert log.name.startswith("land-")


def test_terminate_kills_the_pid(tmp_path):
    log = w.spawn_logged(["sh", "-c", "sleep 30"], tmp_path, tmp_path / "logs", "x")
    pid = int(log.with_suffix(".pid").read_text())
    w.terminate(pid)
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("still alive")
