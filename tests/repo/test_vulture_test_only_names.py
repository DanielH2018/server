"""Each production name that only a test calls is deleted or carries a vulture ignore reason.

The names come from a vulture pass with the test trees excluded, which reports each one, and
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
from pathlib import Path

import pytest
from _helpers import REPO
from lib.proc_testing import run

PYPROJECT = REPO / "pyproject.toml"

TEST_ONLY_NAMES = (
    ("scripts/lib/kubectl.py", "RETIRED_CLUSTER_NODES"),
    ("scripts/dev/review_metrics.py", "COVERAGE_DIR"),
    ("scripts/dev/review_metrics.py", "validate_row"),
    ("scripts/secrets_mgmt/secrets_lib/secret_registry.py", "record_names"),
    ("scripts/renderer/renderer_lib/dialect.py", "off_the_list"),
    ("scripts/renderer/renderer_lib/bundle.py", "rules_missing"),
    ("scripts/renderer/renderer_lib/host_package.py", "installed_paths"),
    ("scripts/lib/stdlib_only.py", "stdlib_only_set"),
    ("scripts/lib/stdlib_only.py", "runtime_import"),
    ("scripts/renderer/renderer_lib/restart.py", "any_pod_template"),
    ("scripts/deploy_tools/land_lib/options.py", "ENV_KNOBS"),
    ("scripts/deploy_tools/promote_lib/reboot_window.py", "end_cron"),
    ("scripts/diagnostics/grafana_panel_report.py", "worth_renavigating"),
    ("scripts/lib/role_installs.py", "no_project_spans"),
    ("scripts/validate/render_lib/setup_plane.py", "standalone_templates"),
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

CENSUS = Path(__file__).resolve().relative_to(REPO).as_posix()


def _tracked(*patterns: str) -> list[str]:
    out = run(["git", "ls-files", "--", *patterns], cwd=REPO, check=True)
    return out.stdout.split()


def _defines(path: str, name: str, root: Path = REPO) -> bool:
    """Whether the module at `root/path` binds `name` anywhere: a def, a class or an assignment."""
    module = root / path
    if not module.is_file():
        return False
    for node in ast.walk(ast.parse(module.read_text())):
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


def _ignore_entry(name: str, pyproject: Path = PYPROJECT) -> str | None:
    """The `ignore_names` entry that matches `name` the way vulture does, by glob."""
    entries = tomllib.loads(pyproject.read_text())["tool"]["vulture"]["ignore_names"]
    return next((e for e in entries if fnmatch.fnmatchcase(name, e)), None)


def _reason(entry: str, pyproject: Path = PYPROJECT) -> list[str]:
    """The comment lines directly above the run of entry lines that holds `entry`."""
    lines = pyproject.read_text().splitlines()
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


def _test_tree_names(
    name: str, paths: list[str] | None = None, root: Path = REPO
) -> bool:
    """Whether a file under a tests/ directory, other than this census, names `name` as a whole word."""
    word = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
    return any(
        word.search((root / path).read_text())
        for path in (_tracked("*.py") if paths is None else paths)
        if "/tests/" in f"/{path}" and path != CENSUS
    )


@pytest.mark.parametrize(
    ("path", "name"),
    TEST_ONLY_NAMES,
    ids=[f"{p.rsplit('/', 1)[-1]}::{n}" for p, n in TEST_ONLY_NAMES],
)
def test_a_test_only_name_is_ignored_with_a_reason(path, name):
    assert path in _tracked(path), (
        f"{path} is not tracked: repoint its TEST_ONLY_NAMES rows to the module's new path"
    )
    assert _defines(path, name), (
        f"{path} no longer binds {name!r}: delete its TEST_ONLY_NAMES row if the name is "
        "gone, otherwise repoint the row to where the name went"
    )
    entry = _ignore_entry(name)
    assert entry is not None, (
        f"{path} still defines {name!r}, which only tests call, and [tool.vulture] "
        "ignore_names does not list it: delete it with its unit test, or list it with the "
        "guard that consumes it as the reason (#8632)"
    )
    assert _reason(entry), (
        f"ignore_names lists {entry!r} with no comment above it giving a reason"
    )
    assert _test_tree_names(name), (
        f"ignore_names keeps {name!r} for a guard, but no test under a tests/ directory "
        "other than this census names it as a whole word"
    )


def test_defines_finds_every_binding_form_is_clean(tmp_path):
    (tmp_path / "mod.py").write_text(
        "def func(): pass\n"
        "async def coro(): pass\n"
        "class Klass: pass\n"
        "ASSIGNED = 1\n"
        "ANNOTATED: int = 2\n"
    )
    for name in ("func", "coro", "Klass", "ASSIGNED", "ANNOTATED"):
        assert _defines("mod.py", name, tmp_path), name


def test_defines_misses_an_unbound_name_is_flagged(tmp_path):
    (tmp_path / "mod.py").write_text(
        "import os\nresult = os.getcwd()\nobj.attr = 1\nprint('quoted_name')\n"
    )
    assert not _defines("mod.py", "quoted_name", tmp_path)
    assert not _defines("mod.py", "attr", tmp_path)
    assert not _defines("gone.py", "result", tmp_path)


PYPROJECT_TEXT = """\
[tool.vulture]
ignore_names = [
  "bare_entry",
  # A reason for the run below.
  "reasoned_entry",
  "run_mate",
  "do_*",
]

[tool.other]
key = 1
"""


def test_ignore_entry_and_reason_find_a_listed_name_is_clean(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_TEXT)
    assert _ignore_entry("do_GET", pyproject) == "do_*"
    assert _ignore_entry("reasoned_entry", pyproject) == "reasoned_entry"
    assert _reason("reasoned_entry", pyproject) == ["# A reason for the run below."]
    assert _reason("run_mate", pyproject) == ["# A reason for the run below."]


def test_ignore_entry_and_reason_miss_an_unlisted_or_unreasoned_name_is_flagged(
    tmp_path,
):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT_TEXT)
    assert _ignore_entry("unlisted_name", pyproject) is None
    assert _ignore_entry("xdo_GET", pyproject) is None
    assert _reason("bare_entry", pyproject) == []


def test_test_tree_names_matches_a_whole_word_is_clean(tmp_path):
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "pkg" / "tests" / "test_a.py").write_text("x = foo(1)\n")
    assert _test_tree_names("foo", ["pkg/tests/test_a.py"], tmp_path)


def test_test_tree_names_rejects_a_longer_word_or_a_non_test_file_is_flagged(tmp_path):
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "pkg" / "tests" / "test_a.py").write_text("foobar(1)\n_foo = 2\n")
    (tmp_path / "pkg" / "prod.py").write_text("foo(1)\n")
    paths = ["pkg/tests/test_a.py", "pkg/prod.py"]
    assert not _test_tree_names("foo", paths, tmp_path)
