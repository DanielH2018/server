"""A role that ships `files/**/*.py` has a `tests/` directory, and that directory is in `testpaths`.

Root CLAUDE.md (Python & Tests): "A role that ships a `files/*.py` with logic adds its `tests/`
directory to `testpaths`." Nothing enforced it. The two sibling guards start from TEST files —
`test_testpaths_covers_every_test_file.py` asks whether each test lies under a testpath, and
`test_no_role_ships_a_test_file.py` asks whether a test sits beside shipped code — so a role
with code and no tests at all is invisible to both. `karakeep/files/karakeep-time-tagger.py`
(479 lines, run from a ConfigMap) had no `tests/` and no `testpaths` entry until #1853.

This starts from the CODE: every tracked `ansible/roles/<plane>/<role>/files/**/*.py` names a
role, and each such role must have a tracked `tests/` directory listed in `testpaths`. A test
directory present on disk but absent from `testpaths` is the failure the first sibling guard
catches once a test exists; asserting it here as well means a role cannot satisfy this guard
with an empty directory pytest never visits.
"""

import subprocess
import tomllib
from pathlib import PurePosixPath

from _helpers import REPO

# Roles the census must find. A `git ls-files` pattern that stops matching returns an empty
# set, and every assertion below passes over one — naming members turns that into a failure
# that says which role went missing.
_KNOWN_SHIPPERS = frozenset(
    {"k8s/monitor-bridge", "setup/gitops_deploy", "k8s/karakeep", "k8s/ical-proxy"}
)


def _tracked(pattern: str) -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", pattern],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [rel for rel in listed.split("\0") if rel]


def _role_of(rel: str) -> str:
    """`ansible/roles/<plane>/<role>/...` -> `<plane>/<role>`."""
    parts = PurePosixPath(rel).parts
    return f"{parts[2]}/{parts[3]}"


def _roles_with(tracked_files, subdir: str) -> set[str]:
    """The `<plane>/<role>` set whose `<subdir>/` holds one of the paths.

    A git pathspec `*` spans `/`, so `roles/*/*/files/*.py` also matches a file nested under
    `files/`; the fifth path component is what says which directory the file is under.
    """
    return {
        _role_of(rel)
        for rel in tracked_files
        if len(PurePosixPath(rel).parts) > 5 and PurePosixPath(rel).parts[4] == subdir
    }


def roles_shipping_python(tracked_files) -> set[str]:
    return _roles_with(tracked_files, "files")


def roles_with_tests(tracked_files) -> set[str]:
    return _roles_with(tracked_files, "tests")


def roles_in_testpaths(testpaths) -> set[str]:
    found = set()
    for entry in testpaths:
        parts = PurePosixPath(entry).parts
        if (
            len(parts) == 5
            and parts[:2] == ("ansible", "roles")
            and parts[4] == "tests"
        ):
            found.add(f"{parts[2]}/{parts[3]}")
    return found


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
    shipping = roles_shipping_python(_tracked("ansible/roles/*/*/files/*.py"))
    assert _KNOWN_SHIPPERS <= shipping, (
        f"census no longer finds {sorted(_KNOWN_SHIPPERS - shipping)}; sees {sorted(shipping)}"
    )
    with_tests = roles_with_tests(_tracked("ansible/roles/*/*/tests/*.py"))
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    in_testpaths = roles_in_testpaths(
        data["tool"]["pytest"]["ini_options"]["testpaths"]
    )
    missing = roles_missing_tests(shipping, with_tests, in_testpaths)
    assert not missing, (
        "these roles ship files/**/*.py with no tests pytest collects: "
        + "; ".join(f"{role}: {why}" for role, why in missing.items())
        + ". Add ansible/roles/<plane>/<role>/tests/ and list it in testpaths."
    )


def test_a_role_with_code_and_no_tests_dir_is_flagged() -> None:
    files = ["ansible/roles/k8s/newthing/files/thing.py"]
    missing = roles_missing_tests(
        roles_shipping_python(files), roles_with_tests([]), roles_in_testpaths([])
    )
    assert missing == {"k8s/newthing": "no tests/ directory"}


def test_a_tests_dir_absent_from_testpaths_is_flagged() -> None:
    files = ["ansible/roles/k8s/newthing/files/thing.py"]
    tests = ["ansible/roles/k8s/newthing/tests/test_thing.py"]
    missing = roles_missing_tests(
        roles_shipping_python(files),
        roles_with_tests(tests),
        roles_in_testpaths(["scripts"]),
    )
    assert missing == {
        "k8s/newthing": "tests/ exists but is not in pyproject.toml testpaths"
    }


def test_a_role_with_a_collected_tests_dir_is_clean() -> None:
    files = ["ansible/roles/setup/thing/files/sub/mod.py"]
    tests = ["ansible/roles/setup/thing/tests/test_mod.py"]
    missing = roles_missing_tests(
        roles_shipping_python(files),
        roles_with_tests(tests),
        roles_in_testpaths(["ansible/tests", "ansible/roles/setup/thing/tests"]),
    )
    assert missing == {}
