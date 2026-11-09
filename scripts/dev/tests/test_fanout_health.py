"""The SessionStart banner lines `place launch` reads from a host for the brief.

Run: uv run pytest scripts/dev/tests/test_fanout_health.py
"""

import json

from fanout_lib.session_banner import HEALTH_CMD, health_lines
from _fanout_fakes import fake_tools, ok


def test_the_brief_takes_the_agent_context_and_drops_the_system_message():
    stdout = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "WARN: health check found issues:\n  ✗ sonarr down",
            },
            "systemMessage": "  ↳ worktree-merged-branch is merged and ready to remove",
        },
        ensure_ascii=False,
    )
    tools, run = fake_tools(answers={"daniel-box": ok(stdout + "\n")})
    assert health_lines(tools, "daniel-box") == [
        "[daniel-box] WARN: health check found issues:",
        "[daniel-box]   ✗ sonarr down",
    ]
    assert run.host_calls == [("daniel-box", HEALTH_CMD, None)]


def test_operator_lines_alone_leave_the_brief_nothing():
    stdout = json.dumps(
        {
            "hookSpecificOutput": {"hookEventName": "SessionStart"},
            "systemMessage": "  ↳ fan-out batch 1345 runs on daniel-server",
        }
    )
    tools, _ = fake_tools(answers={"daniel-box": ok(stdout)})
    assert health_lines(tools, "daniel-box") == []


def test_a_host_still_printing_the_plain_banner_keeps_each_line():
    tools, _ = fake_tools(answers={"daniel-box": ok("WARN: x\n  ✗ sonarr down\n")})
    assert health_lines(tools, "daniel-box") == [
        "[daniel-box] WARN: x",
        "[daniel-box]   ✗ sonarr down",
    ]
