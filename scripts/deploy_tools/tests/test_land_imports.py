"""land_lib's dependency direction: phases import the state, never each other's peers or the pipeline.

Run: uv run pytest scripts/deploy_tools/tests/test_land_imports.py
"""

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
        "policy",
        "classify",
        "ci",
        "tick",
        "deploy",
        "health_verdict",
        "pipeline",
        "detach",
        "handoff",
        "pr_json",
        "land_platform",
        "land_probe",
        "land_reach",
        "land_rerolls",
        "land_shared",
        "land_tags",
    }
)
ALLOWED = {
    "outcome": set(),
    "options": set(),
    # `cause`'s vocabulary lives beside `verdict`'s, and the Ledger validates against it.
    "ledger": {"outcome"},
    # The classifier libraries the Tools defaults wrap (#4349 moved them in from beside land.py).
    "tools": {
        "land_platform",
        "land_reach",
        "land_rerolls",
        "land_shared",
        "land_tags",
    },
    # The PR JSON types are a leaf: `Landing.view` returns them and the policy reads them.
    "pr_json": set(),
    "landing": {"outcome", "options", "tools", "ledger", "pr_json", "land_tags"},
    # The landing policy's checks run inside --arm-merge, before any merge call.
    "merge": {"landing", "outcome", "policy"},
    "policy": {"landing", "outcome", "pr_json"},
    "classify": {"landing", "outcome", "land_tags"},
    "ci": {"landing", "outcome"},
    "tick": {"landing", "outcome"},
    "deploy": {"landing", "outcome", "ci", "tick", "land_platform"},
    # `tick.rearm_tick`: the second kick request, after the gate, for a first one that joined
    # a run in flight. The kick's states live in tick.py, in one place.
    "health_verdict": {"landing", "outcome", "tick"},
    # The fork, the logfile and the verdict wait. It imports no phase: `land.py` hands it a
    # callable and it never knows what a landing is.
    "detach": set(),
    # The handoff to a lander unit reads the Options it refuses flags from, and nothing else.
    "handoff": {"options"},
    # The tag derivation and what it reads. None of them knows what a landing is.
    "land_tags": {"land_reach", "land_shared"},
    "land_platform": {"land_tags"},
    "land_reach": set(),
    "land_rerolls": set(),
    "land_shared": set(),
    # cc-wait's `land` source runs under `uv run --no-project`, so it reads only the
    # stdlib-only fork record and the verdict words.
    "land_probe": {"detach", "outcome"},
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
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("deploy_tools.land_lib.")
        ):
            found.add(node.module.rsplit(".", 1)[1])
    return found


def _present() -> set[str]:
    return {p.stem for p in LIB.glob("*.py")}


def test_the_module_set_is_exactly_what_is_on_disk():
    """`==`, not `<=`: a module added without being checked must fail."""
    assert MODULES == _present(), (
        f"missing from MODULES: {sorted(_present() - MODULES)}; "
        f"listed but absent: {sorted(MODULES - _present())}"
    )


def test_no_module_imports_outside_its_allowed_set():
    """Iterates what is ON DISK, so a new module with no ALLOWED entry is a KeyError."""
    present = _present()
    assert present, "the glob matched nothing -- LIB is wrong, not the tree"
    for name in sorted(present):
        got = _land_lib_imports(LIB / f"{name}.py")
        assert name in ALLOWED, f"{name} has no ALLOWED entry"
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
