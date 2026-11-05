"""A task file `role_callers` cannot load makes each reader refuse or widen (#7298).

The issue leaves open whether `role_callers` raises or its readers widen, so each test drives a
reader and accepts either. `sonarr` includes `helper` from a clean file. `radarr`'s only include
sits in a file that does not load, and a reader that names `sonarr` alone has named too few.

Run: uv run pytest scripts/deploy_tools/tests/test_role_callers_unloadable_task_file.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from _deploy_sh_fakes import make_snapshot_repo, run_front_half, scrub_process_git_env
from _narrow_fixtures import DECLARED, Tree, _refs, build_tree
from deploy_tools import shared_role_callers
from deploy_tools.land_lib import classify
from deploy_tools.narrow_lib import broad as narrow_broad
from lib import k8s_role_callers
from lib.narrow_git import CannotNarrow
from lib.proc_testing import run
from lib.repo_paths import INVENTORY
from lib.repo_paths import REPO

INCLUDE = "- ansible.builtin.include_role:\n    name: k8s/helper\n"
HELPER_TEMPLATE = "ansible/roles/k8s/helper/templates/thing.yaml.j2"
# Everything `land_shared` imports, for the copy `_landing_in_a_copy` runs.
LANDING_CODE = (
    "scripts",
    "hosts/lib",
    "ansible/filter_plugins",
    "ansible/roles/setup/gitops_deploy/files",
    "ansible/roles/k8s/monitor-bridge/files",
)

UNLOADABLE = pytest.mark.parametrize(
    "broken",
    ["- [unclosed\n", "- &x {block: [*x]}\n"],
    ids=["syntax", "recursive-alias"],
)


def _write_callers(root: Path, clean: str, dropped: str, broken: str) -> None:
    """Two callers of `helper`, proving the edge is read before breaking the file holding it."""
    roles = root / "ansible" / "roles" / "k8s"
    (roles / "helper" / "templates").mkdir(parents=True)
    (roles / "helper" / "templates" / "thing.yaml.j2").write_text("kind: Thing\n")
    for caller in (clean, dropped):
        (roles / caller / "tasks").mkdir(parents=True, exist_ok=True)
        (roles / caller / "tasks" / "helper.yml").write_text(INCLUDE)
    assert k8s_role_callers.role_callers(root)["helper"] == {clean, dropped}
    (roles / dropped / "tasks" / "helper.yml").write_text(INCLUDE + broken)


@pytest.fixture
def tree(tmp_path, monkeypatch) -> Tree:
    scrub_process_git_env(monkeypatch)
    return build_tree(tmp_path)


def test_the_broad_narrowing_refuses_or_names_the_dropped_caller(tree: Tree):
    _write_callers(tree.root, "sonarr", "radarr", "- [unclosed\n")
    tree.commit("radarr's include does not load")
    tree.write(HELPER_TEMPLATE, "kind: Changed\n")
    old, new = _refs(tree)
    try:
        tags = narrow_broad.narrow(old, new, cwd=tree.root, declared=DECLARED)
    except narrow_broad.CannotNarrow:
        return
    assert "radarr" in tags, f"narrowed to {sorted(tags)}, which misses radarr"


def _landing_in_a_copy(root: Path, declared: set[str]) -> subprocess.CompletedProcess:
    """`shared_caller_tags` for a helper change, from a copy of the code that walks `root`.

    `role_callers()` with no argument walks the checkout its own file sits in, so a throwaway
    tree needs a copy of the code as well as its own roles.
    """
    for rel in LANDING_CODE:
        shutil.copytree(
            REPO / rel,
            root / rel,
            ignore=shutil.ignore_patterns("tests", "__pycache__"),
            dirs_exist_ok=True,
        )
    code = (
        "import json\n"
        "from deploy_tools.land_lib import land_shared\n"
        f"reached = land_shared.shared_caller_tags([{HELPER_TEMPLATE!r}], {declared!r})\n"
        "print(json.dumps(sorted(reached['helper'])))\n"
    )
    return run([sys.executable, "-c", code], cwd=root / "scripts")


def test_the_landing_refuses_or_names_the_dropped_caller(tmp_path):
    declared = {"sonarr", "radarr", "bazarr"}
    _write_callers(tmp_path, "sonarr", "radarr", "")
    done = _landing_in_a_copy(tmp_path, declared)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == ["radarr", "sonarr"]

    (tmp_path / "ansible/roles/k8s/radarr/tasks/helper.yml").write_text(
        INCLUDE + "- [unclosed\n"
    )
    done = _landing_in_a_copy(tmp_path, declared)
    assert done.returncode != 0 or "radarr" in json.loads(done.stdout), done.stdout


def test_the_tags_expansion_leaves_the_name_or_names_the_dropped_caller(
    tmp_path, monkeypatch
):
    """A name left as typed is safe, because the tag validation refuses it by name."""
    repo = make_snapshot_repo(tmp_path / "repo")
    _write_callers(repo, "alpha", "beta", "- [unclosed\n")
    _, calls = run_front_half(monkeypatch, repo, ["--tags", "helper", "--check"])
    validated = next((call[1] for call in calls if call[0] == "validate"), None)
    assert validated is None or {"helper", "beta"} & set(validated), validated


@UNLOADABLE
def test_the_owned_only_split_refuses_naming_the_file_with_no_traceback(
    tree: Tree, capsys, broken
):
    _write_callers(tree.root, "sonarr", "radarr", broken)
    tree.commit("radarr's include does not load")
    capsys.readouterr()
    assert shared_role_callers.main(["helper", "--repo", str(tree.root)]) == 1
    assert "radarr/tasks/helper.yml does not load" in capsys.readouterr().err


def test_a_landing_that_changes_no_shared_role_is_not_stopped_by_a_bad_task_file(
    tmp_path,
):
    """One bad task file stops only a landing whose answer needs the graph."""
    _write_callers(tmp_path, "sonarr", "radarr", "- [unclosed\n")
    _landing_in_a_copy(
        tmp_path, {"sonarr"}
    )  # For its copy of the code; its own run refuses.
    code = (
        "from deploy_tools.land_lib import land_shared\n"
        "print(land_shared.shared_caller_tags(['docs/x.md'], {'sonarr'}))\n"
        "print(sorted(land_shared.narrowed_roles(['docs/x.md'], {'sonarr'})))\n"
    )
    done = run([sys.executable, "-c", code], cwd=tmp_path / "scripts")
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["{}", "[]"]


def test_the_landing_dies_naming_the_task_file_it_refuses():
    def die(message, _rc):
        raise SystemExit(message)

    def refuse():
        raise CannotNarrow("radarr/tasks/helper.yml does not load")

    ln: Any = SimpleNamespace(die=die)
    with pytest.raises(SystemExit, match=r"radarr/tasks/helper\.yml"):
        classify._classified(ln, "expansion", refuse)


def test_the_tags_expansion_hands_the_typed_name_to_the_tag_validation(
    tmp_path, monkeypatch
):
    repo = make_snapshot_repo(tmp_path / "repo")
    _write_callers(repo, "alpha", "beta", "- [unclosed\n")
    _, calls = run_front_half(monkeypatch, repo, ["--tags", "helper", "--check"])
    assert [call[1] for call in calls if call[0] == "validate"] == [("helper",)]


def test_the_fanout_brief_line_names_the_task_file_it_cannot_read(tmp_path):
    """`fanout place` renders this after claiming a batch, so a raise strands the claims."""
    _write_callers(tmp_path, "sonarr", "radarr", "- [unclosed\n")
    _landing_in_a_copy(
        tmp_path, {"sonarr"}
    )  # For its copy of the code; its own run refuses.
    shutil.copytree(INVENTORY, tmp_path / "ansible" / "inventory")
    code = (
        "from dev.findings_lib.precomputed import deploy_line\n"
        f"print(deploy_line([{HELPER_TEMPLATE!r}]))\n"
    )
    done = run([sys.executable, "-c", code], cwd=tmp_path / "scripts")
    assert done.returncode == 0, done.stderr
    assert "radarr/tasks/helper.yml does not load" in done.stdout
