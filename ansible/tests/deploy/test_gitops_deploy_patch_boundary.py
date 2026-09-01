#!/usr/bin/env python3
"""A test may patch a name only on the module that DEFINES it, and no runtime module may
from-import a name a test patches elsewhere.

`deploy_logic.py` split into `deploy_*` domain modules and became a facade that re-exports
every name. That opens two silent holes, both the ones monitor-bridge's check.py split hit
(`test_monitor_bridge_modules.py`, `test_bridge_patch_boundary.py`):

1. `monkeypatch.setattr(deploy_logic, "ci_verdict", fake)` succeeds — the facade binds the
   name — and rebinds a re-export that no function reads. `next_action` reads `ci_verdict`
   from `deploy_git`'s globals. The test passes against unpatched code.

2. `gitops_deploy.py` does `from deploy_logic import ci_verdict`, taking its own reference at
   import time. A later `monkeypatch.setattr(deploy_git, "ci_verdict", fake)` rebinds
   deploy_git's attribute, and gitops_deploy's copy never sees it.

The monitor-bridge guard counts an imported name as "bound", which a facade defeats: every
name is bound there and none is defined. This one requires a def/class/assignment at the
patched module's top level. Today the suite patches exactly one pair — `gitops_deploy.run`,
which gitops_deploy defines — so the census sanity test pins that the walk still finds it.

Run: uv run pytest ansible/tests/test_gitops_deploy_patch_boundary.py
"""

import ast

from _helpers import REPO


FILES = REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "files"


def _runtime_modules():
    return sorted(
        p.stem
        for p in FILES.glob("*.py")
        if not p.name.startswith("test_") and p.name != "conftest.py"
    )


def _test_files():
    return sorted(
        p
        for p in FILES.glob("*.py")
        if p.name.startswith("test_") or p.name == "conftest.py"
    )


def _patched_pairs(test_sources, module_names):
    """{module: {name}} for every attribute of a runtime module a test assigns, patches, or
    mutates in place. An AST walk, not a grep: a wrapped `monkeypatch.setattr(` and a plain
    `mod.X = ...` assignment both count."""
    pairs = {}

    def _add(module, name):
        if module in module_names:
            pairs.setdefault(module, set()).add(name)

    for source in test_sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                fn = node.func
                is_setattr = (
                    isinstance(fn, ast.Attribute) and fn.attr in ("setattr", "delattr")
                ) or (isinstance(fn, ast.Name) and fn.id in ("setattr", "delattr"))
                if is_setattr and len(node.args) >= 2:
                    target, attr = node.args[0], node.args[1]
                    if (
                        isinstance(target, ast.Name)
                        and isinstance(attr, ast.Constant)
                        and isinstance(attr.value, str)
                    ):
                        _add(target.id, attr.value)
                if isinstance(fn, ast.Attribute):
                    inner = fn.value
                    if isinstance(inner, ast.Attribute) and isinstance(
                        inner.value, ast.Name
                    ):
                        _add(inner.value.id, inner.attr)
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    _add(t.value.id, t.attr)
    return pairs


def _defined_names(source):
    """Names a module DEFINES at top level: def, class, assignment. Imports do not count —
    that is the whole difference from the monitor-bridge guard, and what makes a facade fail."""
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


def _undefined_patches(pairs, sources):
    """[(module, name)] for every patched pair the module's source does not define."""
    return sorted(
        (module, name)
        for module, names in pairs.items()
        for name in names
        if name not in _defined_names(sources[module])
    )


def _from_imports(source):
    """{(module, name)} for every `from <module> import <name>` in a source."""
    found = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                found.add((node.module, alias.name))
    return found


def _leaked_from_imports(pairs, runtime_sources):
    """[(importer, module, name)] where a runtime module from-imports a name a test patches
    on another module. Names patched on the importer itself are its own defs and fine."""
    leaks = []
    for importer, source in runtime_sources.items():
        for module, name in _from_imports(source):
            if module in pairs and name in pairs[module] and module != importer:
                leaks.append((importer, module, name))
    return sorted(leaks)


def _live_pairs():
    return _patched_pairs(
        [p.read_text() for p in _test_files()], set(_runtime_modules())
    )


# --- The live tree ---------------------------------------------------------------------------


def test_the_census_still_finds_the_one_known_patch():
    """Without this the assertions below pass vacuously if the AST walk stops matching."""
    pairs = _live_pairs()
    assert "gitops_deploy" in pairs and "run" in pairs["gitops_deploy"], pairs


def test_every_patched_name_is_defined_on_the_module_it_is_patched_on():
    pairs = _live_pairs()
    sources = {m: (FILES / f"{m}.py").read_text() for m in pairs}
    undefined = _undefined_patches(pairs, sources)
    assert not undefined, (
        f"patched on a module that does not define the name: {undefined} — a re-export "
        "rebinds nothing any function reads, so the test passes against unpatched code. "
        "Patch the deploy_* module whose function reads the name."
    )


def test_no_runtime_module_from_imports_a_name_a_test_patches_elsewhere():
    pairs = _live_pairs()
    sources = {m: (FILES / f"{m}.py").read_text() for m in _runtime_modules()}
    leaks = _leaked_from_imports(pairs, sources)
    assert not leaks, (
        f"from-imported a patched name: {leaks} — the importer holds its own reference from "
        "import time and never sees the patch. Reach it qualified (`module.name`)."
    )


def test_the_facade_defines_nothing():
    """deploy_logic.py is an index. A def added there is a def the two rules above cannot
    place, and the first step back toward one 1.4k-line module."""
    assert _defined_names((FILES / "deploy_logic.py").read_text()) == set()


# --- Proof the guard can go red ----------------------------------------------------------------


def test_facade_patch_is_flagged():
    test = (
        "import deploy_logic\n\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(deploy_logic, "ci_verdict", lambda *a: "pass")\n'
    )
    pairs = _patched_pairs([test], {"deploy_logic", "deploy_git"})
    assert pairs == {"deploy_logic": {"ci_verdict"}}
    sources = {"deploy_logic": "from deploy_git import ci_verdict\n"}
    assert _undefined_patches(pairs, sources) == [("deploy_logic", "ci_verdict")]


def test_patch_on_the_defining_module_is_clean():
    test = (
        "import deploy_git\n\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(deploy_git, "ci_verdict", lambda *a: "pass")\n'
    )
    pairs = _patched_pairs([test], {"deploy_git"})
    sources = {"deploy_git": "def ci_verdict(runs, required):\n    return 'pass'\n"}
    assert _undefined_patches(pairs, sources) == []


def test_plain_assignment_and_in_place_mutation_are_counted():
    test = (
        "import deploy_health\n\n"
        "def test_x():\n"
        "    deploy_health.PENDING_ALERTS_MAX = 1\n"
        "    deploy_health._queue.clear()\n"
    )
    pairs = _patched_pairs([test], {"deploy_health"})
    assert pairs == {"deploy_health": {"PENDING_ALERTS_MAX", "_queue"}}


def test_from_import_of_a_patched_name_is_flagged():
    pairs = {"deploy_git": {"ci_verdict"}}
    sources = {
        "gitops_deploy": "from deploy_git import ci_verdict\n",
        "deploy_git": "def ci_verdict(runs, required):\n    return 'pass'\n",
    }
    assert _leaked_from_imports(pairs, sources) == [
        ("gitops_deploy", "deploy_git", "ci_verdict")
    ]


def test_qualified_access_to_a_patched_name_is_clean():
    pairs = {"deploy_git": {"ci_verdict"}}
    sources = {
        "gitops_deploy": "import deploy_git\n\ndef tick():\n    return deploy_git.ci_verdict()\n",
        "deploy_git": "def ci_verdict(runs, required):\n    return 'pass'\n",
    }
    assert _leaked_from_imports(pairs, sources) == []
