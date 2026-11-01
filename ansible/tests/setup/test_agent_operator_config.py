#!/usr/bin/env python3
"""The agent's CLAUDE.md imports the operator's copied CLAUDE.md only while the switch is true.

`templates/agent-user-claude-md.j2` carries the `@operator/CLAUDE.md` import that
`claude_code_agent_operator_config` gates. The packaged file is read by the agent's sessions, so
a leg that slips leaves a dangling import or instructions the operator switched off.

Run: uv run pytest ansible/tests/setup/test_agent_operator_config.py
"""

import re

from _setup_render import render_component_text

SWITCH = "claude_code_agent_operator_config"


def test_the_template_imports_the_copied_file_only_while_the_switch_is_true() -> None:
    # Claude Code resolves a relative @import against the importing file's directory.
    on = render_component_text("claude-code", "agent-user-claude-md.j2", {SWITCH: True})
    off = render_component_text(
        "claude-code", "agent-user-claude-md.j2", {SWITCH: False}
    )
    assert re.findall(r"^@\S+$", on, re.M) == ["@operator/CLAUDE.md"]
    assert "operator/CLAUDE.md" not in off and not re.search(r"^@", off, re.M)
