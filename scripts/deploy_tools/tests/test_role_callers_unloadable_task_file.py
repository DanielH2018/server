"""A task file `role_callers` cannot load makes each reader refuse or widen (#7298).

The issue leaves open whether `role_callers` raises or its readers widen, so each test drives a
reader and accepts either. `sonarr` includes `helper` from a clean file. `radarr`'s only include
sits in a file that does not load, and a reader that names `sonarr` alone has named too few.

Run: uv run pytest scripts/deploy_tools/tests/test_role_callers_unloadable_task_file.py
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from _deploy_sh_fakes import scrub_process_git_env
from _narrow_fixtures import DECLARED, Tree, _refs, build_tree
from deploy_tools.land_lib import classify
from deploy_tools.narrow_lib import broad as narrow_broad
from lib import k8s_role_callers
from lib.narrow_git import CannotNarrow

INCLUDE = "- ansible.builtin.include_role:\n    name: k8s/helper\n"
HELPER_TEMPLATE = "ansible/roles/k8s/helper/templates/thing.yaml.j2"


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


def test_the_landing_dies_naming_the_task_file_it_refuses():
    def die(message, _rc):
        raise SystemExit(message)

    def refuse():
        raise CannotNarrow("radarr/tasks/helper.yml does not load")

    ln: Any = SimpleNamespace(die=die)
    with pytest.raises(SystemExit, match=r"radarr/tasks/helper\.yml"):
        classify._classified(ln, "expansion", refuse)
