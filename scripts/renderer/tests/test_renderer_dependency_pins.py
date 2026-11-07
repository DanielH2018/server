"""The render-diff job's dependency-pin hazard, and the census that holds its literal.

A renderer dependency bump that changes output would otherwise only print a diff, and nothing
compares the render with Ansible's after R4.7. `RENDERER_DEPENDENCIES` is where the job's read
of `uv.lock` starts, so a package the renderer imports and the literal omits has no hazard.

Run: uv run pytest scripts/renderer/tests/test_renderer_dependency_pins.py
"""

import ast
import sys
from pathlib import Path

from lib.repo_paths import REPO
from validate.render_diff_job import (
    RENDERER_DEPENDENCIES,
    dependency_closure,
    judge,
    pin_hazards,
)

SCRIPTS = REPO / "scripts"
FILTER_PLUGINS = REPO / "ansible" / "filter_plugins"
# An import's top-level module, where its distribution's name differs.
DISTRIBUTIONS = {"yaml": "pyyaml"}


def _lock(jinja: str = "3.1.6", tool: str = "1.0") -> str:
    return f"""
[[package]]
name = "ansible-core"
version = "2.22.0"

[[package]]
name = "jinja2"
version = "{jinja}"
dependencies = [{{ name = "markupsafe" }}]

[[package]]
name = "markupsafe"
version = "3.0.0"

[[package]]
name = "pyyaml"
version = "6.0"

[[package]]
name = "pytest-tool"
version = "{tool}"
"""


def _first_party(module: str, roots: tuple[Path, ...]) -> Path | None:
    parts = module.split(".")
    for root in roots:
        for candidate in (
            root.joinpath(*parts).with_suffix(".py"),
            root.joinpath(*parts),
        ):
            if candidate.exists():
                return candidate
    return None


def _third_party_imports(entries: list[Path], roots: tuple[Path, ...]) -> set[str]:
    """The top-level third-party modules `entries` import, following first-party imports."""
    found: set[str] = set()
    seen: set[Path] = set()
    pending = list(entries)
    while pending:
        path = pending.pop()
        if path in seen or path.is_dir():
            continue
        seen.add(path)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
                names += [f"{node.module}.{alias.name}" for alias in node.names]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                local = _first_party(name, roots)
                if local is not None:
                    pending.append(local)
                elif top not in sys.stdlib_module_names and not _first_party(
                    top, roots
                ):
                    found.add(top)
    return found


def _census_problems(entries: list[Path], roots: tuple[Path, ...]) -> set[str]:
    imported = _third_party_imports(entries, roots)
    return {
        module
        for module in imported
        if DISTRIBUTIONS.get(module, module) not in RENDERER_DEPENDENCIES
    }


def test_a_jinja2_bump_with_no_line_fails_the_job(tmp_path):
    pins = pin_hazards(_lock(), _lock(jinja="3.1.7"))
    assert pins == ["uv.lock or pyproject.toml entry for jinja2"]
    (tmp_path / "flux").mkdir()
    outcome = judge(
        tmp_path, tmp_path, [("M", "uv.lock")], "no line", floor=0, pins=pins
    )
    assert any("jinja2" in problem for problem in outcome.problems)


def test_an_unrelated_dependency_bump_passes_the_job():
    assert pin_hazards(_lock(), _lock(tool="2.0")) == []


def test_a_bump_of_a_dependency_of_a_dependency_counts():
    moved = _lock().replace('version = "3.0.0"', 'version = "3.0.1"')
    assert pin_hazards(_lock(), moved) == [
        "uv.lock or pyproject.toml entry for markupsafe"
    ]


def test_a_requirement_edit_with_the_lock_unmoved_counts():
    assert pin_hazards(
        _lock(),
        _lock(),
        '[dependency-groups]\ndev = ["jinja2>=3"]\n',
        '[dependency-groups]\ndev = ["jinja2>=4"]\n',
    ) == ["uv.lock or pyproject.toml entry for jinja2"]


def test_the_closure_reaches_jinja2s_dependency_in_the_real_lock():
    closure = dependency_closure((REPO / "uv.lock").read_text())
    assert {"jinja2", "markupsafe", "pyyaml"} <= closure
    assert "ansible-core" not in closure


def test_every_third_party_module_the_renderer_imports_is_in_the_literal():
    entries = [
        SCRIPTS / "renderer" / "homelab_render.py",
        *sorted((SCRIPTS / "renderer" / "renderer_lib").glob("*.py")),
    ]
    roots = (
        SCRIPTS,
        *sorted(p for p in SCRIPTS.iterdir() if p.is_dir()),
        FILTER_PLUGINS,
        REPO / "hosts" / "lib",
    )
    imported = _third_party_imports(entries, roots)
    assert {"jinja2", "yaml"} <= imported
    assert "ansible" not in imported
    assert _census_problems(entries, roots) == set()


def test_a_module_importing_a_package_outside_the_literal_fails_the_census(tmp_path):
    module = tmp_path / "fixture_renderer.py"
    module.write_text("import requests\nimport jinja2\nimport json\n")
    assert _census_problems([module], (tmp_path,)) == {"requests"}
