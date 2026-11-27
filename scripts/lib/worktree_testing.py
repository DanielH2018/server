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

A monkeypatch cannot reach a removal that runs in a subprocess, such as a script under bash.
That test merges `ignore_foreign_slice_holds_env(tmp_path)` into the subprocess's
environment instead.
"""

from pathlib import Path as _Path


from lib import _claude_worktree  # noqa: F401
import claude_worktree

# The dotfiles package this process imported: deployed on a host, a pinned checkout in CI.
PACKAGE = _Path(claude_worktree.__file__ or "").resolve().parent


def ignore_foreign_slice_holds(monkeypatch) -> None:
    """Switch the slice rule off for one test; a cwd or `CLAUDE_PROJECT_DIR` hold still counts.

    Args:
        monkeypatch: pytest's fixture, so the rule comes back at teardown.
    """
    monkeypatch.setattr(
        claude_worktree, "_foreign_in_my_slice", lambda entry, uid: False
    )


def ignore_foreign_slice_holds_env(tmp_path: _Path) -> dict[str, str]:
    """The environment that switches the slice rule off in a Python subprocess.

    The subprocess finds `claude_worktree` through `CLAUDE_WORKTREE_HOME`
    (`lib._claude_worktree`). This writes a `claude_worktree.py` under `tmp_path` that runs
    the module this process imported, under the same name, then replaces the rule.

    Args:
        tmp_path: the test's scratch directory.

    Returns:
        `CLAUDE_WORKTREE_HOME`, to merge into the subprocess's environment.
    """
    home = tmp_path / "claude-worktree-no-slice-rule"
    home.mkdir()
    (home / "claude_worktree.py").write_text(
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location(\n"
        f"    'claude_worktree', {claude_worktree.__file__!r}\n"
        ")\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['claude_worktree'] = module\n"
        "spec.loader.exec_module(module)\n"
        "module._foreign_in_my_slice = lambda entry, uid: False\n"
    )
    return {"CLAUDE_WORKTREE_HOME": str(home)}
