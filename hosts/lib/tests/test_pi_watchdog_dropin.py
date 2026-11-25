"""daniel-pi's watchdog daemon: a restart leaves it running, and its load limits are the declared ones.

The restart test starts, stops and kills two stand-in units in the systemd user manager, because only
systemd's job queue decides what a restart does to the daemon. The leak guard lists it in
`_LIVE_API_TESTS`, and it skips where no user manager answers. docs/pi-host-tuning-record.md has
the stop behind it.

Run: uv run pytest hosts/lib/tests/test_pi_watchdog_dropin.py
"""

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from reconcile_lib import lines
from renderer.renderer_lib import host as host_mode

WATCHDOG_CONF = "/etc/watchdog.conf"
WATCHDOG_DROPIN = "/usr/lib/systemd/system/watchdog.service.d/50-homelab.conf"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


def _user_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _becomes_active(unit: str) -> bool:
    """Whether `unit` is active within 10 seconds; an OnFailure= start is queued, not awaited."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _user_systemctl("is-active", unit).stdout.strip() == "active":
            return True
        time.sleep(0.2)
    return False


def _device_holders(dropin: str | None) -> dict[str, str]:
    """Which of two stand-in units runs after the daemon is restarted, stopped and killed.

    The stand-ins hold the directives of watchdog 5.16's watchdog.service and
    wd_keepalive.service that decide the outcome, and run in the systemd user manager.

    Args:
        dropin: The daemon's drop-in, or None for the packaged pair alone.

    Returns:
        `daemon`, `keepalive` or `nothing` for each of `restart`, `stop` and `crash`.
    """
    # Another session may run this on the same host, so each call names its own pair.
    tag = uuid.uuid4().hex[:8]
    daemon, keepalive = (
        f"homelab-test-wd-{tag}.service",
        f"homelab-test-ka-{tag}.service",
    )
    unit_dir = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user"
    dropin_dir = unit_dir / f"{daemon}.d"

    def keepalive_or_nothing() -> str:
        return "keepalive" if _becomes_active(keepalive) else "nothing"

    try:
        dropin_dir.mkdir(parents=True)
        (unit_dir / daemon).write_text(
            f"[Unit]\nConflicts={keepalive}\nOnFailure={keepalive}\n"
            "[Service]\nExecStart=/bin/sleep infinity\nExecStopPost=/bin/false\n"
        )
        (unit_dir / keepalive).write_text(
            f"[Unit]\nBefore={daemon}\nConflicts={daemon}\n"
            "[Service]\nExecStart=/bin/sleep infinity\n"
        )
        if dropin is not None:
            (dropin_dir / "50-homelab.conf").write_text(dropin)
        _user_systemctl("daemon-reload")
        holders = {}

        _user_systemctl("start", daemon)
        _user_systemctl("restart", daemon)
        running = _user_systemctl("is-active", daemon).stdout.strip() == "active"
        holders["restart"] = "daemon" if running else keepalive_or_nothing()

        _user_systemctl("start", daemon)
        _user_systemctl("stop", daemon)
        holders["stop"] = keepalive_or_nothing()

        _user_systemctl("start", daemon)
        _user_systemctl("kill", "--signal=KILL", daemon)
        holders["crash"] = keepalive_or_nothing()
        return holders
    finally:
        _user_systemctl("stop", daemon, keepalive)
        _user_systemctl("reset-failed", daemon, keepalive)
        shutil.rmtree(dropin_dir, ignore_errors=True)
        (unit_dir / daemon).unlink(missing_ok=True)
        (unit_dir / keepalive).unlink(missing_ok=True)
        _user_systemctl("daemon-reload")


def test_a_restart_leaves_the_watchdog_daemon_running_and_a_stop_or_a_crash_starts_the_keepalive(
    package,
):
    if (
        not os.environ.get("XDG_RUNTIME_DIR")
        or _user_systemctl("show-environment").returncode != 0
    ):
        pytest.skip("needs a systemd user manager; CI runners have none")
    handover = {"stop": "keepalive", "crash": "keepalive"}
    assert _device_holders(package.files[WATCHDOG_DROPIN][0].decode()) == {
        "restart": "daemon",
        **handover,
    }
    # The packaged pair alone: a restart ends with the daemon down behind the keepalive.
    assert _device_holders(None) == {"restart": "keepalive", **handover}


# The load lines of daniel-pi's /etc/watchdog.conf: watchdog 5.16's stock conffile and the line
# an earlier edit appended.
LIVE_WATCHDOG_LOAD = (
    "#max-load-1\t\t= 24\n#max-load-5\t\t= 18\n#max-load-15\t\t= 12\nmax-load-1 = 24\n"
)


def load_limits(text: str) -> tuple[int, int, int]:
    """The 1, 5 and 15-minute limits watchdog 5.16 reads, with its defaults for an unset one."""
    declared = {
        int(span): int(limit)
        for span, limit in re.findall(r"^max-load-(\d+)\s*=\s*(\d+)", text, re.M)
    }
    one = declared[1]
    return one, declared.get(5, one * 3 // 4), declared.get(15, one // 2)


def test_no_longer_load_average_reboots_the_pi_below_the_one_minute_limit(package):
    text = LIVE_WATCHDOG_LOAD
    assert load_limits(text) == (24, 18, 12)
    for edit in package.lines:
        if edit["path"] == WATCHDOG_CONF:
            text = lines.with_line(text, edit)
    one, five, fifteen = load_limits(text)
    assert min(five, fifteen) >= one
