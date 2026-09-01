#!/usr/bin/env python3
"""Every runtime module in monitor-bridge's files/ must be in its ConfigMap ship list.

check.py was one 3.5k-line file until it was split. The split introduced a failure mode the
test suite structurally cannot see: pytest imports the modules from `files/` on disk, so it
goes green whatever the ship list says, while the pod only ever receives the files named in
`monitor_bridge_modules`. A module added to files/ and forgotten here therefore passes CI and
kills the bridge at import on its next roll:

    ModuleNotFoundError: No module named 'bridge_parsing'

That lands on the one workload that cannot page about its own failure — monitor-bridge IS the
alert pipeline, which is why the role sets `k8s_autodeploy: false`. The pod would crashloop and
the estate would go quiet rather than loud.

The list is deliberately explicit rather than a `files/*.py` glob, because files/ also holds
the pytest suite and conftest.py, which must not reach the image. This test is what keeps the
explicit list honest.

Run: uv run pytest ansible/tests/test_monitor_bridge_modules.py
"""

import ast

import yaml
from _helpers import REPO


ROLE = REPO / "ansible" / "roles" / "k8s" / "monitor-bridge"
FILES = ROLE / "files"
ENTRYPOINT = "check.py"


def _ship_list():
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    return list(defaults["monitor_bridge_modules"])


def _runtime_modules():
    """The .py files in files/ that are production code, not the test suite."""
    return sorted(
        p.name
        for p in FILES.glob("*.py")
        if not p.name.startswith("test_") and p.name != "conftest.py"
    )


def test_ship_list_matches_the_runtime_modules_on_disk():
    assert sorted(_ship_list()) == _runtime_modules()


def test_ship_list_carries_the_entrypoint():
    """The Deployment runs `python /app/check.py`; shipping the rest without it deploys nothing."""
    assert ENTRYPOINT in _ship_list()


def test_ship_list_excludes_the_test_suite():
    shipped = set(_ship_list())
    tests = {p.name for p in FILES.glob("test_*.py")} | {"conftest.py"}
    assert not (shipped & tests)


def test_every_module_check_py_imports_is_shipped():
    """The direct check: whatever check.py imports from its own directory must travel with it.

    Set equality above already implies this today. It is asserted separately because it is the
    property that actually breaks the pod, and it keeps holding if the runtime/test split above
    is ever loosened.
    """
    shipped = {m[: -len(".py")] for m in _ship_list()}
    local = {p.stem for p in FILES.glob("*.py")}
    tree = ast.parse((FILES / ENTRYPOINT).read_text())

    imported_locally = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module in local
        ):
            imported_locally.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in local:
                    imported_locally.add(alias.name)

    missing = imported_locally - shipped
    assert not missing, (
        f"check.py imports {sorted(missing)}, absent from monitor_bridge_modules"
    )


# --- The second invariant: a split module must not hold anything the suite patches ---
#
# check.py's test suite mutates the `check` module object ~65 distinct names deep. A function
# --- The second invariant: every patched name is bound in the module the test patches it on ---
#
# The suite patches the runtime modules ~210 times: `bridge_io._get_json`, `bridge_config.X`,
# a verdict on the `checks_*` module that from-imports it, `check.CHECKS`. A function reads its
# globals from the module it is DEFINED in, so a patch lands only if the module named in the
# test is the module whose code reads the name. Patch `check.PROM_URL` after PROM_URL moved to
# bridge_config and the setattr still SUCCEEDS — it creates a new attribute on `check` that
# nothing reads — and the test passes against unpatched production code.
#
# That is a silent loss of coverage, which no amount of green runs will surface. Hence a guard:
# every `(module, name)` pair the suite patches must be bound at that module's top level, by a
# `def`, an assignment, or an import. The companion rule — a module must not from-import a name
# some test patches on its source module — is ansible/tests/test_bridge_patch_boundary.py.
#
# The census must be an AST walk, not a grep. A line-oriented regex over
# `monkeypatch.setattr(check, "X"` misses the wrapped form ruff format produces and misses plain
# `check.X = ...` assignment entirely — that hole hid `_cpu_breach_streak`, `_down_streaks` and
# `_evaluate` when the split was first measured.


def _runtime_module_names():
    return {m[: -len(".py")] for m in _runtime_modules()}


def _patched_pairs(test_files=None, module_names=None):
    """{module: {name}} for every attribute of a runtime module the suite assigns, patches, or
    mutates in place."""
    module_names = module_names if module_names is not None else _runtime_module_names()
    if test_files is None:
        test_files = sorted(
            p
            for p in FILES.glob("*.py")
            if p.name.startswith("test_") or p.name == "conftest.py"
        )
    pairs = {}

    def _add(module, name):
        if module in module_names:
            pairs.setdefault(module, set()).add(name)

    for path in test_files:
        for node in ast.walk(ast.parse(path.read_text())):
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
                # module.NAME.mutate(...) — conftest's check._down_streaks.clear()
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


def test_the_patch_census_spans_more_than_the_entrypoint():
    # Without this the assertion below passes vacuously if the AST walk stops matching, or if
    # the suite quietly went back to patching everything on `check`.
    pairs = _patched_pairs()
    assert "check" in pairs and len(pairs) >= 2, sorted(pairs)


def test_every_patched_name_is_bound_in_the_module_it_is_patched_on():
    pairs = _patched_pairs()
    sources = {m: (FILES / f"{m}.py").read_text() for m in pairs}
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
