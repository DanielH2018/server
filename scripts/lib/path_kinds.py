"""Which changed paths are documentation or test-suite material that no host receives.

The landing's promotion wait and the cluster-pin gate both skip these paths, so they share
one definition here.
"""


def is_doc(path: str) -> bool:
    """Whether a changed path is documentation that reaches no host."""
    return path.endswith(".md")


def is_test_only_path(path: str) -> bool:
    """Whether a changed path is test-suite material that no role ever ships to a host.

    Two shapes. The directory shape is the one the tree uses: `ansible/tests/` holds the
    repo-wide guards plus `_helpers.py`, which matches no name pattern at all, and every other
    suite sits in a role-local or per-script-directory `tests/`, a layout
    `ansible/tests/repo/test_testpaths_covers_every_test_file.py` enforces. The name shape,
    a `test_*.py` or `conftest.py` wherever it sits, covers `scripts/conftest.py` and a test a
    session adds beside code before that guard catches it.

    The invariant this rests on: nothing under `ansible/` copies a test file to a host. The
    `no-role-ships-a-pytest-file` row of `ansible/tests/repo/test_census_rows_roles.py` guards
    it, so a role that starts shipping one fails there rather than widening this predicate.
    """
    if path.startswith("ansible/tests/"):
        return True
    parts = path.split("/")
    if "tests" in parts[:-1]:
        return True
    name = parts[-1]
    return name == "conftest.py" or (name.startswith("test_") and name.endswith(".py"))
