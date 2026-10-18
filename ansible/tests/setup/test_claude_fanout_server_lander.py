#!/usr/bin/env python3
"""claude-fanout-server-lander lands finished daniel-server batches' PRs (issue #5496).

The rule that decides a PR is `scripts/dev/fanout_lib/autoland.py`, tested in
`scripts/dev/tests/test_fanout_autoland.py`. This module pins what the rule cannot see about
its unit:

1. `KillMode=process`. A detached landing leaves a user-manager service's cgroup only, so the
   default kill of this system unit would end every landing when the oneshot exits.
2. `SyslogLevel=notice`. This host's journald stores notice and above, so without it the one
   line per candidate PR is dropped.
3. The two `LAND_REQUIRE_*` values, held equal to the ones the command checks itself.
4. The arming. True only on daniel-box, and false must remove the units, not leave a timer.

Run: uv run pytest ansible/tests/setup/test_claude_fanout_server_lander.py
"""

import re

from _helpers import ANSIBLE, load_yaml
from _setup_render import render_setup_text
from fanout_lib import autoland
from lib.repo_paths import HOST_VARS

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
DEFAULTS = load_yaml(ROLE / "defaults" / "main.yml")
SERVICE = "claude-fanout-server-lander.service.j2"
TIMER = "claude-fanout-server-lander.timer.j2"


def _render(template: str = SERVICE, **overrides: object) -> str:
    return render_setup_text("claude_code", template, overrides)


def test_a_detached_landing_survives_the_oneshot_that_started_it():
    unit = _render()
    assert re.search(r"^KillMode=process$", unit, re.M), (
        "the default control-group kill ends every landing when this oneshot exits"
    )
    assert "TimeoutStartSec=" in unit


def test_the_one_line_per_candidate_reaches_the_journal():
    assert re.search(r"^SyslogLevel=notice$", _render(), re.M)


def test_the_unit_runs_the_command_as_the_operator_and_alerts_on_failure():
    unit = _render()
    assert re.search(r"^User=ubuntu$", unit, re.M)
    assert "scripts/dev/fanout.py land-server" in unit
    assert "OnFailure=claude-fanout-server-lander-alert.service" in unit


def test_land_sh_is_pinned_to_the_batches_branch_and_author():
    unit = _render()
    prefix = re.search(r"^Environment=LAND_REQUIRE_BRANCH_PREFIX=(.*)$", unit, re.M)
    author = re.search(r"^Environment=LAND_REQUIRE_AUTHOR=(.*)$", unit, re.M)
    assert prefix and prefix.group(1) == autoland.BRANCH_PREFIX
    assert author and author.group(1) == autoland.AUTHOR


def test_the_timer_runs_every_ten_minutes_by_default():
    timer = _render(TIMER)
    assert "OnUnitActiveSec=10min" in timer
    assert autoland.MAX_STARTS == 2


def test_armed_only_on_daniel_box_and_disarming_removes_the_units():
    key = "claude_code_fanout_server_lander_enabled"
    assert DEFAULTS[key] is False
    assert load_yaml(HOST_VARS / "daniel-box.yml").get(key) is True
    assert not load_yaml(HOST_VARS / "daniel-server.yml").get(key, False)

    tasks = load_yaml(ROLE / "tasks" / "fanout_server_lander.yml")
    removal = [
        t
        for t in tasks
        if t.get("ansible.builtin.file", {}).get("state") == "absent"
        and "claude-fanout-server-lander.timer" in t.get("loop", [])
    ]
    assert removal and removal[0]["when"] == f"not {key}", (
        "disarming must remove the units, or a stale timer keeps landing"
    )


def test_the_role_imports_the_tasks_and_stamps_both_templates():
    main = (ROLE / "tasks" / "main.yml").read_text()
    assert "fanout_server_lander.yml" in main
    for name in (SERVICE, TIMER):
        assert f"claude_code/templates/{name}" in main, f"{name} is not stamped"


def test_the_contract_section_names_the_unit():
    doc = (ROLE / "CLAUDE.md").read_text()
    assert re.search(
        r"^## Autonomous-role contract \(`claude-fanout-server-lander`", doc, re.M
    )
