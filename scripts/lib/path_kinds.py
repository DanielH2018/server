"""Which changed paths are documentation or test-suite material that no host receives.

The landing's promotion wait, its serial-lock rule and the cluster-pin gate all skip these
paths, so they share one definition here.
"""


def is_doc(path: str) -> bool:
    """Whether a changed path is documentation that reaches no host."""
    return path.endswith(".md")


def is_test_only_path(path: str) -> bool:
    """Whether a changed path is test-suite material that no role ever ships to a host.

    Two shapes. The directory shape is the one the tree uses: `tests/` holds the
    repo-wide guards plus `_helpers.py`, which matches no name pattern at all, and every other
    suite sits in a role-local or per-script-directory `tests/`, a layout
    `tests/repo/suite/test_testpaths_covers_every_test_file.py` enforces. The name shape,
    a `test_*.py` or `conftest.py` wherever it sits, covers `scripts/conftest.py` and a test a
    session adds beside code before that guard catches it.

    The invariant this rests on: no role copies a test file to a host. The
    `no-role-ships-a-pytest-file` row of `tests/repo/census_rows/test_census_rows_roles.py` guards
    it, so a role that starts shipping one fails there rather than widening this predicate.
    """
    if path.startswith("tests/"):
        return True
    parts = path.split("/")
    if "tests" in parts[:-1]:
        return True
    name = parts[-1]
    return name == "conftest.py" or (name.startswith("test_") and name.endswith(".py"))


def _reaches_no_render(path: str) -> bool:
    """A document or a test, which no render reads; a `.md` a role ships under `files/` may."""
    if is_test_only_path(path):
        return True
    return is_doc(path) and not (path.startswith("k8s/roles/") and "/files/" in path)


def moves_the_bundle(paths) -> bool:
    """Whether a changed path may move the bundle `promote` publishes (D6.12).

    Every path but a document or a test may: the landing waits on land-wait, whose changed
    set says what moved, and a PR that moved nothing waits on `homelab` alone. A prefix list
    of render inputs would end a PR it missed at `nothing-to-deploy` unwaited.
    """
    return not all(_reaches_no_render(p) for p in paths)
