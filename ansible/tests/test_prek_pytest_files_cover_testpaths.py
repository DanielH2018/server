"""Guard: the prek `pytest` hook's `files` regex must match every pyproject `testpaths` entry.

The prek `pytest` hook (prek.toml) hand-mirrors pyproject.toml's `[tool.pytest.ini_options]`
testpaths so a LOCAL commit touching only one suite still runs pytest. It's a manual copy: add a
new suite to `testpaths` and forget the regex, and local commits touching only that suite silently
skip pytest (CI's `--all-files` is the only backstop). This test closes that drift the same way
`test_renovate_managers.py` closes the Renovate-manager drift — a new/renamed suite that isn't
covered fails here at commit time.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pytest_hook_files_regex():
    data = tomllib.loads((REPO_ROOT / "prek.toml").read_text())
    for repo in data.get("repos", []):
        for hook in repo.get("hooks", []):
            if hook.get("id") == "pytest":
                return hook["files"]
    raise AssertionError("no `pytest` hook found in prek.toml")


def _testpaths():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return data["tool"]["pytest"]["ini_options"]["testpaths"]


def test_prek_pytest_files_regex_covers_every_testpath():
    pattern = re.compile(_pytest_hook_files_regex())
    for testpath in _testpaths():
        # A test file created under this suite must be matched, else a local commit touching
        # only that suite would skip the pytest hook.
        sample = f"{testpath}/test_sample.py"
        assert pattern.search(sample), (
            f"prek `pytest` hook `files` regex does not match testpath {testpath!r} "
            f"(sample {sample!r}) — add it to the regex in prek.toml"
        )
