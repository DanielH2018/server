#!/usr/bin/env python3
"""The restart broker as the claude-code host component ships it.

The design is docs/claude-agent-user.md, "The restart allowlist". The broker's own behaviour is
tested beside it, in hosts/components/claude-code/tests/test_claude_restart.py.

Run: uv run pytest hosts/tests/test_claude_restart_broker.py
"""

import json
import re

import pytest
from _helpers import REPO
from _setup_render import CLAUDE_CODE, render_component_text
from deploy_tools.land_lib import policy
from lib import yaml_fast

UNIT = "claude-restart@.service.j2"
CONFIG = "claude-restart-config.json.j2"


def config(overrides: dict | None = None) -> dict:
    return json.loads(render_component_text("claude-code", CONFIG, overrides))


def shipped() -> list[dict]:
    spec = yaml_fast.safe_load((CLAUDE_CODE / "component.yml").read_text())
    return [e for e in spec["files"] if "claude-restart" in e["dst"]]


def test_the_unit_passes_only_its_instance_to_the_root_script() -> None:
    text = re.sub(r"\\\n\s*", " ", render_component_text("claude-code", UNIT))
    (start,) = [line for line in text.splitlines() if line.startswith("ExecStart=")]
    assert start.endswith("/opt/claude-restart/claude_restart.py %i")
    assert start.count("%") == 1
    assert "User=" not in text
    assert "OnFailure=homelab-alert@%n.service" in text


def test_an_inventory_value_cannot_list_a_workload_or_raise_a_cap() -> None:
    raised = {
        "restart_allowlist": {
            "traefik": {"namespace": "homelab", "deployment": "traefik"}
        },
        "claude_code_restart_allowlist": {"traefik": {}},
        "restart_daily_cap": 99,
        "restart_workload_cooldown_s": 1,
    }
    moved = config(raised)
    assert (moved["allowlist"], moved["daily_cap"], moved["workload_cooldown_s"]) == (
        {},
        3,
        86400,
    )


def test_the_broker_ships_only_behind_its_toggle_and_with_no_polkit_rule() -> None:
    entries = shipped()
    assert {e.get("src", e.get("lib")) for e in entries} == {
        "files/claude_restart.py",
        "host_lib.py",
        "weekly_window.py",
        f"templates/{CONFIG}",
        f"templates/{UNIT}",
    }
    assert {e["when"] for e in entries} == {"claude_code_restart_enabled"}
    rules = sorted((CLAUDE_CODE / "templates").glob("*.rules.j2"))
    assert "50-claude-land.rules.j2" in {r.name for r in rules}
    assert not [r.name for r in rules if "claude-restart" in r.read_text()]


@pytest.mark.parametrize(
    "path",
    [
        "hosts/components/claude-code/files/claude_restart.py",
        f"hosts/components/claude-code/templates/{UNIT}",
        f"hosts/components/claude-code/templates/{CONFIG}",
    ],
)
def test_a_pr_changing_the_broker_needs_the_operator(path, tmp_path) -> None:
    assert (REPO / path).is_file(), f"{path} moved; update this list"
    listed = tmp_path / "approval-paths"
    listed.write_text(
        render_component_text("claude-code", "claude-land-approval-paths.j2")
    )
    floor = policy.read_approval_paths(str(listed))
    assert policy.approval_hits([{"filename": path}], floor) == [path]
