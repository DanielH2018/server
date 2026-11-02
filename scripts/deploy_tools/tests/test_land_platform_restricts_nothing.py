"""#6981: `land_platform` holds no path-tree check, or is gone.

No tag is declared on two platforms, so the tree a path sits in proves nothing about which
host a tag reaches. Either fix in the issue passes these: collapsing `k8s_only_tags` to every
derived tag, or deleting the module along with the `k8s_only` restriction. Neither test imports
`land_platform` at module level, because the second fix removes it.

Run: uv run pytest scripts/deploy_tools/tests/test_land_platform_restricts_nothing.py
"""

import ast
import importlib

import reach
from lib.git import git_stdout
from lib.repo_paths import REPO

_MODULE = "scripts/deploy_tools/land_lib/land_platform.py"
_DECLARED = {"wg-easy", "sonarr"}
# A k8s path and a path outside `roles/k8s/` that `tag_for` still maps to the same tag.
_FILES = [
    "ansible/roles/k8s/wg-easy/templates/deployment.yaml.j2",
    "ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2",
    "ansible/roles/k8s/sonarr/tasks/main.yml",
]


def _tracked_module() -> str | None:
    tracked = git_stdout("ls-files", "--", _MODULE, cwd=REPO).split()
    return tracked[0] if tracked else None


def _tree_checks(source: str) -> list[str]:
    """Each `startswith` call and each `ansible/roles/` literal in `source`."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "startswith":
            found.append(f"line {node.lineno}: .startswith")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("ansible/roles/")
        ):
            found.append(f"line {node.lineno}: {node.value!r}")
    return found


def test_land_platform_is_gone_or_holds_no_path_tree_check():
    path = _tracked_module()
    if path is None:
        return
    source = (REPO / path).read_text(encoding="utf-8")
    assert _tree_checks(source) == [], f"{path} still tests a path's tree"


def test_k8s_only_tags_is_every_tag_the_paths_derive():
    """A tag a non-k8s path names is no longer dropped from the restriction."""
    if _tracked_module() is None:
        return
    module = importlib.import_module("deploy_tools.land_lib.land_platform")
    name = "k8s_only_tags"
    k8s_only_tags = getattr(module, name)
    derived = sorted({t for p in _FILES if (t := reach.tag_for(p, _DECLARED))})
    assert derived, "the fixture paths derive no tag, so this proves nothing"
    assert k8s_only_tags(_FILES, _DECLARED) == derived
