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

A role test that needs its observed output in CI's job log records it with
`record_property(JOB_LOG_PROPERTY, text)`. pytest shows captured stdout only for a failing
test, and under xdist only the controller writes the log, so a print in a passing test reaches
nothing. xdist carries a report's `user_properties` back to the controller, and
`pytest_terminal_summary` here prints each passed test's text under its node id.
"""

import sys
from pathlib import Path

ROLES = Path(__file__).resolve().parent
JOB_LOG_PROPERTY = "job_log"


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


def job_log_lines(reports) -> list[str]:
    """The lines `pytest_terminal_summary` prints for `reports`: each `job_log` text, headed."""
    lines = []
    for report in reports:
        for name, text in report.user_properties:
            if name == JOB_LOG_PROPERTY:
                lines += [f"--- job log: {report.nodeid}", *str(text).splitlines()]
    return lines


def pytest_terminal_summary(terminalreporter):
    for line in job_log_lines(terminalreporter.stats.get("passed", [])):
        terminalreporter.write_line(line)
