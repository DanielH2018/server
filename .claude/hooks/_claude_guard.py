#!/usr/bin/env python3
# gen-hooks: library
#   reason: imported by block-protected-bash.py and _readonly_tables.py to bootstrap the claude_guard package
"""Bootstrap that puts the deployed `claude_guard` package on `sys.path` and imports it.

The seed of the future `homelab_guard/__init__.py` — the dotfiles repo's design spec
(`docs/specs/2026-09-06-claude-guard-design.md`, slice 5) plans a package by that name
consolidating this repo's Bash hooks around `claude_guard.segment` and `claude_guard.tables`.
This module carries the import path only. `_readonly_tables.py` reads `claude_guard.tables`
(the ssh tables, and since #2052 the verb table `TIER1` is derived from), and
`block-protected-bash.py` reads `claude_guard.segment` (#2053). The rest of the consolidation
is a later slice.

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
# must not approve anything at all. This module raises; `_readonly_tables.py`, the module
# every allow-side entry point imports, turns that into the shims' own fail-open shape (one
# stderr line, exit 0, no stdout). `test_the_hook_fails_open_when_the_deploy_is_missing`
# measures that end to end.
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
