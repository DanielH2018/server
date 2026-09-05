"""A runtime module must not from-import a name its test suite patches on the source module.

`monkeypatch.setattr(bridge.common, "log", ...)` rebinds the attribute on the module object.
A module that did `from bridge.common import log` holds its own reference in its globals,
taken at import time, and that reference never sees the patch — the test passes while
patching nothing. The failure is silent in the direction that matters: a test believing it
stubbed the Kuma push would really push.

The consumers are derived, not listed: every k8s role whose `files/` imports a runtime module
defined under ANOTHER role's `files/`, plus that other role. That pair is monitor-bridge and
autofix-bridge today, and it was the scope of the fix this shipped with; a hardcoded pair
would let a third role join the rule silently unchecked — the guard-scope shape the same
review found in four other places (2026-08-25 review M-2). The deployer derives the same set
in `deploy_changes.shared_module_consumers`.

A module is identified by its dotted path under `files/`, so the census sees a module at any
depth and a test's own alias for it (`from bridge import config as cfg`) resolves to the
module it names. The one-level `glob("*.py")` and the `"bridge.common" in text` substring
this used until 2026-09-02 would both have gone quiet the moment the shared module moved into
a package.

Run: uv run pytest ansible/tests/services/test_bridge_patch_boundary.py
"""

import ast

from _helpers import (
    K8S_ROLES as K8S,
    import_bindings,
    imported_module_ids,
    module_of,
    python_modules,
)


def _roles_with_files():
    """Role name -> {module id: path} for every k8s role that ships Python under files/."""
    return {
        role.name: python_modules(role / "files")
        for role in sorted(K8S.iterdir())
        if (role / "files").is_dir()
    }


def _consumer_roots():
    """Every k8s role's files/ that shares a runtime module with another role, either way."""
    per_role = _roles_with_files()
    roots = set()
    for name, modules in per_role.items():
        for other, other_modules in per_role.items():
            if other == name:
                continue
            foreign = set(other_modules) - set(modules)
            if not foreign:
                continue
            for path in modules.values():
                tree = ast.parse(path.read_text(errors="ignore"), filename=str(path))
                if imported_module_ids(tree, foreign):
                    roots |= {name, other}
                    break
    return [K8S / name / "files" for name in sorted(roots)]


def _suite_files():
    """Every Python module under a consumer role's tests/ — the suites this rule binds.

    Every consumer's non-test modules are checked against the union of what any of those
    suites patches, since the shared module is shared between them. Tests live in `tests/`,
    a sibling of the `files/` roots `_consumer_roots()` returns, not inside `files/` itself.

    The glob is every `*.py`, not `test_*.py` + `conftest.py`. A shared helper module —
    monitor-bridge's `_check_gate_helpers.py`, which wires `run_once` for four suites — patches
    the transport exactly as a test does, and under the narrower glob those patches were
    invisible to this rule. A from-import of one of those names would then have passed.
    """
    files = []
    for root in _consumer_roots():
        tests_dir = root.parent / "tests"
        if tests_dir.is_dir():
            files += sorted(tests_dir.glob("*.py"))
    return files


def _runtime_modules():
    """Id -> path for every non-test module under a consumer role's files/, at any depth."""
    found = {}
    for base in _consumer_roots():
        found.update(python_modules(base))
    return found


def _patched_names_by_module(test_files=None, module_names=None):
    """Map each runtime module to the attributes any suite assigns, patches, or mutates.

    Returns `{module: {name}}`.

    AST walk, not a regex — a line-oriented regex over `monkeypatch.setattr(bridge.common, "X"`
    misses the wrapped form ruff format produces and misses plain `bridge.common.X = ...`
    assignment entirely. `ansible/tests/services/test_monitor_bridge_modules.py`'s census hit
    exactly that hole when first measured; this mirrors its AST shape rather than repeating
    the mistake. The test's local name for a module is resolved through its own imports.
    """
    test_files = _suite_files() if test_files is None else test_files
    if module_names is None:
        module_names = set(_runtime_modules())
    names = {}

    for path in test_files:
        tree = ast.parse(path.read_text(), filename=str(path))
        bound = import_bindings(tree, module_names)

        def _add(target, name, bound=bound):
            module = module_of(target, bound, module_names)
            if module:
                names.setdefault(module, set()).add(name)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                is_setattr = (
                    isinstance(fn, ast.Attribute) and fn.attr == "setattr"
                ) or (isinstance(fn, ast.Name) and fn.id == "setattr")
                if is_setattr and len(node.args) >= 2:
                    target, attr = node.args[0], node.args[1]
                    if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                        _add(target, attr.value)
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute):
                    _add(t.value, t.attr)
    return names


def _unqualified_binds(patched, modules):
    """Every `from <M> import <name>` in `modules` where `<name>` is patched on `<M>`.

    `<M>` is a module id, so `from bridge.config import PROM_URL` is checked against
    `patched["bridge.config"]`. A `from bridge import config` names a module, not a patched
    name, and `bridge` is never a patched module, so it passes — as it should.
    """
    problems = []
    for path in modules:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module not in patched:
                continue
            for alias in node.names:
                if alias.name in patched[node.module]:
                    problems.append(
                        f"{path.name}:{node.lineno}: `from {node.module} import "
                        f"{alias.name}` — call it as `{node.module}.{alias.name}` "
                        "instead, or the tests' monkeypatch silently misses this module"
                    )
    return problems


def test_there_are_patched_names_to_check():
    # Without this the assertion below passes vacuously if the AST walk ever stops matching.
    #
    # It named `bridge.config` alongside `bridge.common` until 2026-09-04, when monitor-bridge's
    # configuration became a frozen `Config` built in `main()` and passed down. Nothing patches
    # that module any more — which is the point of the seam, not a lapsed census — so the pair
    # that keeps this honest is now `bridge.common` and `bridge.net`, the two the suite still
    # stubs. Repoint it again rather than deleting it when the next module gets its seam.
    # A subset comparison rather than two `in` tests: `"bridge.net" in patched` reads as a
    # hostname-shaped substring check to CodeQL, which
    # ansible/tests/repo/test_no_host_shaped_membership_literal.py enforces repo-wide.
    patched = _patched_names_by_module()
    assert {"bridge.common", "bridge.net"} <= patched.keys(), sorted(patched)


def test_the_suite_census_sees_shared_helper_modules():
    # Non-vacuity for the `*.py` glob above, naming members rather than counting.
    # `_check_gate_helpers.py` holds seven of monitor-bridge's transport patches and is not
    # named `test_*`; the narrower glob that missed it read exactly as green as one that sees it.
    names = {p.name for p in _suite_files()}
    assert {"_check_gate_helpers.py", "conftest.py"} <= names, sorted(names)


def test_there_are_consumer_modules():
    # Naming the pair as well as counting: an empty census reads the same as a full one to
    # `assert modules`, and this rule exists for these two roles first.
    roots = {r.parent.name for r in _consumer_roots()}
    assert {"monitor-bridge", "autofix-bridge"} <= roots, sorted(roots)
    assert _runtime_modules()


def test_no_module_binds_a_patched_name_by_name():
    patched = _patched_names_by_module()
    problems = _unqualified_binds(patched, sorted(_runtime_modules().values()))
    assert not problems, "\n".join(problems)


def _top_level_bindings(path):
    """Every name a module binds at its top level: def, class, assignment, import."""
    bound = set()
    for node in ast.parse(path.read_text(), filename=str(path)).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bound |= {t.id for t in targets if isinstance(t, ast.Name)}
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return bound


def test_every_patched_name_exists_on_its_module():
    # A rename that leaves a stale patched name would quietly stop guarding it. Checked by AST
    # rather than by importing: autofix-bridge's files/ is not on pytest's pythonpath, and an
    # import would also run each module's env reads for nothing.
    by_id = _runtime_modules()
    missing = []
    for module, names in sorted(_patched_names_by_module().items()):
        bound = _top_level_bindings(by_id[module])
        missing += [f"{module}.{n}" for n in sorted(names) if n not in bound]
    assert not missing, f"patched names absent from their module: {missing}"


def test_checker_fires_on_a_synthesized_bad_sample(tmp_path):
    """Prove the checker can actually fail, not just pass vacuously.

    A prior /homelab-review run found five guards structurally unable to fail — the check
    existed but no input could ever make it report a problem. Synthesize the exact shape the
    rule forbids (a from-import of a patched name) and confirm `_unqualified_binds` catches it.
    """
    bad = tmp_path / "bad_consumer.py"
    bad.write_text(
        "from bridge.common import log\nfrom bridge.config import PROM_URL\n\nlog(PROM_URL)\n"
    )
    patched = {"bridge.common": {"log"}, "bridge.config": {"PROM_URL"}}
    problems = _unqualified_binds(patched, [bad])
    assert len(problems) == 2, problems
    assert "bad_consumer.py" in problems[0] and "log" in problems[0]
    assert "PROM_URL" in problems[1]

    # A qualified import of the same name must NOT be flagged — the checker fires on the
    # binding form, not on merely mentioning the name.
    good = tmp_path / "good_consumer.py"
    good.write_text(
        "import bridge.common\nimport bridge.config as cfg\n\n"
        "bridge.common.log(cfg.PROM_URL)\n"
    )
    assert not _unqualified_binds(patched, [good])

    # And the census itself: a wrapped setattr and a bare assignment both count.
    bad_test = tmp_path / "test_bad.py"
    bad_test.write_text(
        "import bridge.config\n\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(\n"
        '        bridge.config, "PROM_URL", "x"\n'
        "    )\n"
        "    bridge.config.LOKI_URL = 'y'\n"
    )
    assert _patched_names_by_module([bad_test], {"bridge.config"}) == {
        "bridge.config": {"PROM_URL", "LOKI_URL"}
    }


def test_checker_sees_a_packaged_module_in_every_spelling(tmp_path):
    """Red-proof for depth: the rule holds for `bridge.config` exactly as for `bridge.config`.

    The from-import of a patched name is flagged whether the module is flat or packaged; a
    from-import of the MODULE (`from bridge import config`) is the qualified form and passes;
    and the census resolves a test's alias back to the module id.
    """
    patched = {"bridge.config": {"PROM_URL"}}
    bad = tmp_path / "bad.py"
    bad.write_text("from bridge.config import PROM_URL\n")
    assert len(_unqualified_binds(patched, [bad])) == 1
    good = tmp_path / "good.py"
    good.write_text("from bridge import config as cfg\ncfg.PROM_URL\n")
    assert not _unqualified_binds(patched, [good])

    test = tmp_path / "test_alias.py"
    test.write_text(
        "from bridge import config as cfg\nimport bridge.io\n\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(cfg, "PROM_URL", "x")\n'
        "    bridge.io.TIMEOUT = 1\n"
    )
    assert _patched_names_by_module([test], {"bridge.config", "bridge.io"}) == {
        "bridge.config": {"PROM_URL"},
        "bridge.io": {"TIMEOUT"},
    }

    # A nested consumer tree is censused, and a role that only mentions the shared module in
    # a comment is not a consumer.
    k8s = tmp_path / "k8s"
    for rel, source in {
        "alpha/files/bridge/common.py": "def log(): pass\n",
        "alpha/files/check.py": "from bridge import common\n",
        "beta/files/sub/autofix.py": "import bridge.common as bc\n",
        "gamma/files/z.py": "# from bridge import common\n",
    }.items():
        p = k8s / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(source)
    per_role = {
        role.name: python_modules(role / "files") for role in sorted(k8s.iterdir())
    }
    assert set(per_role["alpha"]) == {"bridge.common", "check"}
    assert set(per_role["beta"]) == {"sub.autofix"}
    tree = ast.parse((k8s / "beta/files/sub/autofix.py").read_text())
    assert imported_module_ids(tree, set(per_role["alpha"])) == {"bridge.common"}
    tree = ast.parse((k8s / "gamma/files/z.py").read_text())
    assert not imported_module_ids(tree, set(per_role["alpha"]))
