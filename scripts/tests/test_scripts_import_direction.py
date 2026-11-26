#!/usr/bin/env python3
"""Dependency direction across ``scripts/``: the import contracts, and no new import cycle.

Several modules under ``scripts/`` were split out of a facade that also runs as ``__main__``. A
leaf that imports its facade back gets a SECOND copy of that module under a second name, with its
own module-level constants, or an ``ImportError`` partway through the cycle when the import is at
module level.

THE CONTRACTS. ``[tool.importlinter]`` in ``pyproject.toml`` holds the direction rules and the
one package-API rule. The tests below run each contract against the grimp graph of every
``scripts/`` package, and plant one edge per contract to prove it can break. A contract naming a
module that no longer exists raises rather than passing. grimp reads only the dotted spelling; the
``scripts-import-their-siblings-dotted`` census row refuses the bare one, deferred or not.

THE CYCLE CHECK. ``_module_graph`` walks every non-test ``scripts/**/*.py`` with ``ast``, in both
spellings, and a depth-first walk may meet no back-edge beyond the four in ``ALLOWED_CYCLES``.
Back-edges, not cycles -- ``_cycles``'s own docstring says what that allowlist does and does not
bound.

Run: uv run pytest scripts/tests/test_scripts_import_direction.py
"""

import ast
import copy
import tomllib
from pathlib import Path

import grimp
import importlinter.configuration
import pytest
from importlinter.contracts.forbidden import ForbiddenContract
from importlinter.contracts.layers import LayersContract
from importlinter.contracts.protected import ProtectedContract

from lib.repo_paths import REPO

SCRIPTS = REPO / "scripts"
IMPORTLINTER = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"][
    "importlinter"
]
CONTRACTS = {contract["name"]: contract for contract in IMPORTLINTER["contracts"]}
CONTRACT_TYPES = {
    "forbidden": ForbiddenContract,
    "layers": LayersContract,
    "protected": ProtectedContract,
}

# One edge per contract that breaks it, the shape a late import inside the importer would add.
PLANTED = {
    "secret_rotation's leaves never import it": (
        "secrets_mgmt.rotation_tools",
        "secrets_mgmt.secret_rotation",
    ),
    "script_classify never imports the scripts page generator": (
        "lib.script_classify",
        "docs.reference.scripts",
    ),
    "the render rule modules never import their render family": (
        "validate.validate_lib.shell_lint",
        "validate.render_lib.shell",
    ),
    "catalog_lib never imports service_catalog, and catalog_model imports no sibling": (
        "docs.catalog_lib.catalog_model",
        "docs.catalog_lib.catalog_facts",
    ),
    "the fragment readers and renderers never import gen_doc_fragments": (
        "docs.fragment_readers",
        "docs.gen_doc_fragments",
    ),
    "findings_lib never imports findings": ("dev.findings_lib.verify", "dev.findings"),
    "probe_lib's split-out leaves never import the module they left": (
        "diagnostics.probe_lib.longhorn_budget",
        "diagnostics.probe_lib.longhorn",
    ),
    "outside code imports dev.fanout_lib.review only through its api": (
        "dev.fanout_lib.place",
        "dev.fanout_lib.review.base_check",
    ),
}

# Each entry is one back-edge of a pair that reach each other by a DEFERRED import inside a
# function, not a complete cycle census. Listed so a fifth cannot appear unnoticed; shrinking the
# set is a fix.
ALLOWED_CYCLES = frozenset(
    {
        # longhorn.py states it: b2_ledger imports this module back by module object, so no helper
        # module may import it.
        frozenset(
            {"diagnostics.probe_lib.b2_ledger", "diagnostics.probe_lib.longhorn"}
        ),
        # `probe ha state` renders the derived state model, and the model's `refresh` reads live
        # HA through probe. Both imports are deferred into the function that needs them. Why it
        # stays a cycle rather than a shared module: `# DECIDED:` at probe_lib/ha.py:581 and at
        # ha_state_model.py's `cmd_refresh` (the deferred `from diagnostics.probe_lib import
        # core`/`ha`).
        frozenset({"diagnostics.probe_lib.ha", "home_assistant.ha_state_model"}),
        # ha_state_checks builds on the model at module level; the model reaches back into
        # `check_errors` from inside one function. Why: `# DECIDED:` at ha_state_checks.py's
        # `from ha_state_model import (...)` and at ha_state_model.py's `main()` (the deferred
        # `from ha_state_checks import check_errors`).
        frozenset({"home_assistant.ha_state_checks", "home_assistant.ha_state_model"}),
        # `soak.py` renders the soak kind with `bundle.py`'s writers at module level, and
        # `bundle_files` imports `soak_files` inside the function, because `SHARED_KINDS`
        # makes every bundle render `soak`. Why: `# DECIDED: deferred, a documented cycle` in
        # bundle.py's `bundle_files`.
        frozenset({"renderer.renderer_lib.bundle", "renderer.renderer_lib.soak"}),
    }
)

# Named members, so a stopped walk names the module that went missing; a count alone moves with the
# tree.
ANCHOR_MODULES = frozenset(
    {
        "dev.findings",
        "diagnostics.probe",
        "diagnostics.probe_lib.longhorn",
        "docs.service_catalog",
        "lib.repo_paths",
        "secrets_mgmt.secret_rotation",
        "validate.render_templates",
    }
)


def _module_names() -> dict[str, Path]:
    """{dotted name relative to scripts/: file}, for every non-test module under scripts/."""
    return {
        ".".join(p.relative_to(SCRIPTS).with_suffix("").parts): p
        for p in SCRIPTS.rglob("*.py")
        if "tests" not in p.parts
    }


def _resolve(name: str, importer: str, known: set[str]) -> str | None:
    """The module `name` refers to, from inside `importer`, or None if it is not first-party.

    Two spellings resolve. `diagnostics.probe_lib.core` is the dotted path from `scripts/`,
    which is how a module reached through the `scripts` pythonpath entry names its target.
    `core` alone is the bare-sibling form, which resolves because a directly-invoked script gets
    its OWN directory on sys.path — `postflight.py` reaches `probe` that way.
    """
    if name in known:
        return name
    sibling = ".".join(importer.split(".")[:-1] + [name])
    return sibling if sibling in known else None


def _first_party_imports(source: str, importer: str, known: set[str]) -> set[str]:
    """Every module in `known` that `source` imports, by any form and at any nesting depth.

    `ast.walk` rather than a scan of `tree.body`: an import deferred into a function body is the
    exact shape this guard exists to catch, and it is invisible to a top-level-only walk.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # An alias binds a second name for the same module; the edge is identical.
                if hit := _resolve(alias.name, importer, known):
                    found.add(hit)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if hit := _resolve(node.module, importer, known):
                found.add(hit)
                continue
            # `from diagnostics.probe_lib import core`: the module is the PACKAGE and the imported
            # name is the module; without this arm such an import reads as no edge.
            for alias in node.names:
                if hit := _resolve(f"{node.module}.{alias.name}", importer, known):
                    found.add(hit)
    return found - {importer}


def _module_graph() -> dict[str, set[str]]:
    files = _module_names()
    known = set(files)
    return {
        name: _first_party_imports(path.read_text(), name, known)
        for name, path in files.items()
    }


def _cycles(graph: dict[str, set[str]]) -> set[frozenset[str]]:
    """One entry per BACK-EDGE a depth-first walk of `graph` meets — NOT every cycle in it.

    ``ALLOWED_CYCLES`` is an allowlist of what this returns, so the distinction decides how to
    read it. The walk colours each node white/grey/black; an edge onto a grey node closes a
    loop, and the entry recorded is the grey stack from that node onward.

    Every entry is a real cycle: the stack slice is a path in the walk's tree, and the
    back-edge closes it. The converse fails. Several cycles through one back-edge collapse to a
    single entry, and which of them surfaces is decided by the order the walk reached the
    nodes, so ``len(_cycles(g))`` is a count of back-edges rather than of cycles.

    Detection is still sound, which is what the rule needs: any cycle puts at least one
    back-edge in front of a DFS, so an empty result does mean an acyclic graph. And the walk is
    deterministic — ``sorted`` at both loops — so one tree always yields the same entries.

    A frozenset of members rather than an ordered path: the same loop has as many spellings as
    it has members, and the allowlist must match it whichever node the walk entered from.
    """
    colour: dict[str, int] = {}
    found: set[frozenset[str]] = set()

    def visit(node: str, stack: list[str]) -> None:
        colour[node] = 1
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if colour.get(nxt, 0) == 0:
                visit(nxt, stack)
            elif colour[nxt] == 1:
                found.add(frozenset(stack[stack.index(nxt) :]))
        stack.pop()
        colour[node] = 2

    for node in sorted(graph):
        if colour.get(node, 0) == 0:
            visit(node, [])
    return found


# --- The live tree ---------------------------------------------------------------------------


def test_the_census_finds_every_scripts_module():
    """Non-vacuity for the walk itself: an empty census makes every rule below pass."""
    names = set(_module_names())
    assert len(names) >= 140, (
        f"the glob matched {len(names)} modules — SCRIPTS is wrong"
    )
    assert ANCHOR_MODULES <= names, f"missing: {sorted(ANCHOR_MODULES - names)}"


def test_no_import_cycle_beyond_the_documented_ones():
    graph = _module_graph()
    new = sorted(sorted(cycle) for cycle in _cycles(graph) - ALLOWED_CYCLES)
    assert not new, f"new import cycles under scripts/: {new}"


def test_every_allowed_cycle_still_names_modules_that_exist():
    """Keeps ALLOWED_CYCLES from silently accumulating entries for deleted modules."""
    names = set(_module_names())
    for cycle in ALLOWED_CYCLES:
        assert cycle <= names, (
            f"ALLOWED_CYCLES names missing modules: {sorted(cycle - names)}"
        )


# --- Proof the guard can go red ---------------------------------------------------------------

_KNOWN = {
    "secrets_mgmt.secret_rotation",
    "secrets_mgmt.rotation_tools",
    "lib.repo_paths",
}


def test_a_facade_import_deferred_into_a_function_is_seen():
    """The reject half: a LATE import inside a leaf."""
    source = (
        "def audit(reg):\n"
        "    from secrets_mgmt.secret_rotation import TIER_DAYS\n"
        "    return TIER_DAYS\n"
    )
    assert _first_party_imports(source, "secrets_mgmt.rotation_tools", _KNOWN) == {
        "secrets_mgmt.secret_rotation"
    }


def test_every_import_form_of_the_facade_is_seen():
    """`import x`, `import x as y`, `from x import y`, `from pkg import x` all weigh the same."""
    for source in (
        "import secrets_mgmt.secret_rotation\n",
        "import secrets_mgmt.secret_rotation as sr\n",
        "from secrets_mgmt.secret_rotation import TIER_DAYS\n",
        "from secrets_mgmt import secret_rotation\n",
        "import secret_rotation\n",  # the bare-sibling spelling
    ):
        assert _first_party_imports(source, "secrets_mgmt.rotation_tools", _KNOWN) == {
            "secrets_mgmt.secret_rotation"
        }, source


def test_the_accept_half_is_not_flagged():
    """A leaf importing DOWNWARD, and stdlib, are both clean — the rule is about direction."""
    source = (
        "import datetime as dt\n"
        "import yaml\n"
        "from lib.repo_paths import REPO\n"
        "from secret_rotation_notes import x\n"  # a near-miss name that is not a module
    )
    assert _first_party_imports(source, "secrets_mgmt.rotation_tools", _KNOWN) == {
        "lib.repo_paths"
    }


def test_a_module_importing_itself_is_not_a_cycle():
    """`_first_party_imports` drops the self-edge, so re-exporting a name is not a finding."""
    assert (
        _first_party_imports(
            "import rotation_tools\n", "secrets_mgmt.rotation_tools", _KNOWN
        )
        == set()
    )


def test_the_cycle_detector_finds_a_two_module_cycle():
    assert _cycles({"a": {"b"}, "b": {"a"}, "c": {"a"}}) == {frozenset({"a", "b"})}


def test_the_cycle_detector_clears_a_diamond():
    """The accept half: a shared leaf reached by two paths is not a cycle."""
    assert _cycles({"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set()}) == set()


# --- The import contracts ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def graph() -> grimp.ImportGraph:
    importlinter.configuration.configure()  # a broken forbidden contract reads its timer
    return grimp.build_graph(*IMPORTLINTER["root_packages"], cache_dir=None)


def _kept(name: str, graph: grimp.ImportGraph) -> bool:
    """Whether contract `name` holds over a copy of `graph`; a protected check edits its graph."""
    options = {k: v for k, v in CONTRACTS[name].items() if k not in ("name", "type")}
    contract = CONTRACT_TYPES[CONTRACTS[name]["type"]](name, IMPORTLINTER, options)
    return contract.check(copy.deepcopy(graph), verbose=False).kept


def test_the_contracts_read_every_scripts_package():
    """A package left out of the graph hides its imports from every contract."""
    packages = {
        p.name
        for p in SCRIPTS.iterdir()
        if p.is_dir() and p.name not in ("tests", "__pycache__")
    }
    assert set(IMPORTLINTER["root_packages"]) == packages


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_the_live_tree_keeps_the_contract(name, graph):
    assert _kept(name, graph)


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_a_planted_import_breaks_the_contract(name, graph):
    importer, imported = PLANTED[name]
    planted = copy.deepcopy(graph)
    planted.add_import(importer=importer, imported=imported)
    assert not _kept(name, planted)
