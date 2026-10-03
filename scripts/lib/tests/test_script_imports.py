"""An import credits a script only when the directory it resolves from lies under `scripts/`.

Matching on the basename alone credited `scripts/lib/gitops_markers.py` (deleted in #3275)
with `gitops_state.py`'s import of the gitops_deploy role's `files/gitops_markers.py`, and
`scripts/docs/reference/secrets.py` with the stdlib `secrets`. The synthetic pairs are
the red proof; `test_the_real_tree_*` is the Verify-by from that issue.

Run: uv run pytest scripts/lib/tests/test_script_imports.py
"""

from lib.repo_paths import SCRIPTS
from lib.script_imports import import_graph


def _importers_of_leafy(tmp_path, body: str) -> set[str]:
    """Who imports `scripts/docs/leafy.py`, when `scripts/diagnostics/user.py` holds `body`."""
    scripts = tmp_path / "scripts"
    (scripts / "docs").mkdir(parents=True)
    (scripts / "diagnostics").mkdir()
    (scripts / "docs" / "leafy.py").write_text("VALUE = 1\n")
    (scripts / "diagnostics" / "user.py").write_text(
        f"import sys\nfrom pathlib import Path\n{body}\n"
    )
    return import_graph(scripts, lambda path: True).get("leafy", set())


def test_a_bare_import_through_the_files_own_path_insert_is_credited(tmp_path):
    """The bare-import shape: a file inserts `scripts/docs`, then imports `route_facts`."""
    body = (
        'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docs"))\n'
        "from leafy import VALUE"
    )
    assert _importers_of_leafy(tmp_path, body) == {"user.py"}


def test_a_bare_import_no_insert_explains_is_not_credited(tmp_path):
    """The `gitops_state.py` shape: the only insert is a name pointing outside `scripts/`."""
    body = "sys.path.insert(0, str(GITOPS_DEPLOY_FILES))\nfrom leafy import VALUE"
    assert _importers_of_leafy(tmp_path, body) == set()


def test_an_alias_of_a_package_outside_scripts_is_not_credited(tmp_path):
    """`from ansible.plugins.filter import core` names no script, whatever shares its name."""
    assert _importers_of_leafy(tmp_path, "from ansible.plugins import leafy") == set()


def test_an_alias_of_a_package_under_scripts_is_credited(tmp_path):
    """The `from fanout_lib import launch` shape: the package resolves from `scripts/`."""
    assert _importers_of_leafy(tmp_path, "from docs import leafy") == {"user.py"}


def test_the_real_tree_credits_the_insert_edge_and_not_the_basename_ones():
    imported = import_graph(SCRIPTS, lambda path: not path.name.startswith("test_"))
    assert "networking.py" in imported["route_facts"]
    assert "secret_rotation.py" not in imported.get("secrets", set()), (
        "`import secrets as pysecrets` is the stdlib, not scripts/docs/reference/secrets.py"
    )
