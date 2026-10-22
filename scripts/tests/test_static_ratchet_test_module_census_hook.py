"""The two test-module census files run in their own `static-ratchet-tests-*` hook (#5599).

`static-ratchet-tests` runs under `--dist loadscope`, so its slowest module bounds the hook,
and that module is `scripts/tests/test_census_rows_test_renders.py`. Both
`test_census_rows_test_*` modules read only `pytest_only_modules()` and their own import
closure, so they belong in a sibling hook gated on exactly that, which a commit touching only
non-test `.py` files skips.

Run: uv run pytest scripts/tests/test_static_ratchet_test_module_census_hook.py
"""

import re
import tomllib

from _row_table import tracked
from _test_module_rules import pytest_only_modules
from test_prek_files_cover_entry_imports import import_closure

from lib.repo_paths import REPO

_CENSUS_MODULES = (
    "scripts/tests/test_census_rows_test_modules.py",
    "scripts/tests/test_census_rows_test_renders.py",
)

_PREK = REPO / "prek.toml"


def _hooks() -> dict[str, dict]:
    config = tomllib.loads(_PREK.read_text())
    return {
        hook["id"]: hook
        for repo in config.get("repos", [])
        for hook in repo.get("hooks", [])
    }


def _sibling() -> tuple[str, dict] | None:
    """The `static-ratchet-tests-*` hook whose entry runs both census modules, if one does."""
    for hook_id, hook in _hooks().items():
        entry = hook.get("entry", "").split()
        if hook_id.startswith("static-ratchet-tests-") and all(
            module in entry for module in _CENSUS_MODULES
        ):
            return hook_id, hook
    return None


def _runs(hook: dict, rel: str) -> bool:
    if hook.get("always_run"):
        return True
    exclude = hook.get("exclude")
    return bool(re.search(hook.get("files", ""), rel)) and not (
        exclude and re.search(exclude, rel)
    )


def test_static_ratchet_tests_no_longer_runs_the_test_module_censuses():
    entry = _hooks()["static-ratchet-tests"]["entry"].split()
    still = [module for module in _CENSUS_MODULES if module in entry]
    assert not still, (
        f"`static-ratchet-tests` still runs {still}; the slowest of them bounds the hook "
        "under --dist loadscope. Move them to a `static-ratchet-tests-*` sibling."
    )


def test_a_sibling_hook_runs_both_test_module_censuses():
    assert _sibling() is not None, (
        f"no `static-ratchet-tests-*` hook in prek.toml runs both {_CENSUS_MODULES}"
    )


def test_the_sibling_runs_when_any_census_module_changes():
    found = _sibling()
    assert found is not None, "no `static-ratchet-tests-*` hook runs the census modules"
    hook_id, hook = found
    census = pytest_only_modules()
    assert census, "pytest_only_modules() found nothing"
    missed = [rel for rel in census if not _runs(hook, rel)]
    assert not missed, (
        f"{hook_id} skips a commit touching these test modules: {missed[:20]}"
    )


def test_a_commit_touching_only_non_test_python_skips_the_sibling():
    found = _sibling()
    assert found is not None, "no `static-ratchet-tests-*` hook runs the census modules"
    hook_id, hook = found
    closure = {
        path.relative_to(REPO).as_posix()
        for module in _CENSUS_MODULES
        for path in import_closure(REPO / module)
    }
    tests = set(pytest_only_modules())
    non_test = [
        rel
        for rel in tracked("*.py")
        if rel not in tests
        and rel not in closure
        and "tests" not in rel.split("/")
        and not rel.endswith("conftest.py")
    ]
    # A named member the derivation must keep: deploy.sh's library, which no census imports.
    assert "scripts/deploy_tools/deploy_lib/run.py" in non_test
    fired = [rel for rel in non_test if _runs(hook, rel)]
    assert not fired, (
        f"{hook_id} runs on non-test Python outside its closure: {fired[:20]}"
    )


# "On daniel-server at load average 4.6-6.5, it took 14.3-16.8s": a load range, then a time range.
_SERVER_FIGURE = re.compile(
    r"daniel-server at load average (?P<load>[\d.]+(?:-[\d.]+)?),? (?:it )?took "
    r"(?P<took>[\d.]+(?:-[\d.]+)?)s"
)


def _upper(span: str) -> float:
    return float(span.split("-")[-1])


def test_the_cost_comment_quotes_a_daniel_server_run_under_15s_near_load_3():
    lines = _PREK.read_text().splitlines()
    end = lines.index('id = "static-ratchet-tests"')
    start = end
    while start > 0 and (
        lines[start - 1].startswith("#") or lines[start - 1] == "[[repos.hooks]]"
    ):
        start -= 1
    comment = " ".join(line.lstrip("# ").strip() for line in lines[start:end])
    figures = [
        (_upper(m["load"]), _upper(m["took"])) for m in _SERVER_FIGURE.finditer(comment)
    ]
    assert figures, (
        "the static-ratchet-tests cost comment quotes no daniel-server figure"
    )
    assert any(load <= 4.0 and took < 15.0 for load, took in figures), (
        "no daniel-server figure in the static-ratchet-tests cost comment is under 15s at "
        f"load average near 3: {figures}"
    )


# Modules the census reaches through pytest's `pythonpath`, which `import_closure` does not
# resolve: both census modules import `_row_table`, which imports `lib.proc_testing` and
# `_helpers`, which imports `lib.yaml_fast`.
_PYTHONPATH_CLOSURE = (
    "ansible/tests/_row_table.py",
    "ansible/tests/_helpers.py",
    "scripts/lib/proc_testing.py",
    "scripts/lib/yaml_fast.py",
)


def test_the_sibling_runs_when_a_module_reached_through_pythonpath_changes():
    found = _sibling()
    assert found is not None, "no `static-ratchet-tests-*` hook runs the census modules"
    hook_id, hook = found
    missed = [rel for rel in _PYTHONPATH_CLOSURE if not _runs(hook, rel)]
    assert not missed, (
        f"{hook_id} skips a commit touching the census's imports: {missed}"
    )
