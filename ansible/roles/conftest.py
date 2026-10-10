"""A test under `<plane>/<role>/tests/` imports its own role's `files/` modules by bare name.

pytest puts a test module's own directory on `sys.path`, plus the `pythonpath` entries in
`pyproject.toml`. A role's tests live in `tests/` and the code they cover in the sibling
`files/`, which is what the role ships, so every role test used to carry the same
`sys.path.insert(0, <role>/files)` line. This hook is that line, once (#3746).

The rule is per role on purpose. A test collected from `k8s/crowdsec/tests/` gets
`k8s/crowdsec/files/` and no other role's `files/`, so a test cannot import another role's
module and pass while the same import fails at deploy time, where a role ships only its own
`files/`. A module shared across roles goes on `pythonpath` instead, as `game-stats/files` and
`monitor-bridge/files` do.

The insert happens when pytest collects the file, before it imports the module, and stays for
the rest of the session, exactly as a module-level insert did. Two roles' modules therefore
still share one `sys.path`; the `no-two-import-roots-share-a-module-basename` row of
`ansible/tests/repo/test_census_rows_python.py` keeps their basenames unique (#2608).

`scripts/tests/test_test_module_bootstraps_present.py` loads `role_files_dir` from this file,
credits the directory it names, and refuses an insert of it as redundant.
"""

import sys
from pathlib import Path

ROLES = Path(__file__).resolve().parent


def role_files_dir(path: Path, roles: Path = ROLES) -> Path | None:
    """The `files/` directory of the role whose `tests/` holds `path`, or None.

    `roles` is a parameter so the guard's accept/reject pairs can hand it a synthetic tree.
    """
    try:
        parts = Path(path).resolve().relative_to(roles.resolve()).parts
    except ValueError:
        return None
    if len(parts) < 4 or parts[2] != "tests":
        return None
    files = roles.resolve() / parts[0] / parts[1] / "files"
    return files if files.is_dir() else None


def pytest_collect_file(file_path: Path, parent):
    files = role_files_dir(file_path)
    if files is not None and str(files) not in sys.path:
        sys.path.insert(0, str(files))
