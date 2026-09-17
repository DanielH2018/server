#!/usr/bin/env python3
"""Bootstrap that puts the deployed `claude_guard` package on `sys.path` and imports it.

The seed of the future `homelab_guard/__init__.py` — the dotfiles repo's design spec
(`docs/specs/2026-09-06-claude-guard-design.md`, slice 5) plans a package by that name
consolidating this repo's Bash hooks around `claude_guard.segment` and `claude_guard.tables`.
This module carries only the narrowed slice: the import path for `claude_guard.tables`, which
`_readonly_tables.py` reads. The rest of the consolidation is a later slice.

`claude_guard` is not on the repo's `uv` environment — it lives outside this repo, deployed by
chezmoi to `~/.local/share/claude-guard`. Import this module before anything from
`claude_guard`; it inserts that path at `sys.path[0]` only when the directory exists and the
package is not already importable, then imports `claude_guard` itself so a caller can rely on
`sys.modules["claude_guard"]` being populated.

# DECIDED: no fallback to a stale local copy when the deploy is missing. Raise instead. The
# slice-3 ledger's #484 half-deploy (~/.claude/artifacts/claude-guard-slice3/sdd-ledger/
# progress.md) is why: a machine mid-deploy can have the new hook code without yet having the
# dotfiles package behind it, and a private fallback copy would keep serving a permission
# decision that looks current but was pinned at whatever the fallback last held — nobody sees
# that it fell behind. Crashing here is what the allow-side hook's contract requires:
# `SSH_HOSTS`/`_SSH_SECRET` gate an auto-approve, and a hook that cannot import its own tables
# must not approve anything at all. Measured, not assumed: the shim `exec`s this process, so
# the ImportError surfaces as a nonzero exit with the traceback on stderr, not a clean
# no-output exit — Claude Code's own handling of a hook that exits that way is outside what
# this repo controls, but the one thing this module guarantees is that no auto-approve
# decision comes out of it.
"""

import sys
from pathlib import Path

_CLAUDE_GUARD_DIR = Path("~/.local/share/claude-guard").expanduser()

if "claude_guard" not in sys.modules and _CLAUDE_GUARD_DIR.is_dir():
    sys.path.insert(0, str(_CLAUDE_GUARD_DIR))

try:
    import claude_guard
except ImportError as exc:
    raise ImportError(
        f"claude_guard package not found at {_CLAUDE_GUARD_DIR} or on sys.path. "
        "It is deployed by the dotfiles repo (chezmoi apply) — run that on this host, "
        "or check that ~/.local/share/claude-guard exists."
    ) from exc

del claude_guard
