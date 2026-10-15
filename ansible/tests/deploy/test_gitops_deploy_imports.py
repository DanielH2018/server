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

from _helpers import REPO
from lib.proc_testing import run

FILES = REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "files"
# The `deploy_*` modules live in this namespace package beside the entry point (#4355). ALLOWED
# keys every module by its stem, wherever it sits.
PACKAGE = "deployer_lib"
ENTRY = "gitops_deploy"
# The three modules every caller reaches QUALIFIED — `deploy_io.deploy_k8s(...)` after
# `from deployer_lib import deploy_io`, never `from deployer_lib.deploy_io import deploy_k8s`. The role's CLAUDE.md states the rule; this is its guard.
QUALIFIED = {"deploy_io", "deploy_alerts", "deploy_alert_text"}

# Every module in files/, and which siblings each may import. `None` means no restriction, and
# only the entry module gets it. The module SET is checked with `==` against what is on disk, so
# a new module with no entry here fails; each module's imports are checked with `<=`.
#
# DECIDED: keep the per-module allowlist rather than narrow it to "no cycles, nothing imports the
# entry module" (#4398). No commented copy between deployer modules exists because ALLOWED lacks
# an edge. Each comes from a real cycle (`gitops_markers.owed_clear_cmd`'s "manual_plane"
# literal), a module that ships alone (`gitops_markers`, `gitops_hold`), host_lib purity
# (`deploy_remediation.BRANCH_DEFAULT`), or a source outside files/ under `--no-project`
# (`deploy_release`'s dirs and verdicts, `deploy_k8s.SHARED_K8S_ROLES`, each with its own sync
# test), so the narrower rule would fold none of them. A copy behind a non-cycle edge folds
# by adding the edge here, as `DEPLOY_PLAYBOOK` and the `deploy_release` verdicts did (#4428).
ALLOWED: dict[str, set[str] | None] = {
    # The cross-role tables, a leaf of their own because the tick and land.sh parse another
    # commit's copy of that file by path.
    "deploy_changes": {"deploy_cross_role", "deploy_setup_roles"},
    "deploy_cross_role": set(),
    # Which playbook and tag apply a setup role, answered for a role name rather than a path.
    # Stdlib only: it runs the derivation as a subprocess (#3734).
    "deploy_setup_roles": set(),
    # The narrowing subprocess and the plan it returns: `deploy_config` for `log`, and
    # `gitops_hold` for `held_tag`, the hold-marker format `broad_hold_cleared_by` reads back
    # (#3138) — a stdlib leaf, so no cycle. `gitops_markers` for the playbook its deploy plane
    # names, and `deploy_release` for the render-digest shadow log its plan writes. It
    # must NOT import `deploy_toolbox`, which imports IT for the `narrow_deploy_plane` default.
    "deploy_narrow": {
        "deploy_config",
        "deploy_release",
        "deploy_setup_roles",
        "gitops_hold",
        "gitops_markers",
    },
    # The state directory, the marker table and the line parsers. Import-free by construction:
    # a copy of it ships into every other tree that reads the markers (its header says how),
    # so an import here would have to be satisfiable in a monitor-bridge pod.
    "gitops_markers": set(),
    # The JSON-lines markers (the owed-work ledger and the tick receipt). Reads
    # `gitops_markers` for the `K8sDeferredEntry` tuple and nothing else.
    "gitops_ledger": {"gitops_markers"},
    # The hold and the rule that clears it (#3658). The deploy UI installs it beside the two
    # modules above, so it may import nothing else.
    "gitops_hold": {"gitops_ledger", "gitops_markers"},
    # The k8s release records, read to discharge a `k8s_unapplied` line, and the shadow log's
    # `log` from `deploy_config`, a leaf. `deploy_toolbox` imports it for the `release_commit`
    # default, so an import of `deploy_toolbox` or anything reaching it would be a cycle; so
    # would `deploy_narrow`, whose `BroadPlan` the shadow log reads without importing.
    "deploy_release": {"deploy_config"},
    # The render bases a `manifests` line stores, as pure transforms of the ledger's text, and
    # `log` for the line naming the callers it waits on (#5118, #5122). `deploy_k8s_owed`
    # does the read and the write.
    "deploy_render_bases": {"deploy_config", "gitops_ledger"},
    "deploy_config": set(),
    "deploy_failtext": set(),
    "deploy_git": set(),
    "deploy_health": set(),
    "deploy_inventory": {"deploy_changes"},
    "deploy_k8s": {"deploy_changes"},
    # The per-service flocks. A leaf: `scripts/deploy.sh` takes the same locks by the same
    # names, and keeping this module free of the deployer's config is what lets a test drive
    # it directly.
    "deploy_locks": set(),
    # `gitops_ledger` for the `manual_plane` class name its clear command prints (#3547), and
    # `deploy_cross_role`, a leaf, for the roles that render `common`'s files (#4316).
    "deploy_remediation": {
        "deploy_changes",
        "deploy_cross_role",
        "gitops_ledger",
        "gitops_markers",
    },
    # The broad arm's two deferral shapes — park, or record in `manual_plane`. It reaches the
    # alert transport, so it sits with `deploy_handlers` rather than among the pure modules.
    "deploy_defer": {
        "deploy_alert_text",
        "deploy_alerts",
        "deploy_changes",
        "deploy_config",
        "deploy_narrow",
        "deploy_remediation",
        "deploy_state",
        "deploy_tick_types",
        "deploy_toolbox",
        "gitops_markers",
    },
    # The owed k8s changes, `k8s_deferred` and `k8s_unapplied` (#3669); no stage module touches them.
    "deploy_k8s_owed": {
        "deploy_alerts",
        "deploy_changes",
        "deploy_config",
        "deploy_k8s",
        "deploy_render_bases",
        "deploy_state",
        "deploy_toolbox",
        "gitops_markers",
    },
    "deploy_tick_types": {"deploy_changes"},
    # The k8s half of a broad range: applying the promoted bumps a plane did not.
    "deploy_broad_k8s": {
        "deploy_alert_text",
        "deploy_alerts",
        "deploy_changes",
        "deploy_config",
        "deploy_defer",
        "deploy_io",
        "deploy_k8s_owed",
        "deploy_locks",
        "deploy_state",
        "deploy_tick_types",
        "deploy_toolbox",
        "gitops_markers",
    },
    # The marker files, plus `behind_marker` from `deploy_git`, the hold's owner
    # `gitops_hold`, and `deploy_config` for `log`.
    "deploy_state": {
        "deploy_config",
        "deploy_git",
        "gitops_hold",
        "gitops_ledger",
        "gitops_markers",
    },
    "deploy_io": {
        "deploy_config",
        "deploy_failtext",
        "deploy_health",
        "deploy_inventory",
        "deploy_k8s",
        "deploy_locks",
        "deploy_state",
        "gitops_markers",
    },
    # The boundaries object: `deploy_io` for the transport, `deploy_git` for the CI verdict,
    # `deploy_config` for the Config it binds. It holds `post`, the webhook itself, so that
    # `deploy_alerts` can import IT rather than the other way round. Never `gitops_deploy` —
    # that is rule 1.
    "deploy_toolbox": {
        "deploy_config",
        "deploy_git",
        "deploy_io",
        "deploy_narrow",
        "deploy_release",
        "deploy_setup_roles",
    },
    # Every message body, and nothing that sends one: the ChangeSet its signatures take, the
    # error-text slicers and the k8s remediation it appends. No transport, no state:
    # `gitops_markers` is the leaf that names marker files and clear commands, and the alert
    # takes `HOLD_CLEAR_CMD` from it (#4015).
    "deploy_alert_text": {
        "deploy_changes",
        "deploy_failtext",
        "deploy_remediation",
        "gitops_markers",
    },
    "deploy_alerts": {
        "deploy_alert_text",
        "deploy_changes",
        "deploy_config",
        "deploy_health",
        "deploy_io",
        "deploy_remediation",
        "deploy_state",
        "deploy_tick_types",
        "deploy_toolbox",
    },
    "deploy_phases": {
        "deploy_alert_text",
        "deploy_alerts",
        "deploy_changes",
        "deploy_config",
        "deploy_cross_role",
        # `drop_deleted_setup_roles`, which `plan_tick` asks beside the k8s drop (#4326).
        "deploy_defer",
        "deploy_setup_roles",
        "deploy_git",
        "deploy_inventory",
        "deploy_io",
        "deploy_k8s",
        "deploy_k8s_owed",
        "deploy_state",
        "deploy_tick_types",
        "deploy_toolbox",
    },
    "deploy_handlers": {
        "deploy_alert_text",
        "deploy_alerts",
        "deploy_broad_k8s",
        "deploy_defer",
        "deploy_narrow",
        "deploy_changes",
        "deploy_config",
        "deploy_git",
        "deploy_health",
        "deploy_io",
        "deploy_k8s",
        "deploy_k8s_owed",
        "deploy_locks",
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
    },
    ENTRY: None,
}


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
