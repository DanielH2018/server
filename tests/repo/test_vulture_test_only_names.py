"""Each production name that only a test calls is deleted or carries a vulture ignore reason.

The names come from #8632: a vulture pass with the test trees excluded reports each one, and
a test under a `tests/` directory is its only caller. Each name is triaged one of two ways.
Either its own unit test is the only consumer, so the name goes. Or a guard is its consumer,
so the name stays and `[tool.vulture] ignore_names` lists it under a comment giving that
reason.

Run: uv run pytest tests/repo/test_vulture_test_only_names.py
"""

import ast
import fnmatch
import re
import tomllib

import pytest
from _helpers import REPO
from lib.proc_testing import run

PYPROJECT = REPO / "pyproject.toml"

TEST_ONLY_NAMES = (
    ("scripts/lib/facts/citations.py", "REJECT_REASONS"),
    ("scripts/lib/kubectl.py", "RETIRED_CLUSTER_NODES"),
    ("scripts/dev/review_metrics.py", "COVERAGE_DIR"),
    ("scripts/dev/review_metrics.py", "validate_row"),
    ("scripts/secrets_mgmt/secrets_lib/secret_registry.py", "record_names"),
    ("scripts/renderer/renderer_lib/dialect.py", "off_the_list"),
    ("scripts/renderer/renderer_lib/bundle.py", "rules_missing"),
    ("scripts/renderer/renderer_lib/host_package.py", "installed_paths"),
    ("scripts/lib/stdlib_only.py", "stdlib_only_set"),
    ("scripts/lib/stdlib_only.py", "runtime_import"),
    ("scripts/renderer/renderer_lib/secret_checks.py", "scan_template"),
    ("scripts/renderer/renderer_lib/restart.py", "any_pod_template"),
    ("scripts/deploy_tools/land_lib/options.py", "ENV_KNOBS"),
    ("scripts/deploy_tools/promote_lib/reboot_window.py", "end_cron"),
    ("scripts/diagnostics/grafana_panel_report.py", "worth_renavigating"),
    ("scripts/lib/role_installs.py", "no_project_spans"),
    ("scripts/validate/setup_templates.py", "standalone_templates"),
    ("scripts/secrets_mgmt/git_dates.py", "historical_names"),
    ("scripts/secrets_mgmt/git_dates.py", "undeclared_departures"),
    ("scripts/secrets_mgmt/git_dates.py", "revived_departures"),
    # The guard libraries the issue names by module: vulture's report for each on 2026-11-14.
    ("scripts/lib/facts/citations.py", "FORMS"),
    ("scripts/lib/facts/citations.py", "node_citations"),
    ("scripts/lib/facts/citations.py", "line_numbered_citations"),
    ("scripts/lib/facts/citations.py", "guard_path_citations"),
    ("scripts/lib/facts/citations.py", "macro_citations"),
    ("scripts/lib/facts/citations.py", "heading_pointers"),
    ("scripts/lib/facts/citations.py", "names_a_part"),
    ("scripts/lib/invocation_sites.py", "prek_hook_entries"),
    ("scripts/lib/invocation_sites.py", "workflow_run_steps"),
    ("scripts/lib/invocation_sites.py", "systemd_exec_lines"),
    ("scripts/lib/invocation_sites.py", "claude_settings_entries"),
    ("scripts/lib/systemd_timer.py", "longest_gap_s"),
    ("scripts/lib/systemd_timer.py", "timeout_start_s"),
    ("scripts/lib/cli_registry.py", "assert_complete"),
    ("scripts/lib/cli_registry.py", "package_entry_points"),
)


def _tracked(*patterns: str) -> list[str]:
    out = run(["git", "ls-files", "--", *patterns], cwd=REPO, check=True)
    return out.stdout.split()


def _defines(path: str, name: str) -> bool:
    """Whether the tracked module still binds `name` anywhere: a def, a class or an assignment."""
    if path not in _tracked(path):
        return False
    tree = ast.parse((REPO / path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return True
    return False


def _ignore_entry(name: str) -> str | None:
    """The `ignore_names` entry that matches `name` the way vulture does, by glob."""
    entries = tomllib.loads(PYPROJECT.read_text())["tool"]["vulture"]["ignore_names"]
    return next((e for e in entries if fnmatch.fnmatchcase(name, e)), None)


def _reason(entry: str) -> list[str]:
    """The comment lines directly above the run of entry lines that holds `entry`."""
    lines = PYPROJECT.read_text().splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.startswith("ignore_names = [")
    )
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "]")
    block = [line.strip() for line in lines[start + 1 : end]]
    at = next(i for i, line in enumerate(block) if f'"{entry}"' in line)
    while at >= 0 and block[at].startswith('"'):
        at -= 1
    comment = []
    while at >= 0 and block[at].startswith("#"):
        comment.insert(0, block[at])
        at -= 1
    return comment


def _test_tree_mentions(name: str) -> bool:
    for path in _tracked("*.py"):
        if "/tests/" in f"/{path}" and name in (REPO / path).read_text():
            return True
    return False


@pytest.mark.parametrize(
    ("path", "name"),
    TEST_ONLY_NAMES,
    ids=[f"{p.rsplit('/', 1)[-1]}::{n}" for p, n in TEST_ONLY_NAMES],
)
def test_a_test_only_name_is_deleted_or_ignored_with_a_reason(path, name):
    if not _defines(path, name):
        return
    entry = _ignore_entry(name)
    assert entry is not None, (
        f"{path} still defines {name!r}, which only tests call, and [tool.vulture] "
        "ignore_names does not list it: delete it with its unit test, or list it with the "
        "guard that consumes it as the reason (#8632)"
    )
    assert _reason(entry), (
        f"ignore_names lists {entry!r} with no comment above it giving a reason"
    )
    assert _test_tree_mentions(name), (
        f"ignore_names keeps {name!r} for a guard, but no file under a tests/ directory names it"
    )


def test_defines_finds_a_bound_name_is_clean():
    assert _defines("scripts/lib/kubectl.py", "CLUSTER_NODES")
    assert _defines("scripts/lib/kubectl.py", "WrongCluster")


def test_defines_misses_an_unbound_name_is_flagged():
    assert not _defines("scripts/lib/kubectl.py", "no_such_name_8632")
    assert not _defines("scripts/lib/no_such_module_8632.py", "CLUSTER_NODES")


def test_ignore_entry_matches_by_glob_is_clean():
    assert _ignore_entry("server_hostname") == "server_hostname"
    assert _ignore_entry("do_GET") == "do_*"
    assert _reason("server_hostname")


def test_ignore_entry_misses_an_unlisted_name_is_flagged():
    assert _ignore_entry("no_such_name_8632") is None


@pytest.mark.parametrize("path", sorted({p for p, _ in TEST_ONLY_NAMES}))
def test_every_row_names_a_tracked_module(path):
    """A moved module would make its rows pass as deleted; the row must follow the move."""
    assert path in _tracked(path), (
        f"{path} is not tracked: repoint its TEST_ONLY_NAMES rows"
    )


def _test_tree_names(name: str) -> bool:
    word = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
    return any(
        word.search((REPO / path).read_text())
        for path in _tracked("*.py")
        if "/tests/" in f"/{path}"
        and path != "tests/repo/test_vulture_test_only_names.py"
    )


@pytest.mark.parametrize(
    ("path", "name"),
    TEST_ONLY_NAMES,
    ids=[f"{p.rsplit('/', 1)[-1]}::{n}" for p, n in TEST_ONLY_NAMES],
)
def test_a_kept_name_is_named_whole_by_a_test_outside_this_census(path, name):
    if not _defines(path, name):
        return
    assert _test_tree_names(name), (
        f"{name!r} is kept for a guard, but no test under a tests/ directory other than this "
        "census names it as a whole word"
    )


def test_a_name_only_inside_a_longer_word_is_flagged():
    assert not _test_tree_names("no_such_name_8632")
    assert not _test_tree_names("ORMS_8632")
