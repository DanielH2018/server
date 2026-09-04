"""land_lib's dependency direction: phases import the state, never each other's peers or the pipeline.

Run: uv run pytest scripts/deploy_tools/tests/test_land_imports.py
"""

from __future__ import annotations

import ast
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "land_lib"
MODULES = frozenset(
    {
        "outcome",
        "options",
        "tools",
        "ledger",
        "landing",
        "merge",
        "classify",
        "ci",
        "tick",
        "deploy",
        "health_verdict",
        "pipeline",
    }
)
ALLOWED = {
    "outcome": set(),
    "options": set(),
    "ledger": set(),
    "tools": set(),
    "landing": {"outcome", "options", "tools", "ledger"},
    "merge": {"landing", "outcome"},
    "classify": {"landing", "outcome"},
    "ci": {"landing", "outcome"},
    "tick": {"landing", "outcome"},
    "deploy": {"landing", "outcome", "ci", "tick"},
    "health_verdict": {"landing", "outcome"},
    "pipeline": {
        "landing",
        "outcome",
        "merge",
        "classify",
        "ci",
        "tick",
        "deploy",
        "health_verdict",
    },
}


def _land_lib_imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module == "deploy_tools.land_lib":
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "deploy_tools.land_lib."
        ):
            found.add(node.module.rsplit(".", 1)[1])
    return found


def test_every_module_exists():
    present = {p.stem for p in LIB.glob("*.py")}
    assert MODULES <= present, MODULES - present


def test_no_module_imports_outside_its_allowed_set():
    for name in sorted(MODULES):
        got = _land_lib_imports(LIB / f"{name}.py")
        assert got <= ALLOWED[name], f"{name} imports {sorted(got - ALLOWED[name])}"


def test_the_guard_would_catch_a_phase_importing_the_pipeline(tmp_path):
    """The reject half: the reader must see both import forms."""
    p = tmp_path / "x.py"
    p.write_text(
        "from deploy_tools.land_lib import pipeline\nfrom deploy_tools.land_lib.deploy import deploy_phase\n"
    )
    assert _land_lib_imports(p) == {"pipeline", "deploy"}


def test_no_init_py():
    """A namespace package, like probe_lib; CLAUDE.md forbids the file."""
    assert not (LIB / "__init__.py").exists()
