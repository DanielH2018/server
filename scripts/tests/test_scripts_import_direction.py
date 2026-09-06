#!/usr/bin/env python3
"""Dependency direction across ``scripts/``: a leaf never imports the facade it was split out of.

Six modules under ``scripts/`` were split out of a larger one during the 2026-09-04 module
split, and each records the same invariant in its own docstring — ``rotation_tools.py`` says it
"names ``secret_rotation`` nowhere, at import time or later", ``lib/script_classify.py`` says
"a leaf never imports the facade it was split out of", ``lib/k8s_pvc.py`` and
``docs/catalog_model.py`` say it again in their own words. A docstring is not a check. The
deployer's own split is guarded by ``ansible/tests/deploy/test_gitops_deploy_imports.py`` and
monitor-bridge's by ``ansible/tests/services/test_bridge_patch_boundary.py``, but nothing walked
``scripts/``, so a late ``import secret_rotation`` inside a leaf function would have landed green.

WHY THE DIRECTION MATTERS, not just tidiness. Every facade here is also an entry point run as
``__main__``. A leaf that imports it back gets a SECOND copy of that module under a second name,
with its own module-level constants — and, when the import is at module level rather than
deferred into a function, an ``ImportError`` partway through the cycle instead.

WHAT IS CHECKED. ``_module_graph`` walks every non-test ``scripts/**/*.py`` with ``ast`` and
records the first-party modules each one imports. ``ast.walk`` descends into function bodies, so
a deferred import weighs exactly as much as a module-level one; both the ``import x`` and the
``from x import y`` forms count, in the dotted-from-``scripts/`` and the bare-sibling spellings
both, because which form a module uses changes nothing about the direction of the edge.

Two rules ride on that graph: the declared facade edges in ``FACADE_EDGES`` may not run
backwards, and a depth-first walk may meet no back-edge beyond the three in
``ALLOWED_CYCLES``. Back-edges, not cycles -- ``_cycles``'s own docstring says what that
allowlist does and does not bound.

Run: uv run pytest scripts/tests/test_scripts_import_direction.py
"""

import ast
from pathlib import Path

from lib.repo_paths import REPO

SCRIPTS = REPO / "scripts"

# Every documented facade -> leaf split under scripts/, as the edge it puts in the graph. This
# is the NON-VACUITY ANCHOR: the direction rule below iterates these pairs, so a rename or a
# move that emptied the census would leave `all()` over nothing and pass. Requiring each edge to
# be PRESENT in the live graph makes that failure name the pair that went missing instead.
FACADE_EDGES = frozenset(
    {
        # scripts/secrets_mgmt/: `secret_rotation.py` is the entry point; the other five are its
        # leaves. rotation_tools.py's docstring is the one that spells the rule out.
        ("secrets_mgmt.secret_rotation", "secrets_mgmt.consumers"),
        ("secrets_mgmt.secret_rotation", "secrets_mgmt.git_dates"),
        ("secrets_mgmt.secret_rotation", "secrets_mgmt.rotation_tools"),
        ("secrets_mgmt.secret_rotation", "secrets_mgmt.secret_registry"),
        ("secrets_mgmt.secret_rotation", "secrets_mgmt.sops_io"),
        # scripts/docs/reference/scripts.py renders the page; the census and the coverage
        # judgement were split into lib/ so `lib.script_coverage` could reach them too.
        ("docs.reference.scripts", "lib.script_classify"),
        ("docs.reference.scripts", "lib.script_coverage"),
        ("lib.script_coverage", "lib.script_classify"),
        # scripts/validate/k8s_manifests.py re-exports every name in lib/k8s_pvc.py.
        ("validate.k8s_manifests", "lib.k8s_pvc"),
        # scripts/docs/service_catalog.py over the four catalogue modules; catalog_model is the
        # one leaf with no first-party dependency beyond lib.repo_paths.
        ("docs.service_catalog", "docs.catalog_backup"),
        ("docs.service_catalog", "docs.catalog_facts"),
        ("docs.service_catalog", "docs.catalog_model"),
        ("docs.service_catalog", "docs.catalog_render"),
        ("docs.catalog_facts", "docs.catalog_model"),
        ("docs.catalog_render", "docs.catalog_model"),
        # scripts/docs/gen_doc_fragments.py over the fragment reader/renderer pair.
        ("docs.gen_doc_fragments", "docs.fragment_readers"),
        ("docs.gen_doc_fragments", "docs.fragment_renderers"),
        # scripts/dev/findings.py over the findings_* leaves.
        ("dev.findings", "dev.findings_gh"),
        ("dev.findings", "dev.findings_model"),
        ("dev.findings", "dev.findings_plans"),
        ("dev.findings", "dev.findings_tools"),
        ("dev.findings", "dev.findings_verify"),
        # probe_lib/longhorn.py over the three modules split out of its 630 lines.
        ("diagnostics.probe_lib.longhorn", "diagnostics.probe_lib.longhorn_blocks"),
        ("diagnostics.probe_lib.longhorn", "diagnostics.probe_lib.longhorn_budget"),
        ("diagnostics.probe_lib.longhorn", "diagnostics.probe_lib.longhorn_cluster"),
        # probe_lib/health.py over the transport- and object-specific readers.
        ("diagnostics.probe_lib.health", "diagnostics.probe_lib.health_cronjob"),
        ("diagnostics.probe_lib.health", "diagnostics.probe_lib.health_docker"),
        ("diagnostics.probe_lib.health", "diagnostics.probe_lib.health_rollout"),
    }
)

# The three back-edges `_cycles` found in the tree on 2026-09-05, each one a pair of modules that
# reach each other by a DEFERRED import inside a function. An entry is one back-edge, NOT a
# complete cycle census -- `_cycles` says why, and the name is kept only because the entries here
# happen to be two-module loops, where the two coincide. They are listed so a FOURTH cannot
# appear unnoticed; nothing here says they are good, and shrinking this set is a fix.
ALLOWED_CYCLES = frozenset(
    {
        # longhorn.py:16 states it: "b2_ledger imports this module back by module object, so no
        # helper module may import it." Both edges are module-level and survive only because
        # each side reaches the other qualified.
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
    }
)

# Named members the census must contain. A count alone moves with the tree; these say which
# module went missing when the walk stops finding things.
ANCHOR_MODULES = frozenset(
    {
        "dev.findings",
        "diagnostics.probe",
        "diagnostics.probe_lib.longhorn",
        "docs.service_catalog",
        "lib.repo_paths",
        "secrets_mgmt.secret_rotation",
        "validate.k8s_manifests",
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
            # `from diagnostics.probe_lib import core` — the module is the PACKAGE and the
            # imported name is the module. Without this arm every such import reads as no edge.
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


def test_every_declared_facade_edge_is_in_the_live_graph():
    """Non-vacuity for the direction rule: a pair whose leaf moved must fail here, loudly.

    Without this, renaming a leaf silently drops it from the graph and the rule below stops
    governing it while still reading green.
    """
    graph = _module_graph()
    missing = sorted(
        f"{facade} -> {leaf}"
        for facade, leaf in FACADE_EDGES
        if leaf not in graph.get(facade, set())
    )
    assert not missing, f"declared facade edges absent from the tree: {missing}"


def test_no_leaf_imports_its_facade():
    """The rule the docstrings state. Both import forms, module-level and deferred alike."""
    graph = _module_graph()
    backwards = sorted(
        f"{leaf} imports {facade}"
        for facade, leaf in FACADE_EDGES
        if facade in graph.get(leaf, set())
    )
    assert not backwards, (
        f"a leaf reaches back into the facade it was split out of: {backwards} — "
        f"when the facade runs as __main__ that is a second copy of it under a second name"
    )


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
    """The reject half, in the exact shape #1194 describes: a LATE import inside a leaf."""
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
