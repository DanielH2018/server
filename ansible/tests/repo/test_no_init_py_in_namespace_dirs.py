"""No `__init__.py` under `scripts/`, `ansible/tests/`, or a role's `tests/`.

Those directories resolve as PEP 420 namespace packages on purpose (repo CLAUDE.md): pytest
names a test module by its basename under `rootdir`-relative rules, and an `__init__.py` in
one of them changes the module name of every test beneath it, breaking the basename-unique
convention `test_pythonpath_module_basenames.py` relies on and the `from <dir> import <mod>`
form every `scripts/` importer uses. An editor or a scaffolding tool adds one silently.

Run: uv run pytest ansible/tests/repo/test_no_init_py_in_namespace_dirs.py
"""

import subprocess

from _helpers import REPO

NAMESPACE_DIRS = ("scripts/", "ansible/tests/", "ansible/roles/")


def tracked_init_files(listed: str) -> list[str]:
    """Every `__init__.py` in a `git ls-files -z` listing that sits under a namespace dir."""
    return sorted(
        rel
        for rel in listed.split("\0")
        if rel.endswith("__init__.py")
        and rel.startswith(NAMESPACE_DIRS)
        # A role ships its Python from `files/`; only its `tests/` is a namespace dir.
        and (not rel.startswith("ansible/roles/") or "/tests/" in rel)
    )


def test_no_namespace_dir_carries_an_init_file():
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    assert "scripts/lib/yaml_fast.py" in listed.split("\0"), (
        "the listing is not the repo"
    )
    assert tracked_init_files(listed) == []


def test_an_init_file_in_a_namespace_dir_is_flagged():
    listed = "\0".join(
        [
            "scripts/lib/__init__.py",
            "ansible/tests/__init__.py",
            "ansible/roles/k8s/svc/tests/__init__.py",
            "ansible/roles/k8s/svc/files/pkg/__init__.py",
            "Email-to-RSS/src/__init__.py",
        ]
    )
    assert tracked_init_files(listed) == [
        "ansible/roles/k8s/svc/tests/__init__.py",
        "ansible/tests/__init__.py",
        "scripts/lib/__init__.py",
    ]
