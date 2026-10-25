"""The prek files-cover-imports census resolves a test module's imports through pytest's `pythonpath`.

A hook whose entry is a test module runs under pytest, which puts every `pythonpath` root in
`pyproject.toml` on `sys.path`. A bare `from _row_table import ...` in
`ansible/tests/repo/` resolves only that way, so a census that skips those roots stops the
closure there and cannot see a `files` gate that misses the module (#5921).
"""

from pathlib import Path

import pytest
from lib.repo_paths import REPO
from test_prek_files_cover_entry_imports import (
    HOOKS,
    gaps,
    import_closure,
    imported_files,
)

_ROW_TABLE = (REPO / "ansible/tests/_row_table.py").resolve()


def _entries(hook_id: str) -> list[tuple[str, Path, dict]]:
    found = [
        (entry.relative_to(REPO).as_posix(), entry, hook)
        for found_id, entry, hook in HOOKS
        if found_id == hook_id
    ]
    assert found, f"prek.toml has no Python-entry hook {hook_id!r}"
    return found


def test_a_test_modules_bare_row_table_import_resolves_through_pythonpath():
    module = REPO / "ansible/tests/repo/test_census_rows_python.py"
    assert _ROW_TABLE in imported_files(module)


def test_the_static_ratchet_hook_covers_the_row_table_its_entry_imports():
    entry_rel = "ansible/tests/repo/test_census_rows_python.py"
    by_entry = {
        rel: (entry, hook) for rel, entry, hook in _entries("static-ratchet-tests")
    }
    entry, hook = by_entry[entry_rel]
    closure = import_closure(entry)
    assert _ROW_TABLE in closure
    assert gaps(hook, closure) == []


# Each hook's named member is a module its entry reaches only through a `pythonpath` root,
# measured on 2026-10-22 by adding those roots to the census for modules under `tests/`.
@pytest.mark.parametrize(
    ("hook_id", "member"),
    [
        ("static-ratchet-tests-lander", "ansible/filter_plugins/filter_errors.py"),
        ("static-ratchet-tests-checksum", "scripts/validate/k8s_manifests.py"),
        ("static-ratchet-tests-shell", "ansible/filter_plugins/filter_errors.py"),
        ("static-ratchet-tests-renovate", "ansible/filter_plugins/filter_errors.py"),
    ],
)
def test_a_hook_gate_covers_the_modules_its_entry_reaches_through_pythonpath(
    hook_id, member
):
    closures = {
        rel: (import_closure(entry), hook) for rel, entry, hook in _entries(hook_id)
    }
    assert any(
        (REPO / member).resolve() in closure for closure, _ in closures.values()
    ), f"{hook_id}'s import closure does not reach {member} through pytest's pythonpath"
    missing = {rel: gaps(hook, closure) for rel, (closure, hook) in closures.items()}
    assert not any(missing.values()), missing
