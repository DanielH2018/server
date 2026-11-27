"""Bootstrap that puts the deployed `claude_worktree` module on `sys.path` and imports it.

`claude_worktree` is the dotfiles repo's worktree library: the readers, the landed ladder,
the holds, `classify` and `remove`. The worktree pruner beside it,
`~/.claude/hooks/prune-worktrees.py`, is the same repo's. Neither is on this repo's `uv`
environment: chezmoi deploys the library to `~/.local/share/claude-worktree`, beside
`claude-guard`, and `CLAUDE_WORKTREE_HOME` overrides that path. Import this module before
anything from `claude_worktree`; `lib.worktrees` does, and every consumer in this repo imports
the library from there.

`REQUIRED_INTERFACE` is the `claude_worktree.INTERFACE` this repo was written for. A deployed
copy older than that raises here, and the weekly sweep passes the same number to the pruner
as `--require`
(`hosts/tests/repo_crons/test_worktree_sweep_cron_reports_its_exit.py::test_the_sweep_requires_the_interface_this_repo_was_written_for`).

# DECIDED: no fallback to a stale local copy when the deploy is missing or old. Raise instead.
# The same reasoning as `.claude/hooks/_claude_guard.py`: a private fallback keeps
# serving a verdict that looks current but was pinned at whatever the copy last held,
# and nobody sees it fall behind. Here the verdicts decide what to DELETE, and every
# importer's dangerous action — `findings.py reap`, `fanout.py place clean` — is a removal,
# so an ImportError is the KEEP direction: with the package absent nothing is reported and
# nothing is removed. The SessionStart banner
# is the one importer that must not crash, and `session-health.py` already catches the
# ImportError and prints it. CI gets the real module the way a host does: the `pytest`
# job in `.github/workflows/ci.yml` checks out the dotfiles repo at a pinned SHA and links
# the package into `~/.local/share/`.
"""

# pyright: reportMissingImports=false

import os
import sys
from pathlib import Path

REQUIRED_INTERFACE = 2

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

_deployed = getattr(claude_worktree, "INTERFACE", 1)
if _deployed < REQUIRED_INTERFACE:
    raise ImportError(
        f"claude_worktree at {claude_worktree.__file__} is interface {_deployed} and this "
        f"repo needs {REQUIRED_INTERFACE}. It is deployed by the dotfiles repo (chezmoi "
        "apply) — run that on this host."
    )

del claude_worktree
