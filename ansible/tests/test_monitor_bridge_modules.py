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
# reads globals from the module it is DEFINED in, so a function that moved out of check.py while
# a test patches its name — or a name it reads — leaves the test patching something nothing
# reads. check.py re-imports every moved name, so the patch still SUCCEEDS and simply has no
# effect: the test passes against unpatched production code.
#
# That is a silent loss of coverage, which no amount of green runs will surface. Hence a guard.
#
# The census must be an AST walk, not a grep. A line-oriented regex over
# `monkeypatch.setattr(check, "X"` misses the wrapped form ruff format produces and misses plain
# `check.X = ...` assignment entirely — that hole hid `_cpu_breach_streak`, `_down_streaks` and
# `_evaluate` when this split was first measured.

SPLIT_MODULES = [
    "bridge_parsing",
    "verdicts_cluster",
    "verdicts_host",
    "verdicts_service",
]


def _patched_names():
    """Every attribute of `check` the test suite assigns, patches, or mutates in place."""
    names = set()
    for path in sorted(FILES.glob("*.py")):
        if not (path.name.startswith("test_") or path.name == "conftest.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                fn = node.func
                is_setattr = (
                    isinstance(fn, ast.Attribute) and fn.attr == "setattr"
                ) or (isinstance(fn, ast.Name) and fn.id == "setattr")
                if is_setattr and len(node.args) >= 2:
                    target, attr = node.args[0], node.args[1]
                    if (
                        isinstance(target, ast.Name)
                        and target.id == "check"
                        and isinstance(attr, ast.Constant)
                        and isinstance(attr.value, str)
                    ):
                        names.add(attr.value)
                # check.NAME.mutate(...) — conftest's check._down_streaks.clear()
                if isinstance(fn, ast.Attribute):
                    inner = fn.value
                    if (
                        isinstance(inner, ast.Attribute)
                        and isinstance(inner.value, ast.Name)
                        and inner.value.id == "check"
                    ):
                        names.add(inner.attr)
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if (
                    isinstance(t, ast.Attribute)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "check"
                ):
                    names.add(t.attr)
    return names


def test_no_split_module_defines_a_name_the_suite_patches():
    patched = _patched_names()
    offenders = {}
    for module in SPLIT_MODULES:
        tree = ast.parse((FILES / f"{module}.py").read_text())
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        if defined & patched:
            offenders[module] = sorted(defined & patched)
    assert not offenders, (
        f"moved out of check.py but still patched there: {offenders} — "
        "the patch would bind a name nothing reads, so its tests pass against unpatched code"
    )


def test_no_split_module_reads_a_name_the_suite_patches():
    patched = _patched_names()
    offenders = {}
    for module in SPLIT_MODULES:
        tree = ast.parse((FILES / f"{module}.py").read_text())
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            used = {d.id for d in ast.walk(node) if isinstance(d, ast.Name)}
            hits = (used & patched) - defined
            if hits:
                offenders.setdefault(module, []).append((node.name, sorted(hits)))
    assert not offenders, (
        f"split-out code reads names patched on check: {offenders} — "
        "patching them in a test cannot affect this module's globals"
    )
