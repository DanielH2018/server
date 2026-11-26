"""The weekly AIDE check passes `--config`, or it checks nothing.

WHY. AIDE 0.18 has no default config path. A bare `aide --check` exits on its first line with
`ERROR: missing configuration`, and the cron piped that line to `logger -t aide`, where nothing
alerted on it. aide-check.sh pushes that exit DOWN, but a check that never reads its config
still checks nothing.

Run: uv run pytest hosts/tests/host_checks/test_aide_check_names_its_config.py
"""

import re

import pytest
from _host_packages import package, shipped_text
from _shell_render import rendered_shell_text

UNIT_DIR = "/usr/lib/systemd/system"
TIMER = f"{UNIT_DIR}/homelab-aide-check.timer"
SERVICE = f"{UNIT_DIR}/homelab-aide-check.service"
SCRIPT = ("components", "aide-scan", "aide-check.sh.j2")

AIDE_CONF = "/usr/lib/homelab/aide.conf"

# `aide` as a command word, followed by `--config=` or `--config ` and the AIDE_CONF variable.
_AIDE_WITH_CONFIG = re.compile(
    r'/aide\s+(?:\S+\s+)*?(?:--config[= ]|-c\s)"?\$AIDE_CONF\b'
)


@pytest.mark.parametrize("host", ["daniel-box", "daniel-server"])
def test_the_weekly_cron_runs_the_check_script(host: str):
    """homelab-aide-check.timer starts the service whose ExecStart is the check script."""
    assert not re.search(r"^Unit=", shipped_text(host, TIMER), re.M), (
        "the timer names another unit, so homelab-aide-check.service is never started"
    )
    assert "ExecStart=/usr/lib/homelab/aide-check\n" in shipped_text(host, SERVICE), (
        "the AIDE service must run aide-check, which keeps aide's exit code and pushes it"
    )


def test_weekly_aide_check_passes_its_config():
    script = rendered_shell_text(*SCRIPT)
    assert re.search(rf"^AIDE_CONF={re.escape(AIDE_CONF)}$", script, re.M)
    assert AIDE_CONF in package("daniel-box").files
    aide_lines = [
        line
        for line in script.splitlines()
        if "/usr/bin/aide " in line and not line.lstrip().startswith("#")
    ]
    assert aide_lines, "aide-check.sh no longer invokes /usr/bin/aide"
    for line in aide_lines:
        assert "--update" in line, line
        assert _AIDE_WITH_CONFIG.search(line), (
            f"aide-check.sh runs aide without --config: {line}"
        )


def test_the_pattern_refuses_a_bare_check():
    assert not _AIDE_WITH_CONFIG.search(
        "/usr/bin/nice -n19 /usr/bin/aide --check 2>&1 | logger -t aide"
    )
