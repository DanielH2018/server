#!/usr/bin/env python3
"""Every runtime module in monitor-bridge's files/ must be in its ConfigMap ship list.

check.py was one 3.5k-line file until it was split. The split introduced a failure mode the
test suite structurally cannot see: pytest imports the modules from `files/` on disk, so it
goes green whatever the ship list says, while the pod only ever receives the files named in
`monitor_bridge_modules`. A module added to files/ and forgotten here therefore passes CI and
kills the bridge at import on its next roll:

    ModuleNotFoundError: No module named 'bridge.parsing'

That lands on the one workload that cannot page about its own failure — monitor-bridge IS the
alert pipeline, which is why the role sets `k8s_autodeploy: false`. The pod would crashloop and
the estate would go quiet rather than loud.

The list is deliberately explicit rather than a `files/*.py` glob: nothing on disk stops a
future test file from landing in `files/` instead of the sibling `tests/` directory, and if
one did, it must not reach the image. This test is what keeps the explicit list honest.

A module is identified by its dotted path under `files/` (`bridge/config.py` is
`bridge.config`), and the ship list carries paths relative to `files/`, so the census sees a
module at any depth. The one-level `FILES.glob("*.py")` this used until 2026-09-02 would have
returned nothing for a packaged layout and compared an empty census against the ship list.

Run: uv run pytest ansible/tests/services/test_monitor_bridge_modules.py
"""

import ast

from lib import yaml_fast
from _helpers import (
    REPO,
    import_bindings,
    imported_module_ids,
    module_id,
    module_of,
    python_modules,
)


ROLE = REPO / "ansible" / "roles" / "k8s" / "monitor-bridge"
FILES = ROLE / "files"
TESTS = ROLE / "tests"
ENTRYPOINT = "cli.py"


def _ship_list():
    defaults = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
    return list(defaults["monitor_bridge_modules"])


def _runtime_modules():
    """Every production .py under files/, as a path relative to files/, at any depth."""
    return sorted(
        p.relative_to(FILES).as_posix() for p in python_modules(FILES).values()
    )


def test_ship_list_matches_the_runtime_modules_on_disk():
    assert sorted(_ship_list()) == _runtime_modules()


def test_ship_list_carries_the_entrypoint():
    """The Deployment runs `python /app/cli.py`; shipping the rest without it deploys nothing."""
    assert ENTRYPOINT in _ship_list()


def test_ship_list_excludes_the_test_suite():
    shipped = set(_ship_list())
    tests = {p.name for p in TESTS.glob("test_*.py")} | {"conftest.py"}
    assert not (shipped & tests)


def test_every_module_the_entrypoint_imports_is_shipped():
    """The direct check: whatever cli.py imports from its own directory must travel with it.

    Set equality above already implies this today. It is asserted separately because it is the
    property that actually breaks the pod, and it keeps holding if the runtime/test split above
    is ever loosened.
    """
    local = set(python_modules(FILES))
    shipped = {module_id(FILES / m, FILES) for m in _ship_list()}
    tree = ast.parse((FILES / ENTRYPOINT).read_text())
    missing = imported_module_ids(tree, local) - shipped
    assert not missing, (
        f"{ENTRYPOINT} imports {sorted(missing)}, absent from monitor_bridge_modules"
    )


def test_the_census_sees_a_module_inside_a_package(tmp_path):
    """Red-proof for depth: a packaged layout must census the same way a flat one does."""
    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge" / "config.py").write_text("X = 1\n")
    (tmp_path / "check.py").write_text("from bridge import config as cfg\n")
    (tmp_path / "test_stray.py").write_text("")
    assert set(python_modules(tmp_path)) == {"bridge.config", "check"}
    tree = ast.parse((tmp_path / "check.py").read_text())
    assert imported_module_ids(tree, {"bridge.config", "check"}) == {"bridge.config"}


# --- The second invariant: every patched name is bound in the module the test patches it on ---
#
# The suite patches the runtime modules ~210 times: `bridge.net._get_json`, `bridge.config.X`,
# a verdict on the `checks_*` module that from-imports it, `check.CHECKS`. A function reads its
# globals from the module it is DEFINED in, so a patch lands only if the module named in the
# test is the module whose code reads the name. Patch `check.PROM_URL` after PROM_URL moved to
# bridge.config and the setattr still SUCCEEDS — it creates a new attribute on `check` that
# nothing reads — and the test passes against unpatched production code.
#
# That is a silent loss of coverage, which no amount of green runs will surface. Hence a guard:
# every `(module, name)` pair the suite patches must be bound at that module's top level, by a
# `def`, an assignment, or an import. The companion rule — a module must not from-import a name
# some test patches on its source module — is ansible/tests/services/test_bridge_patch_boundary.py.
#
# The census must be an AST walk, not a grep. A line-oriented regex over
# `monkeypatch.setattr(check, "X"` misses the wrapped form ruff format produces and misses plain
# `check.X = ...` assignment entirely — that hole hid `_cpu_breach_streak`, `_down_streaks` and
# `_evaluate` when the split was first measured.
#
# The test's local name for a module is resolved through its own imports, so `from bridge import
# config as cfg` followed by `monkeypatch.setattr(cfg, "X", 1)` is a patch on `bridge.config`,
# and `bridge.config.X = 1` after `import bridge.config` is the same patch spelled longhand.


def _runtime_module_names():
    return set(python_modules(FILES))


def _patched_pairs(test_files=None, module_names=None):
    """Map each runtime module to the attributes the suite assigns, patches, or mutates in place.

    Returns `{module: {name}}`.
    """
    module_names = module_names if module_names is not None else _runtime_module_names()
    if test_files is None:
        test_files = sorted(
            p
            for p in TESTS.glob("*.py")
            if p.name.startswith("test_") or p.name == "conftest.py"
        )
    pairs = {}

    for path in test_files:
        tree = ast.parse(path.read_text())
        bound = import_bindings(tree, module_names)

        def _add(target, name, bound=bound):
            module = module_of(target, bound, module_names)
            if module:
                pairs.setdefault(module, set()).add(name)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                is_setattr = (
                    isinstance(fn, ast.Attribute) and fn.attr in ("setattr", "delattr")
                ) or (isinstance(fn, ast.Name) and fn.id in ("setattr", "delattr"))
                if is_setattr and len(node.args) >= 2:
                    target, attr = node.args[0], node.args[1]
                    if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                        _add(target, attr.value)
                # module.NAME.mutate(...) — conftest's check._down_streaks.clear()
                if isinstance(fn, ast.Attribute) and isinstance(
                    fn.value, ast.Attribute
                ):
                    _add(fn.value.value, fn.value.attr)
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute):
                    _add(t.value, t.attr)
    return pairs


def _top_level_bindings(source):
    """Every name a module binds at its top level: def, class, assignment, import."""
    bound = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
    return bound


def _unbound_patches(pairs, sources):
    """[(module, name)] for every patched pair the module's source does not bind."""
    return sorted(
        (module, name)
        for module, names in pairs.items()
        for name in names
        if name not in _top_level_bindings(sources[module])
    )


def test_the_patch_census_spans_more_than_one_module():
    # Without this the assertion below passes vacuously if the AST walk stops matching.
    #
    # It named `check` until 2026-09-05, when the registry moved to registry.py and the gate
    # sets and probes became the `Gates` value run_once is handed. Nothing patches the run loop
    # any more — which is the point of that seam, not a lapsed census — so the pair that keeps
    # this honest is `bridge.common` and `bridge.net`, the two the suite still stubs. Repoint it
    # again rather than deleting it when the next module gets its seam.
    pairs = _patched_pairs()
    assert {"bridge.common", "bridge.net"} <= pairs.keys(), sorted(pairs)
    assert len(pairs) >= 2, sorted(pairs)


def test_every_patched_name_is_bound_in_the_module_it_is_patched_on():
    pairs = _patched_pairs()
    modules = python_modules(FILES)
    sources = {m: modules[m].read_text() for m in pairs}
    unbound = _unbound_patches(pairs, sources)
    assert not unbound, (
        f"patched on a module that does not bind the name: {unbound} — the setattr creates "
        "an attribute nothing reads, so the test passes against unpatched code. Patch the "
        "module whose code reads the name."
    )


def test_unbound_patch_checker_fires_on_a_synthesized_bad_sample(tmp_path):
    """Prove the checker can fail: a test patching `mod.gone` where mod never binds `gone`."""
    bad_test = tmp_path / "test_bad.py"
    bad_test.write_text(
        "import mod\n\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(mod, "gone", 1)\n'
        '    monkeypatch.setattr(mod, "kept", 2)\n'
        "    mod.also_gone = 3\n"
    )
    pairs = _patched_pairs([bad_test], {"mod"})
    assert pairs == {"mod": {"gone", "kept", "also_gone"}}
    unbound = _unbound_patches(pairs, {"mod": "from elsewhere import kept\n"})
    assert unbound == [("mod", "also_gone"), ("mod", "gone")]


def test_the_patch_census_resolves_every_spelling_of_a_packaged_module(tmp_path):
    """Red-proof for depth: the four ways a test can name `bridge.config` all census as it.

    An alias (`as cfg`), a from-import of the module, a dotted import, and a dotted import with
    its own alias. A census keyed on the bare `Name` a test used would file three of these
    under names no module has and lose the patch silently.
    """
    test = tmp_path / "test_forms.py"
    test.write_text(
        "from bridge import config as cfg\n"
        "from bridge import io\n"
        "import bridge.streaks\n"
        "import checks.b2 as b2\n\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(cfg, "A", 1)\n'
        '    monkeypatch.setattr(io, "B", 2)\n'
        "    bridge.streaks.C = 3\n"
        "    b2.D = 4\n"
        "    bridge.streaks._state.clear()\n"
    )
    ids = {"bridge.config", "bridge.io", "bridge.streaks", "checks.b2", "check"}
    assert _patched_pairs([test], ids) == {
        "bridge.config": {"A"},
        "bridge.io": {"B"},
        "bridge.streaks": {"C", "_state"},
        "checks.b2": {"D"},
    }
