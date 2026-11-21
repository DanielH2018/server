"""image-leg.yml's `paths:` filter matches every in-repo file the image-marked tests load.

The image leg runs on a pull request only when its diff matches the workflow's `paths:` list,
so a module the image tests import that the list misses can change alone, start no image run,
and break a test only the nightly sweep then reaches (#9749). The files a run loads are the
import closure of each module holding an `image` mark, of every `conftest.py` pytest loads above
it, and of every `-p` plugin in `addopts`, which `-m image` does not drop. The closure is read
the way the prek files-cover-imports census reads it. The local actions the job `uses:` count
too, because `$/` pins each one to the pull request's own commit.
"""

import ast
import shlex
import tomllib
from collections.abc import Iterable
from pathlib import Path

from _ratchet_census import tracked_files
from _renovate import _minimatch_to_regex
from lib import yaml_fast
from lib.repo_paths import REPO
from test_prek_files_cover_entry_imports import _PYTEST_ROOTS, import_closure

WORKFLOW = REPO / ".github/workflows/image-leg.yml"


def _marks_image(path: Path) -> bool:
    text = path.read_text()
    if "mark.image" not in text:
        return False
    return any(
        isinstance(node, ast.Attribute)
        and node.attr == "image"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
        for node in ast.walk(ast.parse(text))
    )


def image_modules() -> list[Path]:
    """Every tracked test module that marks a test `image`."""
    return [
        REPO / rel
        for rel in tracked_files("*.py")
        if Path(rel).name.startswith("test_") and _marks_image(REPO / rel)
    ]


def _plugins() -> list[Path]:
    """The in-repo modules `addopts` loads with `-p`."""
    addopts = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"][
        "ini_options"
    ]["addopts"]
    args = shlex.split(addopts)
    names = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-p"]
    return [
        root / f"{name}.py"
        for name in names
        for root in _PYTEST_ROOTS
        if (root / f"{name}.py").is_file()
    ]


def _conftests(module: Path) -> list[Path]:
    return [
        directory / "conftest.py"
        for directory in module.parents
        if directory.is_relative_to(REPO) and (directory / "conftest.py").is_file()
    ]


def loaded_files(tests: Iterable[Path]) -> set[str]:
    """The repo-relative `.py` files a pytest run of `tests` imports."""
    roots = set(_plugins())
    for test in tests:
        roots |= {test, *_conftests(test)}
    closure = set().union(*(import_closure(root) for root in roots))
    return {path.relative_to(REPO).as_posix() for path in closure}


def _workflow() -> dict:
    return yaml_fast.safe_load(WORKFLOW.read_text())


def local_actions(workflow: dict) -> set[str]:
    """The `action.yml` of each repository action a job step `uses:`."""
    found = set()
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            if uses.startswith(("$/", "./")):
                found.add(f"{uses[2:].rstrip('/')}/action.yml")
    return found


def pull_request_paths(workflow: dict) -> list[str]:
    triggers = workflow.get("on", workflow.get(True))
    return triggers["pull_request"]["paths"]


def uncovered(paths: list[str], files: Iterable[str]) -> list[str]:
    """The files a pull request could change alone without matching `paths`."""
    globs = [_minimatch_to_regex(pattern) for pattern in paths]
    return sorted(f for f in files if not any(g.match(f) for g in globs))


def test_the_census_reads_the_plugins_conftests_and_helpers_of_the_image_tests():
    tests = image_modules()
    assert REPO / "k8s/roles/crowdsec/tests/test_jobs_image.py" in tests, tests
    files = loaded_files(tests)
    for module in (
        "tests/leakguard.py",
        "tests/conftest.py",
        "tests/_helpers.py",
        "scripts/lib/yaml_fast.py",
    ):
        assert module in files, module
    assert ".github/actions/setup/action.yml" in local_actions(_workflow())


def test_a_change_to_any_file_the_image_tests_load_starts_the_image_leg():
    workflow = _workflow()
    files = loaded_files(image_modules()) | local_actions(workflow)
    missing = uncovered(pull_request_paths(workflow), files)
    assert not missing, (
        "the image tests load these files, but no `paths:` entry under `pull_request` in "
        ".github/workflows/image-leg.yml matches them, so a pull request changing one alone "
        "starts no image run. Add each to the list:\n" + "\n".join(missing)
    )


def test_a_paths_list_naming_only_the_test_flags_what_it_imports():
    test = "k8s/roles/crowdsec/tests/test_jobs_image.py"
    missing = uncovered([test], loaded_files([REPO / test]))
    assert test not in missing
    assert "scripts/lib/yaml_fast.py" in missing
    assert "tests/leakguard.py" in missing
