"""A module its own package imports is a library, however the package spells the import.

Three shapes read as "no automated caller in the tree" on `docs/reference/scripts.md` for
modules that cannot be run at all, because none has an `if __name__ == "__main__"` guard:

- a relative import, `from .citations import Citation` in `scripts/lib/facts/atoms.py`;
- an absolute import naming the importer's OWN package directory,
  `from fanout_lib.manifest import Batch` inside `scripts/dev/fanout_lib`, which resolves
  against `scripts/dev` and not against `scripts/`;
- a module whose only importers are its tests, `grafana_panel_report.py`.

A fourth shape: a role's tasks naming a guardless module to ship it into an
image, `obs_api.py`, which read as a deploy-time gate.

The synthetic cases below are the red proof for each shape. `test_the_real_tree_*` is the
non-vacuity half: the census reads the tree, so a directory move or a rename can empty it.

Run: uv run pytest scripts/lib/tests/test_script_classify_package_imports.py
"""

from lib import script_classify as sc
from lib.repo_paths import REPO, SCRIPTS

# The four shapes, one live module each. A rename here is fine; an empty census is not.
MUST_BE_LIBRARIES = frozenset(
    {"citations.py", "manifest.py", "grafana_panel_report.py", "obs_api.py"}
)


def _package(tmp_path, sibling_body: str, *, nested: bool = True):
    """A tree whose only script is a two-module package under `scripts/dev/` (or `scripts/`)."""
    scripts = tmp_path / "scripts"
    pkg = scripts / "dev" / "pkg" if nested else scripts / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "leaf.py").write_text('"""Summary."""\nVALUE = 1\n')
    (pkg / "sibling.py").write_text(sibling_body)
    return tmp_path, scripts


def test_a_relative_import_makes_the_sibling_a_library(tmp_path):
    repo, scripts = _package(tmp_path, '"""Summary."""\nfrom .leaf import VALUE\n')
    verdict, evidence = sc.classify(repo, scripts)["leaf.py"]
    assert verdict == "library"
    assert "sibling.py" in evidence


def test_a_relative_import_of_the_module_itself_makes_it_a_library(tmp_path):
    """`from . import leaf` names the module in the alias, not in the module part."""
    repo, scripts = _package(tmp_path, '"""Summary."""\nfrom . import leaf\n')
    assert sc.classify(repo, scripts)["leaf.py"][0] == "library"


def test_an_absolute_import_naming_the_own_package_directory_is_seen(tmp_path):
    """The `fanout_lib` shape: the head names the importer's own directory, under `scripts/dev`."""
    repo, scripts = _package(tmp_path, '"""Summary."""\nfrom pkg.leaf import VALUE\n')
    verdict, evidence = sc.classify(repo, scripts)["leaf.py"]
    assert verdict == "library"
    assert "sibling.py" in evidence


def test_an_import_of_a_module_that_is_not_there_is_not_an_edge(tmp_path):
    """The reject half: `pkg` exists, `pkg/absent.py` does not, so nothing is imported."""
    repo, scripts = _package(tmp_path, '"""Summary."""\nfrom pkg.absent import VALUE\n')
    assert sc.classify(repo, scripts)["leaf.py"][0] == "adhoc"


def test_a_guardless_module_only_a_test_imports_is_a_library(tmp_path):
    """A test importer does not make a library, but a module with no way to run is one.

    Its complement is the test below. The pair moved here out of
    `scripts/docs/tests/test_gen_reference_scripts.py`, which held the first half alone and is
    at its length cap.
    """
    repo, scripts = _package(tmp_path, '"""Summary."""\nVALUE = 2\n')
    tests = scripts / "dev" / "pkg" / "tests"
    tests.mkdir()
    (tests / "test_leaf.py").write_text("from leaf import VALUE\n")
    verdict, evidence = sc.classify(repo, scripts)["leaf.py"]
    assert verdict == "library"
    assert "test_leaf.py" in evidence


def test_a_test_importing_an_entry_point_does_not_make_it_a_library(tmp_path):
    """The reject half: a `__main__` guard means a person can run it, so its tests do not decide.

    Otherwise every tested entry point in the tree would read as a module nobody runs.
    """
    repo, scripts = _package(tmp_path, '"""Summary."""\nVALUE = 2\n')
    (scripts / "dev" / "pkg" / "leaf.py").write_text(
        '"""Summary."""\nVALUE = 1\nif __name__ == "__main__":\n    print(VALUE)\n'
    )
    tests = scripts / "dev" / "pkg" / "tests"
    tests.mkdir()
    (tests / "test_leaf.py").write_text("from leaf import VALUE\n")
    assert sc.classify(repo, scripts)["leaf.py"][0] == "adhoc"


def _shipped_by_a_role(repo, leaf_body: str):
    """Write `leaf.py` with `leaf_body`, then name it in a role's tasks, as an image ship list does."""
    (repo / "scripts" / "dev" / "pkg" / "leaf.py").write_text(leaf_body)
    tasks = repo / "ansible" / "roles" / "k8s" / "svc" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        '- vars:\n    files:\n      "../scripts/dev/pkg/leaf.py": leaf.py\n'
    )


def test_a_guardless_module_a_role_ships_is_a_library(tmp_path):
    repo, scripts = _package(tmp_path, '"""Summary."""\n')
    _shipped_by_a_role(repo, '"""Summary."""\nVALUE = 1\n')
    verdict, evidence = sc.classify(repo, scripts)["leaf.py"]
    assert verdict == "library"
    assert "carried by deploy: ansible/roles/k8s/svc/tasks/main.yml" in evidence


def test_a_guarded_script_a_role_names_is_a_deploy_gate(tmp_path):
    """The reject half: a script with a `__main__` guard named by a role's tasks runs there."""
    repo, scripts = _package(tmp_path, '"""Summary."""\n')
    _shipped_by_a_role(
        repo, '"""Summary."""\nif __name__ == "__main__":\n    print(1)\n'
    )
    assert sc.classify(repo, scripts)["leaf.py"][0] == "gate"


def test_the_real_tree_still_classifies_each_shape_as_a_library():
    verdicts = sc.classify(REPO, SCRIPTS)
    wrong = {
        name: verdicts[name]
        for name in MUST_BE_LIBRARIES
        if verdicts[name][0] != "library"
    }
    assert not wrong, f"the census lost these to a non-library verdict: {wrong}"


def test_no_python_module_without_a_main_guard_reads_as_something_a_person_runs():
    """The invariant the three shapes were each breaking, asked of the whole tree."""
    paths = sc.by_name(SCRIPTS)
    verdicts = sc.classify(REPO, SCRIPTS)
    guardless = [
        name
        for name, path in paths.items()
        if name.endswith(".py") and not sc._has_main_guard(sc.file_text(path))
    ]
    assert len(guardless) > 100, (
        f"only {len(guardless)} guardless modules — the scan is empty"
    )
    runnable = {n: verdicts[n] for n in guardless if verdicts[n][0] != "library"}
    assert not runnable, (
        f"these cannot be run and are not catalogued as libraries: {runnable}"
    )
