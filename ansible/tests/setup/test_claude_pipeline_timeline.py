#!/usr/bin/env python3
"""claude-pipeline-timeline snapshots the PR timeline daily, before its journal inputs rotate (#5661).

The joiner is `scripts/dev/pipeline_timeline.py`, tested in
`scripts/dev/tests/test_pipeline_timeline.py`. This module pins what that test cannot see
about the unit:

1. The command. A window of days and the daniel-server pull, because a bare run reads seven
   days and no server records, which is the hand-run gap the timer closes.
2. The failure path. The unit pages through `OnFailure=` and the textfile rule watches it.
3. The arming. True only on daniel-box, and false must remove the units, not leave a timer.

Run: uv run pytest ansible/tests/setup/test_claude_pipeline_timeline.py
"""

import re

from _helpers import ANSIBLE, load_yaml
from _setup_render import render_setup_text
from lib.repo_paths import HOST_VARS

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
DEFAULTS = load_yaml(ROLE / "defaults" / "main.yml")
SERVICE = "claude-pipeline-timeline.service.j2"
TIMER = "claude-pipeline-timeline.timer.j2"
KEY = "claude_code_pipeline_timeline_enabled"


def _render(template: str = SERVICE, **overrides: object) -> str:
    return render_setup_text("claude_code", template, overrides)


def test_the_unit_runs_the_joiner_over_a_window_with_the_server_pull():
    unit = _render()
    assert re.search(
        r"^ExecStart=\S*uv run .*scripts/dev/pipeline_timeline\.py "
        r"--days 3 --pull-reviews daniel-server$",
        unit,
        re.M,
    )
    assert re.search(r"^User=ubuntu$", unit, re.M)
    assert re.search(r"^SyslogLevel=notice$", unit, re.M), (
        "the run's summary is dropped by a journald that stores notice and above"
    )


def test_a_failed_run_pages_and_a_clean_one_writes_the_heartbeat():
    unit = _render()
    assert "OnFailure=claude-pipeline-timeline-alert.service" in unit
    assert "write_check_textfile claude-pipeline-timeline 1" in unit


def test_the_timer_runs_daily_by_default():
    assert "OnUnitActiveSec=1440min" in _render(TIMER)


def test_the_window_survives_a_missed_run():
    days = DEFAULTS["claude_code_pipeline_timeline_days"]
    interval_days = int(DEFAULTS["claude_code_pipeline_timeline_interval"][:-3]) / 1440
    assert days > interval_days, "a missed run would leave a day outside every window"


def test_armed_only_on_daniel_box_and_disarming_removes_the_units():
    assert DEFAULTS[KEY] is False
    assert load_yaml(HOST_VARS / "daniel-box.yml").get(KEY) is True
    assert not load_yaml(HOST_VARS / "daniel-server.yml").get(KEY, False)

    tasks = load_yaml(ROLE / "tasks" / "pipeline_timeline.yml")
    removal = [
        t
        for t in tasks
        if t.get("ansible.builtin.file", {}).get("state") == "absent"
        and "claude-pipeline-timeline.timer" in t.get("loop", [])
    ]
    assert removal and removal[0]["when"] == f"not {KEY}", (
        "disarming must remove the units, or a stale timer keeps running"
    )


def test_the_role_imports_the_tasks_and_stamps_both_templates():
    main = (ROLE / "tasks" / "main.yml").read_text()
    assert "pipeline_timeline.yml" in main
    for name in (SERVICE, TIMER):
        assert f"claude_code/templates/{name}" in main, f"{name} is not stamped"


def test_the_contract_section_names_the_unit():
    doc = (ROLE / "CLAUDE.md").read_text()
    assert re.search(
        r"^## Autonomous-role contract \(`claude-pipeline-timeline`", doc, re.M
    )
