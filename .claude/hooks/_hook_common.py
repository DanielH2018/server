#!/usr/bin/env python3
"""Shared helpers for the PreToolUse hooks (auto-approve-readonly.py, block-protected-edits.py).

Both hooks run standalone under the repo's uv python with the hooks dir as ``sys.path[0]`` (the
``exec uv run ... python .../X.py`` shim), and the test suite loads each hook by path from this same
dir, so a plain ``from _hook_common import ...`` resolves in both. Stdlib-only — the hooks must stay
dependency-free."""

from __future__ import annotations

import json


def emit_pretooluse_decision(decision: str, reason: str) -> None:
    """Print the Claude Code PreToolUse permission-decision JSON — ``decision`` is ``"allow"`` or
    ``"deny"``, ``reason`` the human-readable justification the harness surfaces."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
