"""Every guard has an accept case and a reject case."""

import time

from deploy_ui_lib import deploy_ui_writes as w
from _process_waits import wait_for_exit
from deploy_ui_lib.deploy_ui_reads import Run

L = Run(4321, 10, "land", "1543", "", "land.py --pr 1543", (), ())


def refused(reason: str | None) -> str:
    """A guard's refusal text; fails the test when the guard let the request through."""
    assert reason is not None, "the guard accepted a request it should refuse"
    return reason


def test_write_allowed_header_and_json_is_clean():
    assert (
        w.write_allowed({"X-Deploy-UI": "1", "Content-Type": "application/json"})
        is None
    )


def test_write_allowed_missing_header_is_flagged():
    assert "X-Deploy-UI" in refused(
        w.write_allowed({"Content-Type": "application/json"})
    )


def test_write_allowed_form_body_is_flagged():
    assert "json" in refused(
        w.write_allowed(
            {"X-Deploy-UI": "1", "Content-Type": "application/x-www-form-urlencoded"}
        )
    )


def test_guard_land_new_pr_is_clean():
    assert w.guard_land("1550", [L]) is None


def test_guard_land_duplicate_pr_is_flagged():
    assert "1543" in refused(w.guard_land("1543", [L]))


def test_guard_cancel_listed_pid_is_clean():
    assert w.guard_cancel(4321, {4321}) is None


def test_guard_cancel_unlisted_pid_is_flagged():
    assert "4321" in refused(w.guard_cancel(4321, {9}))


def test_spawn_logged_writes_output_and_returns_log(tmp_path):
    log = w.spawn_logged(["sh", "-c", "echo hi"], tmp_path, tmp_path / "logs", "land")
    assert wait_for_exit(int(log.with_suffix(".pid").read_text())), (
        "the child never exited"
    )
    assert log.read_text().strip() == "hi"
    assert log.name.startswith("land-")


def test_spawn_logged_never_takes_a_name_already_on_disk(tmp_path):
    """The collision case, staged rather than raced.

    The victim file is the exact name a second-granular scheme would derive, written before
    the spawn — so a `<action>-<ts>.log` name would open it `wb` and truncate a log another
    process was still writing to, plus overwrite its `.pid`. Staging it is what makes this
    deterministic: two live spawns need not land in the same second.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    victim = logs / f"land-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.log"
    victim.write_text("output of the first process\n")
    log = w.spawn_logged(["sh", "-c", "sleep 5"], tmp_path, logs, "land")
    try:
        assert log != victim
        assert victim.read_text() == "output of the first process\n"
    finally:
        w.terminate(int(log.with_suffix(".pid").read_text()))


def test_terminate_kills_the_pid(tmp_path):
    log = w.spawn_logged(["sh", "-c", "sleep 30"], tmp_path, tmp_path / "logs", "x")
    pid = int(log.with_suffix(".pid").read_text())
    w.terminate(pid)
    assert wait_for_exit(pid), "still alive"
