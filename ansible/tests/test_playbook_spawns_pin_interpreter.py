"""Every test that spawns a real `ansible-playbook` must pin its Python interpreter.

`ansible.cfg` enables a jsonfile fact cache at `~/.cache/ansible/facts` with a two-hour TTL.
The cache key is the host — `localhost` — which is the same key from every worktree, while the
value it stores is `discovered_interpreter_python`, a path into one tree's `.venv`. So the last
tree to run Ansible pins its own interpreter for every other tree on the machine.

That is invisible until the pinned tree goes away. `prune_worktrees.py --prune` removing a
merged worktree is enough: from then until the entry expires, every play these tests spawn dies
with rc 127 on a path that no longer exists, in every tree, including a clean master.

Measured 2026-08-22, on master at 78358ddb with no local changes:

    The module interpreter '.../worktrees/git-identity-leak-fix/.venv/bin/python3.14'
    was not found.

Four tests failed that way. Nothing about the failure points at the cache: the tests assert on
playbook output, so it surfaces as `assert 'GUARD_PASSED' in ''`, and `interpreter_python =
auto_silent` (ansible.cfg:21) suppresses the discovery message that would name the substitution.

These plays all target `-i localhost,` and never need discovery, so setting the interpreter
explicitly removes the dependency outright. It closes both halves: the run stops reading another
tree's path, and — because discovery is skipped — stops writing its own path back for the next
tree to trip on.
"""

from __future__ import annotations

import ast
from pathlib import Path
from _helpers import REPO as _REPO


_SCAN_DIRS = (_REPO / "ansible/tests", _REPO / "scripts")
_ENV_KEY = "ANSIBLE_PYTHON_INTERPRETER"

# The spawn sites known when this guard was written. Asserted below so that a refactor which
# moves or renames them fails loudly here rather than leaving the guard matching nothing and
# passing vacuously.
_KNOWN = {
    "test_longhorn_api.py",
    "test_volume_revert_input_guard.py",
    "test_volume_snapshot_register.py",
    "test_setup_render_manifest.py",
}


def _spawns_a_playbook(node: ast.AST) -> bool:
    """True for a subprocess call whose argv literal starts with `ansible-playbook`."""
    if not isinstance(node, ast.Call):
        return False
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in ("run", "Popen", "call", "check_output"):
        return False
    if not node.args or not isinstance(node.args[0], ast.List):
        return False
    argv = node.args[0].elts
    return (
        bool(argv)
        and isinstance(argv[0], ast.Constant)
        and argv[0].value == "ansible-playbook"
    )


def _pins_the_interpreter(fn: ast.AST) -> bool:
    """True if anything in `fn` assigns the env key, e.g. `env[_ENV_KEY] = sys.executable`."""
    return any(
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == _ENV_KEY
        for node in ast.walk(fn)
    )


def _spawn_sites() -> list[tuple[Path, str, bool]]:
    """(path, enclosing function name, whether it pins the interpreter) per spawn site."""
    sites: list[tuple[Path, str, bool]] = []
    for scan_dir in _SCAN_DIRS:
        for path in sorted(scan_dir.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not any(_spawns_a_playbook(node) for node in ast.walk(fn)):
                    continue
                sites.append((path, fn.name, _pins_the_interpreter(fn)))
    return sites


def test_every_playbook_spawn_pins_its_interpreter() -> None:
    unpinned = [
        f"{path.relative_to(_REPO)}::{name}"
        for path, name, pinned in _spawn_sites()
        if not pinned
    ]
    assert not unpinned, (
        f"these spawn a real ansible-playbook without setting {_ENV_KEY}: {unpinned}. "
        "They will inherit whichever worktree's .venv the shared fact cache last recorded, "
        "and fail with rc 127 once that worktree is removed. Set "
        f'env["{_ENV_KEY}"] = sys.executable alongside the other ANSIBLE_* env keys.'
    )


def test_the_guard_still_matches_the_known_spawn_sites() -> None:
    """A guard that matches nothing passes for the wrong reason."""
    found = {path.name for path, _, _ in _spawn_sites()}
    assert _KNOWN <= found, (
        f"expected spawn sites not found: {sorted(_KNOWN - found)}. Either they moved, or the "
        "AST matcher above stopped recognising the call shape — fix the matcher, or update "
        "_KNOWN if the site is genuinely gone."
    )
