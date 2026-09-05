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
import os
import subprocess
import sys

from _helpers import REPO

FILES = REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "files"
ENTRY = "gitops_deploy"
# The two modules every caller reaches QUALIFIED — `deploy_io.deploy_k8s(...)`, never
# `from deploy_io import deploy_k8s`. The role's CLAUDE.md states the rule; this is its guard.
QUALIFIED = {"deploy_io", "deploy_alerts"}

# Every module in files/, and which siblings each may import. `None` means no restriction, and
# only the entry module gets it. Checked with `==` against what is on disk, not `<=`: a new
# module with no entry here would otherwise be governed by nothing.
ALLOWED: dict[str, set[str] | None] = {
    "deploy_changes": set(),
    "deploy_config": set(),
    "deploy_failtext": set(),
    "deploy_git": set(),
    "deploy_health": set(),
    "deploy_inventory": {"deploy_changes"},
    "deploy_k8s": {"deploy_changes"},
    "deploy_remediation": {"deploy_changes"},
    "deploy_tick_types": {"deploy_changes"},
    # The marker files, plus the two pure hold-marker decisions `clear_broad_hold` makes.
    "deploy_state": {"deploy_config", "deploy_git"},
    "deploy_io": {
        "deploy_config",
        "deploy_failtext",
        "deploy_health",
        "deploy_inventory",
        "deploy_k8s",
        "deploy_state",
    },
    # The boundaries object: `deploy_io` for the transport, `deploy_git` for the CI verdict,
    # `deploy_config` for the Config it binds. It holds `post`, the webhook itself, so that
    # `deploy_alerts` can import IT rather than the other way round. Never `gitops_deploy` —
    # that is rule 1.
    "deploy_toolbox": {"deploy_config", "deploy_git", "deploy_io"},
    "deploy_alerts": {
        "deploy_changes",
        "deploy_config",
        "deploy_failtext",
        "deploy_health",
        "deploy_io",
        "deploy_remediation",
        "deploy_state",
        "deploy_tick_types",
        "deploy_toolbox",
    },
    # Import-pure by contract, not by accident: `deploy_logic` re-exports it and the
    # scripts/deploy_tools/ tools import that index with only files/ on sys.path.
    # test_deploy_logic_imports_without_the_common_files_path is the executable half.
    "deploy_staging": set(),
    "deploy_phases": {
        "deploy_alerts",
        "deploy_changes",
        "deploy_config",
        "deploy_git",
        "deploy_inventory",
        "deploy_io",
        "deploy_k8s",
        "deploy_state",
        "deploy_tick_types",
        "deploy_toolbox",
    },
    "deploy_handlers": {
        "deploy_alerts",
        "deploy_changes",
        "deploy_config",
        "deploy_git",
        "deploy_health",
        "deploy_io",
        "deploy_k8s",
        "deploy_remediation",
        "deploy_staging",
        "deploy_state",
        "deploy_tick_types",
        "deploy_toolbox",
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
    ENTRY: None,
}


def _modules_on_disk() -> set[str]:
    return {p.stem for p in FILES.glob("*.py")}


def _from_imports(source: str, names: set[str]) -> set[str]:
    """Every module in `names` that `source` reaches by `from <module> import ...`."""
    return {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module in names
    }


def _plain_imports(source: str, names: set[str]) -> set[str]:
    """Every module in `names` that `source` reaches by an unaliased `import <module>`.

    `import deploy_io as dio` is excluded on purpose: an alias is a second name for the module,
    the same thing the qualified-access rule exists to forbid, so `_aliased_imports` reports it.
    """
    return {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name in names and alias.asname is None
    }


def _aliased_imports(source: str, names: set[str]) -> set[str]:
    """Every module in `names` that `source` imports under another name."""
    return {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name in names and alias.asname is not None
    }


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
    assert len(siblings) >= 18, (
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


def test_deploy_io_and_deploy_alerts_are_reached_qualified():
    """`deploy_io.deploy_k8s(...)`, never `from deploy_io import deploy_k8s`.

    Those two modules are the deployer's I/O surface, and the suite reads the argv their
    functions build. A from-import binds a second name for one of them, which a reader
    chasing a call site cannot tell from a local def — and it is the shape that made the
    retired `test_gitops_deploy_patch_boundary.py` necessary in the first place.
    """
    plain = set()
    for name in sorted(_modules_on_disk()):
        source = (FILES / f"{name}.py").read_text()
        assert _from_imports(source, QUALIFIED) == set(), (
            f"{name} from-imports {sorted(_from_imports(source, QUALIFIED))} — "
            f"reach them qualified instead"
        )
        assert _aliased_imports(source, QUALIFIED) == set(), (
            f"{name} aliases {sorted(_aliased_imports(source, QUALIFIED))} — "
            f"reach them by their own name"
        )
        if _plain_imports(source, QUALIFIED):
            plain.add(name)
    # Non-vacuity, against named members rather than a count: with no module importing either
    # one, the loop above asserts nothing and still passes.
    assert {ENTRY, "deploy_toolbox"} <= plain, (
        f"only {sorted(plain)} import them at all"
    )


def test_a_from_import_of_the_io_surface_is_flagged():
    """The reject half. The plain form beside it must NOT count as a from-import."""
    source = (
        "import deploy_io\nfrom deploy_alerts import post\nimport deploy_alerts as da\n"
    )
    assert _from_imports(source, QUALIFIED) == {"deploy_alerts"}
    assert _plain_imports(source, QUALIFIED) == {"deploy_io"}
    assert _aliased_imports(source, QUALIFIED) == {"deploy_alerts"}


def test_deploy_logic_imports_without_the_common_files_path():
    """The index must import with ONLY the role's files/ on sys.path — no `host_lib`.

    `scripts/deploy_tools/await_ci.py`, `land_tags.py` and `backfill_staging_gate.py` all reach
    the deployer's decisions through `deploy_logic`, and each puts only this directory on
    `sys.path`. `host_lib` lives in `roles/setup/common/files`, which those tools never add, so
    any module-level import chain from `deploy_logic` down to `deploy_config` breaks `land.sh`
    with a `ModuleNotFoundError` five frames from anything the tool is about. That happened when
    the staging gate's I/O shell was moved into `deploy_staging.py`, and the suite only caught it
    because `test_land_shim.py` shells out. This asserts it directly.

    A subprocess rather than an import: the in-process `sys.path` already carries every role's
    files/ by the time this test runs, so nothing checked in-process can see the difference.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(FILES)

    def _import(module: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            env=env,
            capture_output=True,
            text=True,
        )

    assert _import("deploy_logic").returncode == 0, _import("deploy_logic").stderr
    # The reject half, through the identical env: `deploy_config` is the module the chain ended
    # at, and it MUST fail here. Without this, the test above passes just as happily when the
    # subprocess can no longer see the difference at all — an inherited path entry, a stray
    # PYTHONPATH, or `host_lib` landing in files/ one day.
    impure = _import("deploy_config")
    assert impure.returncode != 0, (
        "deploy_config imported with only files/ on the path — this test can no longer tell "
        "a pure import chain from an impure one"
    )
    assert "host_lib" in impure.stderr, impure.stderr


def test_the_pure_modules_the_index_re_exports_are_import_pure():
    """The reader's half of the rule above, and the one that names the offender.

    The subprocess says only that something broke. This says which module grew the edge, and
    fails the moment one of them imports anything with a `host_lib` chain under it.
    """
    impure = {
        "deploy_config",
        "deploy_io",
        "deploy_alerts",
        "deploy_toolbox",
        "deploy_state",
    }
    siblings = _modules_on_disk()
    indexed = ALLOWED["deploy_logic"]
    assert indexed, "deploy_logic re-exports nothing — ALLOWED is wrong"
    for name in sorted(indexed):
        got = _sibling_imports((FILES / f"{name}.py").read_text(), siblings)
        assert not (got & impure), (
            f"{name} imports {sorted(got & impure)} — the index re-exports it, so that "
            f"reaches host_lib from every scripts/deploy_tools/ caller"
        )


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
