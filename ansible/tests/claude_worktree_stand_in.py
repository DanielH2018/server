"""Feed the suite a `claude_worktree` when the dotfiles deploy is absent (CI, a bare host).

Registered as a plugin from `addopts` in pyproject.toml, beside `leakguard`, so it covers
every `testpaths` entry and every subprocess a test spawns — the SessionStart banner test
runs `session-health.py` as a real subprocess, which imports `prune_worktrees` off its own
sys.path and never sees an in-process stand-in.

`scripts/dev/_claude_worktree.py` is the real bootstrap and raises when the package is not
deployed; its `DECIDED:` says why there is no fallback at runtime. This is NOT that
fallback: it never touches `sys.path` for the deployed case, it is a test fixture rather
than a runtime import, and `_claude_worktree_stand_in/claude_worktree.py` is a
byte-identical copy of the deployed module that
`scripts/dev/tests/test_claude_worktree_import.py::test_the_ci_stand_in_is_the_deployed_module`
diffs against the real one on a deployed host — a diff CI itself cannot run, which is why
that test is deployed-only and goes red under `prek run` here first.

The env var is the seam: `CLAUDE_WORKTREE_HOME` is what the bootstrap reads, so pointing
it at the stand-in directory makes the real bootstrap import the copy, in this process
and in every subprocess that inherits the environment. `__claude_worktree_stand_in__` on
the module is the marker the deployed-only tests skip on.
"""

import importlib
import os
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_STAND_IN = _TESTS / "_claude_worktree_stand_in"

try:
    import _claude_worktree  # noqa: F401  (real bootstrap; populates sys.modules on success)
except ImportError:
    os.environ["CLAUDE_WORKTREE_HOME"] = str(_STAND_IN)
    sys.path.insert(0, str(_STAND_IN))
    importlib.import_module("claude_worktree")

# Marked by where the module came from, not by which branch ran: an xdist worker inherits
# the env var the controller set above, so in the worker the real bootstrap succeeds --
# against the stand-in -- and the except branch never runs.
_module = sys.modules["claude_worktree"]
if Path(_module.__file__ or "").resolve().parent == _STAND_IN:
    _module.__claude_worktree_stand_in__ = True
