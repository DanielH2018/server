#!/usr/bin/env python3
"""Guard that the prek `pytest` hook's `files` regex stays a superset of pyproject's testpaths.

The hook's `files` regex is hand-maintained to "mirror pyproject.toml [tool.pytest.ini_options]
testpaths" (see the comment above it in prek.toml) so a LOCAL commit touching only one suite still
re-runs pytest — CI runs `--all-files` regardless, but the local/CI parity is the whole point of the
mirror. If a new testpath is added without updating the regex, the local fast-path silently skips
that suite, and the drift only surfaces as a CI-only failure later. This asserts every testpath dir
has a `.py` path the hook regex matches, so the drift fails at commit time — the same lockstep-guard
pattern as test_renovate_managers.py's shellcheck-py / portainer / python-version pins.

Run: uv run pytest scripts/test_prek_pytest_paths.py
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _testpaths() -> list[str]:
    data = tomllib.loads((_REPO / "pyproject.toml").read_text())
    return data["tool"]["pytest"]["ini_options"]["testpaths"]


def _pytest_hook_files_re() -> re.Pattern:
    prek = tomllib.loads((_REPO / "prek.toml").read_text())
    for repo in prek["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") == "pytest":
                return re.compile(hook["files"])
    raise AssertionError("no pytest hook found in prek.toml")


@pytest.mark.parametrize("testpath", _testpaths())
def test_pytest_hook_regex_covers_testpath(testpath: str) -> None:
    files_re = _pytest_hook_files_re()
    # A .py file directly under the testpath must match the hook's `files` regex; otherwise a local
    # commit editing only that suite skips pytest (CI --all-files still runs it, but the parity the
    # hook exists for is broken).
    probe = f"{testpath}/_probe.py"
    assert files_re.match(probe), (
        f"testpath {testpath!r} has no .py path matched by the prek pytest hook's `files` regex "
        f"(tried {probe!r}) — add it to prek.toml's pytest hook `files` so a local single-suite "
        f"commit still re-runs pytest."
    )
