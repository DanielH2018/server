"""A patched helper must be reached qualified, never from-imported.

Precedent: `scripts/diagnostics/tests/test_probe_boundaries.py` solves the identical problem for the probe
family, and this guard is a close port of it onto monitor-bridge + autofix-bridge.
`monkeypatch.setattr(bridge_config, "PROM_URL", ...)` rebinds the attribute on the
bridge_config module object. A caller that did `from bridge_config import PROM_URL` holds its
own reference in its globals, taken at import time, and that reference never sees the patch —
the test passes while patching nothing.

The failure is silent in the direction that matters: a test believing it silenced `log`
would really print to stdout (harmless, but proves the patch did nothing), and a check that
reads a stubbed threshold through an unqualified name would run the real value instead of the
stubbed one. Every runtime module's header records the qualified-access rule this enforces;
this is the check that keeps it honest.

The rule began as bridge_common-only. It widened to every runtime module when check.py split
by domain: the tests now patch `bridge_config.X` and `bridge_io._get_json` rather than
`check.X`, so a from-import of any patched name from any module is the same defect.

Run: uv run pytest ansible/tests/services/test_bridge_patch_boundary.py
"""

import ast

from _helpers import REPO

K8S = REPO / "ansible" / "roles" / "k8s"
MONITOR_FILES = K8S / "monitor-bridge" / "files"


def _consumer_roots():
    """Every k8s role's files/ dir that imports bridge_common, monitor-bridge included.

    Derived, not the hardcoded (monitor-bridge, autofix-bridge) pair this was written for.
    That pair is the scope of the fix it shipped with, so a third role importing
    bridge_common would join the rule silently unchecked -- the guard-scope shape the same
    review found in four other places (2026-08-25 review M-2). The deployer derives its own
    consumer set the same way, in deploy_logic.shared_module_consumers.
    """
    roots = []
    for role in sorted(K8S.iterdir()):
        files = role / "files"
        if not files.is_dir():
            continue
        if any(
            "bridge_common" in p.read_text(errors="ignore")
            for p in files.glob("*.py")
            if p.name != "bridge_common.py"
        ):
            roots.append(files)
    return roots


def _is_test_file(path):
    return path.name.startswith("test_") or path.name == "conftest.py"


def _test_and_conftest_files():
    """Every test module + conftest.py under a role that imports bridge_common — the suites
    this rule binds. Every consumer's non-test modules are checked against the union of what
    any of those suites patches, since bridge_common is shared between them.

    Tests live in `tests/`, a sibling of the `files/` roots `_consumer_roots()` returns, not
    inside `files/` itself.
    """
    files = []
    for root in _consumer_roots():
        tests_dir = root.parent / "tests"
        if not tests_dir.is_dir():
            continue
        files += sorted(tests_dir.glob("test_*.py"))
        conftest = tests_dir / "conftest.py"
        if conftest.exists():
            files.append(conftest)
    return files


def _runtime_modules():
    """Every non-test module under a consumer role's files/ — the modules a test can patch
    and the modules that may from-import a patched name."""
    found = []
    for base in _consumer_roots():
        found += sorted(p for p in base.glob("*.py") if not _is_test_file(p))
    return found


def _patched_names_by_module(test_files=None, module_names=None):
    """{module: {name}} for every attribute of a runtime module either suite assigns, patches,
    or mutates in place.

    AST walk, not a regex — a line-oriented regex over `monkeypatch.setattr(bridge_common, "X"`
    misses the wrapped form ruff format produces and misses plain `bridge_common.X = ...`
    assignment entirely. `ansible/tests/services/test_monitor_bridge_modules.py`'s census hit exactly
    that hole when first measured; this mirrors its AST shape rather than repeating the mistake.
    """
    test_files = _test_and_conftest_files() if test_files is None else test_files
    if module_names is None:
        module_names = {p.stem for p in _runtime_modules()}
    names = {}

    def _add(module, name):
        if module in module_names:
            names.setdefault(module, set()).add(name)

    for path in test_files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                is_setattr = (
                    isinstance(fn, ast.Attribute) and fn.attr == "setattr"
                ) or (isinstance(fn, ast.Name) and fn.id == "setattr")
                if is_setattr and len(node.args) >= 2:
                    target, attr = node.args[0], node.args[1]
                    if (
                        isinstance(target, ast.Name)
                        and isinstance(attr, ast.Constant)
                        and isinstance(attr.value, str)
                    ):
                        _add(target.id, attr.value)
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    _add(t.value.id, t.attr)
    return names


def _unqualified_binds(patched, modules):
    """Every `from <M> import <name>` in `modules` where `<name>` is patched on `<M>`."""
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
    patched = _patched_names_by_module()
    assert "bridge_common" in patched and "bridge_config" in patched, sorted(patched)


def test_there_are_consumer_modules():
    assert _runtime_modules(), (
        f"no runtime modules found under {[str(r) for r in _consumer_roots()]}"
    )


def test_no_module_binds_a_patched_name_by_name():
    patched = _patched_names_by_module()
    problems = _unqualified_binds(patched, _runtime_modules())
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
    by_stem = {p.stem: p for p in _runtime_modules()}
    missing = []
    for module, names in sorted(_patched_names_by_module().items()):
        bound = _top_level_bindings(by_stem[module])
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
        "from bridge_common import log\nfrom bridge_config import PROM_URL\n\nlog(PROM_URL)\n"
    )
    patched = {"bridge_common": {"log"}, "bridge_config": {"PROM_URL"}}
    problems = _unqualified_binds(patched, [bad])
    assert len(problems) == 2, problems
    assert "bad_consumer.py" in problems[0] and "log" in problems[0]
    assert "PROM_URL" in problems[1]

    # A qualified import of the same name must NOT be flagged — the checker fires on the
    # binding form, not on merely mentioning the name.
    good = tmp_path / "good_consumer.py"
    good.write_text(
        "import bridge_common\nimport bridge_config as cfg\n\n"
        "bridge_common.log(cfg.PROM_URL)\n"
    )
    assert not _unqualified_binds(patched, [good])

    # And the census itself: a wrapped setattr and a bare assignment both count.
    bad_test = tmp_path / "test_bad.py"
    bad_test.write_text(
        "import bridge_config\n\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(\n"
        '        bridge_config, "PROM_URL", "x"\n'
        "    )\n"
        "    bridge_config.LOKI_URL = 'y'\n"
    )
    assert _patched_names_by_module([bad_test], {"bridge_config"}) == {
        "bridge_config": {"PROM_URL", "LOKI_URL"}
    }
