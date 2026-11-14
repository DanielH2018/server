"""A role that ships `files/**/*.py` has a `tests/` directory, and that directory is in `testpaths`.

`.claude/rules/python-layout.md`: "A role that ships a `files/*.py` with logic gets a `tests/`
directory, which the `k8s/roles/*/tests` glob in `testpaths` collects." The two sibling guards start from TEST files —
`test_testpaths_covers_every_test_file.py` asks whether each test lies under a testpath, and
the `no-role-ships-a-pytest-file` census row asks whether a role ships one — so a role
with code and no tests at all is invisible to both.

This starts from the CODE: every tracked `k8s/roles/<role>/files/**/*.py` names a
role, and each such role must have a tracked `tests/` directory listed in `testpaths`. A test
directory present on disk but absent from `testpaths` is the failure the first sibling guard
catches once a test exists; asserting it here as well means a role cannot satisfy this guard
with an empty directory pytest never visits.
"""

import tomllib
from pathlib import PurePosixPath

from _helpers import REPO
from lib.proc_testing import run

# Roles the census must find. A `git ls-files` pattern that stops matching returns an empty
# set, and every assertion below passes over one — naming members turns that into a failure
# that says which role went missing.
_KNOWN_SHIPPERS = frozenset({"k8s/monitor-bridge", "k8s/karakeep", "k8s/ical-proxy"})


def _tracked(pattern: str) -> list[str]:
    listed = run(["git", "ls-files", "-z", "--", pattern], cwd=REPO, check=True).stdout
    return [rel for rel in listed.split("\0") if rel]


def _role_of(rel: str) -> str:
    """`k8s/roles/<role>/...` -> `k8s/<role>`."""
    return f"k8s/{PurePosixPath(rel).parts[2]}"


def _roles_with(tracked_files, subdir: str) -> set[str]:
    """The `k8s/<role>` set whose `<subdir>/` holds one of the paths.

    A git pathspec `*` spans `/`, so `k8s/roles/*/files/*.py` also matches a file nested under
    `files/`; the fourth path component is what says which directory the file is under.
    """
    return {
        _role_of(rel)
        for rel in tracked_files
        if len(PurePosixPath(rel).parts) > 4 and PurePosixPath(rel).parts[3] == subdir
    }


def roles_shipping_python(tracked_files) -> set[str]:
    return _roles_with(tracked_files, "files")


def roles_with_tests(tracked_files) -> set[str]:
    return _roles_with(tracked_files, "tests")


def roles_in_testpaths(testpaths, roles) -> set[str]:
    """The `roles` whose `tests/` directory a `testpaths` entry names, literally or by glob."""
    return {
        role
        for role in roles
        if any(
            PurePosixPath(f"k8s/roles/{role.removeprefix('k8s/')}/tests").full_match(e)
            for e in testpaths
        )
    }


def roles_missing_tests(shipping, with_tests, in_testpaths) -> dict[str, str]:
    """role -> what is missing, for every role that ships Python without a collected test dir."""
    missing = {}
    for role in sorted(shipping):
        if role not in with_tests:
            missing[role] = "no tests/ directory"
        elif role not in in_testpaths:
            missing[role] = "tests/ exists but is not in pyproject.toml testpaths"
    return missing


def test_every_role_shipping_python_has_a_collected_tests_dir() -> None:
    shipping = roles_shipping_python(_tracked("k8s/roles/*/files/*.py"))
    assert _KNOWN_SHIPPERS <= shipping, (
        f"census no longer finds {sorted(_KNOWN_SHIPPERS - shipping)}; sees {sorted(shipping)}"
    )
    with_tests = roles_with_tests(_tracked("k8s/roles/*/tests/*.py"))
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    in_testpaths = roles_in_testpaths(
        data["tool"]["pytest"]["ini_options"]["testpaths"], with_tests
    )
    missing = roles_missing_tests(shipping, with_tests, in_testpaths)
    assert not missing, (
        "these roles ship files/**/*.py with no tests pytest collects: "
        + "; ".join(f"{role}: {why}" for role, why in missing.items())
        + ". Add k8s/roles/<role>/tests/ and list it in testpaths."
    )


def test_a_role_with_code_and_no_tests_dir_is_flagged() -> None:
    files = ["k8s/roles/newthing/files/thing.py"]
    missing = roles_missing_tests(
        roles_shipping_python(files),
        roles_with_tests([]),
        roles_in_testpaths([], set()),
    )
    assert missing == {"k8s/newthing": "no tests/ directory"}


def test_a_tests_dir_absent_from_testpaths_is_flagged() -> None:
    files = ["k8s/roles/newthing/files/thing.py"]
    tests = ["k8s/roles/newthing/tests/test_thing.py"]
    missing = roles_missing_tests(
        roles_shipping_python(files),
        roles_with_tests(tests),
        roles_in_testpaths(["scripts"], roles_with_tests(tests)),
    )
    assert missing == {
        "k8s/newthing": "tests/ exists but is not in pyproject.toml testpaths"
    }


def test_a_role_with_a_collected_tests_dir_is_clean() -> None:
    files = ["k8s/roles/thing/files/sub/mod.py"]
    tests = ["k8s/roles/thing/tests/test_mod.py"]
    assert roles_shipping_python(files) == {"k8s/thing"}
    missing = roles_missing_tests(
        roles_shipping_python(files),
        roles_with_tests(tests),
        roles_in_testpaths(
            ["tests", "k8s/roles/thing/tests"],
            roles_with_tests(tests),
        ),
    )
    assert missing == {}
