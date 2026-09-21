"""Bootstrap that puts the deployed `claude_worktree` module on `sys.path` and imports it.

`claude_worktree` holds the readers `prune_worktrees.py` shares with the dotfiles
`prune-worktrees.py` SessionStart hook — the `Worktree` record, `parse_worktree_list`,
`session_is_alive`, `cherry_says_landed`, `merge_tree_says_contained`, `default_ref`
(#2133). It is not on the repo's `uv` environment: chezmoi deploys it to
`~/.local/share/claude-worktree`, beside `claude-guard`, and `CLAUDE_WORKTREE_HOME`
overrides that path. Import this module before anything from `claude_worktree`.

# DECIDED: no fallback to a stale local copy when the deploy is missing. Raise instead.
# The same reasoning as `.claude/hooks/_claude_guard.py`: a private fallback keeps
# serving a verdict that looks current but was pinned at whatever the copy last held,
# and nobody sees it fall behind. Here the verdicts decide what to DELETE, and every
# importer's dangerous action — `prune_worktrees.py --prune`, `findings.py reap`,
# `fanout_place.py clean` — is a removal, so an ImportError is the KEEP direction: with
# the package absent nothing is reported and nothing is removed. The SessionStart banner
# is the one importer that must not crash, and `session-health.py` already catches the
# ImportError and prints it. CI, which has no dotfiles deploy, gets the readers from the
# `claude_worktree_stand_in` pytest plugin (`ansible/tests/`), a byte-identical copy a
# deployed-host test diffs against the real one.
"""

import os
import sys
from pathlib import Path

_CLAUDE_WORKTREE_HOME = Path(
    os.environ.get("CLAUDE_WORKTREE_HOME", "~/.local/share/claude-worktree")
).expanduser()

if "claude_worktree" not in sys.modules and _CLAUDE_WORKTREE_HOME.is_dir():
    sys.path.insert(0, str(_CLAUDE_WORKTREE_HOME))

try:
    import claude_worktree
except ImportError as exc:
    raise ImportError(
        f"claude_worktree not found at {_CLAUDE_WORKTREE_HOME} or on sys.path. "
        "It is deployed by the dotfiles repo (chezmoi apply) — run that on this host, "
        "or point CLAUDE_WORKTREE_HOME at a checkout of it."
    ) from exc

del claude_worktree
