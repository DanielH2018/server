"""The weekly AIDE check passes `--config`, or it checks nothing.

WHY. AIDE 0.18 has no default config path. A bare `aide --check` exits on its first line with
`ERROR: missing configuration`, and the cron piped that line to `logger -t aide`, where nothing
alerted on it. Every host ran the check that way until #4591 and checked nothing. Since #4594 the
cron runs aide-check.sh, which pushes that exit DOWN, but a check that never reads its config
still checks nothing.

Run: uv run pytest ansible/tests/setup/test_aide_check_names_its_config.py
"""

import re

from _helpers import ROLES, load_tasks, walk_tasks
from _shell_render import rendered_shell_text

INTEGRITY = ROLES / "setup" / "initial_setup" / "tasks" / "integrity.yml"
CRON_NAME = "Weekly AIDE file integrity check"
SCRIPT = ("setup", "initial_setup", "aide-check.sh.j2")

# `aide` as a command word, followed by `--config=<path>` or `--config <path>` before the end.
_AIDE_WITH_CONFIG = re.compile(
    r"/aide\s+(?:\S+\s+)*?(?:--config[= ]|-c\s)/etc/aide/aide\.conf\b"
)


def _aide_cron_job() -> str:
    for task in walk_tasks(load_tasks(INTEGRITY)):
        cron = task.get("ansible.builtin.cron")
        if isinstance(cron, dict) and cron.get("name") == CRON_NAME:
            return str(cron["job"])
    raise AssertionError(f"no cron named {CRON_NAME!r} in {INTEGRITY}")


def test_the_weekly_cron_runs_the_check_script():
    assert _aide_cron_job() == "/usr/local/bin/aide-check.sh", (
        "the AIDE cron must run aide-check.sh, which keeps aide's exit code and pushes it"
    )


def test_weekly_aide_check_passes_its_config():
    aide_lines = [
        line
        for line in rendered_shell_text(*SCRIPT).splitlines()
        if "/usr/bin/aide " in line and not line.lstrip().startswith("#")
    ]
    assert aide_lines, "aide-check.sh no longer invokes /usr/bin/aide"
    for line in aide_lines:
        assert "--check" in line, line
        assert _AIDE_WITH_CONFIG.search(line), (
            f"aide-check.sh runs aide without --config: {line}"
        )


def test_the_pattern_refuses_a_bare_check():
    assert not _AIDE_WITH_CONFIG.search(
        "/usr/bin/nice -n19 /usr/bin/aide --check 2>&1 | logger -t aide"
    )
