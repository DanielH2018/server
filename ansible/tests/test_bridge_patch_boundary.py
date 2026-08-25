"""bridge_common's patched helpers must be reached qualified, not from-imported.

Precedent: `scripts/probe/test_probe_boundaries.py` solves the identical problem for the probe
family, and this guard is a close port of it onto monitor-bridge + autofix-bridge.
`monkeypatch.setattr(bridge_common, "log", ...)` rebinds the attribute on the bridge_common
module object. A caller that did `from bridge_common import log` holds its own reference in
its globals, taken at import time, and that reference never sees the patch — the test passes
while patching nothing.

The failure is silent in the direction that matters: a test believing it silenced `log`
would really print to stdout (harmless, but proves the patch did nothing), and a helper that
one day reads real state through an unqualified name would run that real state instead of the
stubbed one. `bridge_common.py`'s module docstring records the qualified-access rule this
enforces; this is the check that keeps it honest.

Run: uv run pytest ansible/tests/test_bridge_patch_boundary.py
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


def _test_and_conftest_files():
    """Every test module + conftest.py under a role that imports bridge_common — the suites
    this rule binds. Every consumer's non-test modules are checked against the union of what
    any of those suites patches, since bridge_common is shared between them."""
    files = []
    for root in _consumer_roots():
        files += sorted(root.glob("test_*.py"))
        conftest = root / "conftest.py"
        if conftest.exists():
            files.append(conftest)
    return files


def _consumer_modules():
    """Every non-test module under either role's files/ that imports bridge_common.

    Not a `check.py`/`autofix.py` name check — bridge_parsing.py and verdicts_service.py also
    sit in files/ and either could gain a bridge_common import later. The rule is about who
    imports it, so that is what this matches, same reasoning as probe_boundaries' postflight.py
    note.
    """
    found = []
    for base in _consumer_roots():
        for path in sorted(base.glob("*.py")):
            if path.name in ("bridge_common.py",) or path.name.startswith("test_"):
                continue
            if path.name == "conftest.py":
                continue
            if "bridge_common" in path.read_text():
                found.append(path)
    return found


def _patched_bridge_common_names():
    """Every attribute of `bridge_common` either suite assigns, patches, or mutates in place.

    AST walk, not a regex — a line-oriented regex over `monkeypatch.setattr(bridge_common, "X"`
    misses the wrapped form ruff format produces and misses plain `bridge_common.X = ...`
    assignment entirely. `ansible/tests/test_monitor_bridge_modules.py`'s `_patched_names()`
    hit exactly that hole when first measured; this mirrors its AST shape rather than repeating
    the mistake.
    """
    names = set()
    for path in _test_and_conftest_files():
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
                        and target.id == "bridge_common"
                        and isinstance(attr, ast.Constant)
                        and isinstance(attr.value, str)
                    ):
                        names.add(attr.value)
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if (
                    isinstance(t, ast.Attribute)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "bridge_common"
                ):
                    names.add(t.attr)
    return names


def _unqualified_binds(patched, modules):
    """Every `from bridge_common import <name>` in `modules` where `<name>` is in `patched`."""
    problems = []
    for path in modules:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "bridge_common":
                continue
            for alias in node.names:
                if alias.name in patched:
                    problems.append(
                        f"{path.name}:{node.lineno}: `from bridge_common import "
                        f"{alias.name}` — call it as `bridge_common.{alias.name}(...)` "
                        "instead, or the tests' monkeypatch silently misses this module"
                    )
    return problems


def test_there_are_patched_names_to_check():
    # Without this the assertion below passes vacuously if the AST walk ever stops matching.
    assert _patched_bridge_common_names(), (
        "no bridge_common attribute is patched by either suite — "
        "check _patched_bridge_common_names() against the live test files"
    )


def test_there_are_consumer_modules():
    assert _consumer_modules(), (
        f"no bridge_common consumers found under {[str(r) for r in _consumer_roots()]}"
    )


def test_no_module_binds_a_patched_bridge_common_name_by_name():
    patched = _patched_bridge_common_names()
    problems = _unqualified_binds(patched, _consumer_modules())
    assert not problems, "\n".join(problems)


def test_every_patched_name_exists_on_bridge_common():
    # A rename that leaves a stale patched name would quietly stop guarding it.
    import bridge_common

    patched = _patched_bridge_common_names()
    missing = sorted(n for n in patched if not hasattr(bridge_common, n))
    assert not missing, f"patched names absent from bridge_common: {missing}"


def test_checker_fires_on_a_synthesized_bad_sample(tmp_path):
    """Prove the checker can actually fail, not just pass vacuously.

    A prior /homelab-review run found five guards structurally unable to fail — the check
    existed but no input could ever make it report a problem. Synthesize the exact shape the
    rule forbids (a from-import of a patched name) and confirm `_unqualified_binds` catches it.
    """
    bad = tmp_path / "bad_consumer.py"
    bad.write_text("from bridge_common import log\n\nlog('hi')\n")
    problems = _unqualified_binds({"log"}, [bad])
    assert problems, (
        "checker did not fire on a synthesized `from bridge_common import log`"
    )
    assert "bad_consumer.py" in problems[0]
    assert "log" in problems[0]

    # A qualified import of the same name must NOT be flagged — the checker fires on the
    # binding form, not on merely mentioning the name.
    good = tmp_path / "good_consumer.py"
    good.write_text("import bridge_common\n\nbridge_common.log('hi')\n")
    assert not _unqualified_binds({"log"}, [good])
