"""Limit a removal test's in-use check to the processes the test itself starts.

WHY A SHARED MODULE. `lib.worktrees.processes_using` scans the real /proc before every
removal. Its slice rule (#3994) counts an unreadable process of another non-root uid inside
this uid's login slice as a holder of every tree. One `sudo -u claude` running anywhere in
that slice therefore refuses every removal a test makes, and the test fails for a reason
unrelated to its change (#8730). The rule lives in the dotfiles `claude_worktree` module, so
the private name a test must patch is decided here once.

Import it from any test, at any depth. `pyproject.toml` puts `scripts/` on `pythonpath`::

    from lib.worktree_testing import ignore_foreign_slice_holds

    ignore_foreign_slice_holds(monkeypatch)
    ok, err = remove(str(repo), tree)
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib import _claude_worktree  # noqa: F401
import claude_worktree


def ignore_foreign_slice_holds(monkeypatch) -> None:
    """Switch the slice rule off for one test; a cwd or `CLAUDE_PROJECT_DIR` hold still counts.

    Args:
        monkeypatch: pytest's fixture, so the rule comes back at teardown.
    """
    monkeypatch.setattr(
        claude_worktree, "_foreign_in_my_slice", lambda entry, uid: False
    )
