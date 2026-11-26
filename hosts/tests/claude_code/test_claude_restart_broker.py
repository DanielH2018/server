#!/usr/bin/env python3
"""The restart broker as the claude-code host component ships it.

The design is docs/claude-agent-user.md, "The restart allowlist". The broker's own behaviour is
tested beside it, in hosts/components/claude-code/tests/test_claude_restart.py.

Run: uv run pytest hosts/tests/claude_code/test_claude_restart_broker.py
"""

import json
import os
import re
import subprocess
import sys

import pytest
from _helpers import REPO
from _setup_render import CLAUDE_CODE, render_component_text
from deploy_tools.land_lib import policy
from lib import yaml_fast
from renderer.renderer_lib import host as host_mode

UNIT = "claude-restart@.service.j2"
CONFIG = "claude-restart-config.json.j2"
RULE = "50-claude-restart.rules.j2"


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
    assert (moved["daily_cap"], moved["workload_cooldown_s"]) == (3, 86400)
    assert moved == config()


def test_the_broker_ships_only_behind_its_toggle() -> None:
    entries = shipped()
    assert {e.get("src") or e.get("lib") or e["shared"] for e in entries} == {
        "files/claude_restart.py",
        "host_lib.py",
        "k8s_conditions.py",
        "weekly_window.py",
        f"templates/{CONFIG}",
        f"templates/{UNIT}",
        f"templates/{RULE}",
    }
    assert all("claude_code_restart_enabled" in e["when"] for e in entries)
    # The rule names the triage controller, a user only triage's toggles create.
    (rule,) = [e for e in entries if e.get("src") == f"templates/{RULE}"]
    assert "claude_code_triage_enabled" in rule["when"]


def test_the_shipped_broker_imports_from_the_package_layout_alone(tmp_path) -> None:
    files = host_mode.render_host("daniel-box").files
    for dst, (body, _mode) in files.items():
        if dst.startswith("/opt/claude-restart/"):
            target = tmp_path / dst.removeprefix("/opt/claude-restart/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
    # `-S` drops the venv's `.pth`, so only the files beside the script resolve its imports.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-S", str(tmp_path / "claude_restart.py"), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def rule_pattern() -> re.Pattern:
    (found,) = re.findall(
        r"!/(\^[^/]+\$)/\.test\(", render_component_text("claude-code", RULE)
    )
    return re.compile(found)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("homelab-claude-restart@docs.service", True),
        ("homelab-claude-restart@docs --all.service", False),
        ("homelab-claude-restart@docs.service.d", False),
        ("x-homelab-claude-restart@docs.service", False),
    ],
)
def test_the_rule_admits_only_the_broker_in_the_scripts_name_shape(
    name, expected
) -> None:
    assert bool(rule_pattern().search(name)) is expected


def test_only_the_triage_controller_may_start_the_broker_and_only_start_it() -> None:
    """The operator's 2026-11-20 ruling: the controller, never `claude` or the session user."""
    rendered = render_component_text("claude-code", RULE)
    users = re.findall(r'subject\.user !== "([^"]+)"', rendered)
    assert users == ["claude-triage-ctl"]
    assert 'action.lookup("verb") !== "start"' in rendered
    assert rendered.count("polkit.Result.YES") == 1
    # The rule's instance shape is the script's NAME, so polkit admits no name the script would call
    # malformed.
    script = (CLAUDE_CODE / "files" / "claude_restart.py").read_text()
    (name,) = re.findall(r'^NAME = re\.compile\(r"\^(.+)\$"\)$', script, re.MULTILINE)
    assert rule_pattern().pattern == rf"^homelab-claude-restart@{name}\.service$"


@pytest.mark.parametrize(
    "path",
    sorted(f"hosts/components/claude-code/{e['src']}" for e in shipped() if "src" in e),
)
def test_a_pr_changing_the_broker_needs_the_operator(path, tmp_path) -> None:
    assert (REPO / path).is_file(), f"{path} moved; update this list"
    listed = tmp_path / "approval-paths"
    listed.write_text(
        render_component_text("claude-code", "claude-land-approval-paths.j2")
    )
    floor = policy.read_approval_paths(str(listed))
    assert policy.approval_hits([{"filename": path}], floor) == [path]
