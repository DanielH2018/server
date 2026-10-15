#!/usr/bin/env python3
"""The deployer's dependency direction: leaves upward only, and nothing imports the entry module.

`gitops_deploy.py` is the entry point; `deploy_toolbox.py` holds the injectable boundaries;
`deploy_logic.py` is an index that re-exports the decision modules and defines nothing itself.
Everything else is a leaf. Two rules keep that shape, and both fail silently without a guard:

1. **A leaf may not reach back up.** `deploy_toolbox` supplies `gitops_deploy`'s boundaries, so
   an import of `gitops_deploy` from it is a cycle — and when the entry module runs as
   `__main__`, a SECOND copy of it under a second name, with its own CONFIG.
2. **`deploy_logic.py` defines nothing.** A `def` added to the index is a def the layer map
   cannot place, and the first step back toward one 1.4k-line module.

`DeployTools` (in `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_toolbox.py`, with
`tests/_deploy_fakes.py`) injects the deployer's I/O boundaries, so tests do not patch module
attributes and the direction of the imports is what needs holding.

Run: uv run pytest ansible/tests/deploy/test_gitops_deploy_imports.py
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

from _gitops_deploy_allowed import ALLOWED, ENTRY
from _helpers import REPO
from lib.proc_testing import run

FILES = REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "files"
# The `deploy_*` modules live in this namespace package beside the entry point (#4355). ALLOWED
# keys every module by its stem, wherever it sits.
PACKAGE = "deployer_lib"
# The three modules every caller reaches QUALIFIED — `deploy_io.deploy_k8s(...)` after
# `from deployer_lib import deploy_io`, never `from deployer_lib.deploy_io import deploy_k8s`. The role's CLAUDE.md states the rule; this is its guard.
QUALIFIED = {"deploy_io", "deploy_alerts", "deploy_alert_text"}


def _sources() -> dict[str, Path]:
    """Every module's stem mapped to its file: flat in files/, or in the `deployer_lib` package."""
    found = list(FILES.glob("*.py")) + list((FILES / PACKAGE).glob("*.py"))
    return {p.stem: p for p in found}


def _modules_on_disk() -> set[str]:
    return set(_sources())


def _source(name: str) -> str:
    return _sources()[name].read_text()


def _edges(source: str) -> list[tuple[str, str, bool]]:
    """Each import in `source` as (module stem, form, aliased).

    The form is `plain` when the statement binds the module itself (`import gitops_markers`,
    `from deployer_lib import deploy_io`) and `from` when it binds names out of it
    (`from deployer_lib.deploy_io import run`). A package module's stem is its name inside
    `deployer_lib`, so the rules below read the same for both layouts.
    """
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for a in node.names:
                stem = a.name.removeprefix(f"{PACKAGE}.")
                out.append((stem, "plain", a.asname is not None))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == PACKAGE:
                out += [(a.name, "plain", a.asname is not None) for a in node.names]
            else:
                out.append((node.module.removeprefix(f"{PACKAGE}."), "from", False))
    return out


def _from_imports(source: str, names: set[str]) -> set[str]:
    """Every module in `names` that `source` binds names out of, `from <module> import ...`."""
    return {m for m, form, _ in _edges(source) if form == "from" and m in names}


def _plain_imports(source: str, names: set[str]) -> set[str]:
    """Every module in `names` that `source` binds by its own name, unaliased.

    `from deployer_lib import deploy_io as dio` is excluded on purpose: an alias is a second
    name for the module, the same thing the qualified-access rule exists to forbid, so
    `_aliased_imports` reports it.
    """
    return {
        m
        for m, form, aliased in _edges(source)
        if form == "plain" and not aliased and m in names
    }


def _aliased_imports(source: str, names: set[str]) -> set[str]:
    """Every module in `names` that `source` imports under another name."""
    return {m for m, form, aliased in _edges(source) if aliased and m in names}


def _sibling_imports(source: str, siblings: set[str]) -> set[str]:
    """Every sibling module `source` imports, by either form.

    `from deployer_lib import deploy_io` and `from deployer_lib.deploy_io import run` both
    count: which form a module uses changes nothing about the direction of the edge.
    """
    return {m for m, _, _ in _edges(source) if m in siblings}


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
    # Named members from both halves of the layout, so a move that empties either glob fails.
    assert {ENTRY, "gitops_markers", "deploy_logic", "deploy_io"} <= siblings, (
        f"the globs matched {sorted(siblings)} — FILES or PACKAGE is wrong"
    )
    for name in sorted(siblings):
        assert name in ALLOWED, f"{name} has no ALLOWED entry"
        allowed = ALLOWED[name]
        if allowed is None:
            continue
        got = _sibling_imports(_source(name), siblings)
        assert got <= allowed, f"{name} imports {sorted(got - allowed)}"


def test_no_leaf_imports_the_entry_module():
    """The rule ALLOWED encodes, asserted directly so the failure names the cycle.

    A leaf that imports `gitops_deploy` gets a second copy of it whenever the deployer runs as
    `__main__`, with its own CONFIG object.
    """
    siblings = _modules_on_disk()
    importers = sorted(
        name
        for name in siblings - {ENTRY}
        if ENTRY in _sibling_imports(_source(name), siblings)
    )
    assert not importers, f"{importers} import the entry module — that is a cycle"


def test_deploy_io_and_deploy_alerts_are_reached_qualified():
    """`deploy_io.deploy_k8s(...)`, never `from deploy_io import deploy_k8s`.

    Those two modules are the deployer's I/O surface, and the suite reads the argv their
    functions build. A from-import binds a second name for one of them, which a reader
    chasing a call site cannot tell from a local def — and it is the shape that makes
    tests patch a module attribute instead of using `DeployTools`.
    """
    plain = set()
    for name in sorted(_modules_on_disk()):
        source = _source(name)
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
        "from deployer_lib import deploy_io\n"
        "from deployer_lib.deploy_alerts import post\n"
        "from deployer_lib import deploy_alerts as da\n"
    )
    assert _from_imports(source, QUALIFIED) == {"deploy_alerts"}
    assert _plain_imports(source, QUALIFIED) == {"deploy_io"}
    assert _aliased_imports(source, QUALIFIED) == {"deploy_alerts"}


def test_deploy_logic_imports_without_the_common_files_path():
    """The index must import with ONLY the role's files/ on sys.path — no `host_lib`.

    `scripts/deploy_tools/await_ci.py` and `land_tags.py` both reach
    the deployer's decisions through `deploy_logic`, and each puts only this directory on
    `sys.path`. `host_lib` lives in `roles/setup/common/files`, which those tools never add, so
    any module-level import chain from `deploy_logic` down to `deploy_config` breaks `land.sh`
    with a `ModuleNotFoundError` five frames from anything the tool is about. Only a test that
    shells out notices a re-export that drags such a chain in. This asserts it directly.

    A subprocess rather than an import: the in-process `sys.path` already carries every role's
    files/ by the time this test runs, so nothing checked in-process can see the difference.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(FILES)

    def _import(module: str) -> subprocess.CompletedProcess:
        return run([sys.executable, "-c", f"import {module}"], env=env)

    pure = _import(f"{PACKAGE}.deploy_logic")
    assert pure.returncode == 0, pure.stderr
    # The reject half, through the identical env: `deploy_config` is the module the chain ended
    # at, and it MUST fail here. Without this, the test above passes just as happily when the
    # subprocess can no longer see the difference at all — an inherited path entry, a stray
    # PYTHONPATH, or `host_lib` landing in files/ one day.
    impure = _import(f"{PACKAGE}.deploy_config")
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
        got = _sibling_imports(_source(name), siblings)
        assert not (got & impure), (
            f"{name} imports {sorted(got & impure)} — the index re-exports it, so that "
            f"reaches host_lib from every scripts/deploy_tools/ caller"
        )


def test_the_index_defines_nothing():
    """deploy_logic.py is an index over the decision modules, not a home for code."""
    assert _defined_names(_source("deploy_logic")) == set()


# --- Proof the guard can go red ---------------------------------------------------------------


def test_both_import_forms_are_seen():
    """The reject half for the reader: a plain import and a from-import weigh the same."""
    source = (
        "from deployer_lib import deploy_io\n"
        "from deployer_lib.deploy_alerts import post\nimport json\n"
    )
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
    assert _defined_names("from deployer_lib.deploy_git import ci_verdict\n") == set()
