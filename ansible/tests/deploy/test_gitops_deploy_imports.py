#!/usr/bin/env python3
"""The deployer's dependency direction: leaves upward only, and nothing imports the entry module.

`gitops_deploy.py` is the entry point; `deploy_toolbox.py` holds the injectable boundaries;
`deploy_logic.py` is an index that re-exports the decision modules and defines nothing itself.
Everything else is a leaf. Two rules keep that shape, and both fail silently without a guard:

1. **A leaf may not reach back up.** `deploy_toolbox` supplies `gitops_deploy`'s boundaries, so
   an import of `gitops_deploy` from it is a cycle — and when the entry module runs as
   `__main__`, a SECOND copy of it under a second name, with its own CONFIG and its own STATE.
2. **`deploy_logic.py` defines nothing.** A `def` added to the index is a def the layer map
   cannot place, and the first step back toward one 1.4k-line module.

This replaces `test_gitops_deploy_patch_boundary.py`, which required a patched name to be
defined on the module it was patched on. That rule existed because about 30 tests reached the
deployer's I/O by patching a module attribute. `DeployTools` (in
`ansible/roles/setup/gitops_deploy/files/deploy_toolbox.py`, with `tests/_deploy_fakes.py`)
replaced those patches with an injected object, so the rule now governs almost nothing and the
direction is what needs holding.

Run: uv run pytest ansible/tests/deploy/test_gitops_deploy_imports.py
"""

import ast

from _helpers import REPO

FILES = REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "files"
ENTRY = "gitops_deploy"

# Every module in files/, and which siblings each may import. `None` means no restriction, and
# only the entry module gets it. Checked with `==` against what is on disk, not `<=`: a new
# module with no entry here would otherwise be governed by nothing.
ALLOWED: dict[str, set[str] | None] = {
    "deploy_changes": set(),
    "deploy_config": set(),
    "deploy_failtext": set(),
    "deploy_git": set(),
    "deploy_health": set(),
    "deploy_staging": set(),
    "deploy_state": set(),
    "deploy_inventory": {"deploy_changes"},
    "deploy_k8s": {"deploy_changes"},
    "deploy_remediation": {"deploy_changes"},
    "deploy_alerts": {"deploy_config", "deploy_failtext", "deploy_remediation"},
    "deploy_io": {
        "deploy_config",
        "deploy_failtext",
        "deploy_health",
        "deploy_inventory",
        "deploy_k8s",
        "deploy_staging",
        "deploy_state",
    },
    # The index over the decision modules.
    "deploy_logic": {
        "deploy_changes",
        "deploy_git",
        "deploy_health",
        "deploy_inventory",
        "deploy_k8s",
        "deploy_remediation",
        "deploy_staging",
    },
    # The boundaries object: `deploy_io` for the transport, `deploy_alerts` for the webhook,
    # `deploy_git` for the CI verdict, `deploy_config` for the Config it binds. Never
    # `gitops_deploy` — that is rule 1.
    "deploy_toolbox": {"deploy_alerts", "deploy_config", "deploy_git", "deploy_io"},
    ENTRY: None,
}


def _modules_on_disk() -> set[str]:
    return {p.stem for p in FILES.glob("*.py")}


def _sibling_imports(source: str, siblings: set[str]) -> set[str]:
    """Every sibling module `source` imports, by either form.

    `import deploy_io` and `from deploy_io import run` both count: which form a module uses
    changes nothing about the direction of the edge.
    """
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name in siblings}
        elif isinstance(node, ast.ImportFrom) and node.module in siblings:
            found.add(node.module)
    return found


def _defined_names(source: str) -> set[str]:
    """Names a module DEFINES at top level: def, class, assignment. Imports do not count."""
    defined = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    defined.add(t.id)
    return defined


# --- The live tree ---------------------------------------------------------------------------


def test_the_module_set_is_exactly_what_is_on_disk():
    """A module added to files/ with no ALLOWED entry is governed by nothing without this."""
    on_disk = _modules_on_disk()
    assert set(ALLOWED) == on_disk, (
        f"missing from ALLOWED: {sorted(on_disk - set(ALLOWED))}; "
        f"listed but absent: {sorted(set(ALLOWED) - on_disk)}"
    )


def test_no_module_imports_outside_its_allowed_set():
    """Iterates what is ON DISK, so a new module with no ALLOWED entry fails here."""
    siblings = _modules_on_disk()
    assert len(siblings) >= 14, (
        f"the glob matched {len(siblings)} modules — FILES is wrong"
    )
    for name in sorted(siblings):
        assert name in ALLOWED, f"{name} has no ALLOWED entry"
        if ALLOWED[name] is None:
            continue
        got = _sibling_imports((FILES / f"{name}.py").read_text(), siblings)
        assert got <= ALLOWED[name], f"{name} imports {sorted(got - ALLOWED[name])}"


def test_no_leaf_imports_the_entry_module():
    """The rule ALLOWED encodes, asserted directly so the failure names the cycle.

    A leaf that imports `gitops_deploy` gets a second copy of it whenever the deployer runs as
    `__main__`, with its own CONFIG object and its own STATE.
    """
    siblings = _modules_on_disk()
    importers = sorted(
        name
        for name in siblings - {ENTRY}
        if ENTRY in _sibling_imports((FILES / f"{name}.py").read_text(), siblings)
    )
    assert not importers, f"{importers} import the entry module — that is a cycle"


def test_the_index_defines_nothing():
    """deploy_logic.py is an index over the decision modules, not a home for code."""
    assert _defined_names((FILES / "deploy_logic.py").read_text()) == set()


# --- Proof the guard can go red ---------------------------------------------------------------


def test_both_import_forms_are_seen():
    """The reject half for the reader: a plain import and a from-import weigh the same."""
    source = "import deploy_io\nfrom deploy_alerts import post\nimport json\n"
    assert _sibling_imports(source, {"deploy_io", "deploy_alerts"}) == {
        "deploy_io",
        "deploy_alerts",
    }


def test_a_leaf_importing_the_entry_module_is_flagged():
    assert _sibling_imports("from gitops_deploy import REPO\n", {ENTRY}) == {ENTRY}


def test_a_def_on_the_index_is_flagged():
    assert _defined_names("def ci_verdict(runs, required):\n    return 'pass'\n") == {
        "ci_verdict"
    }


def test_a_re_export_is_not_a_definition():
    """A facade BINDS every name it re-exports; only a def/class/assignment defines one."""
    assert _defined_names("from deploy_git import ci_verdict\n") == set()
