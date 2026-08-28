"""Slice 3's staging gate must be advisory, and must not be able to break a prod deploy.

Source-level guards, matching test_gitops_discord_contract.py: gitops_deploy.py cannot be
imported in CI (module-level `C = cfg()` reads /etc config that does not exist there — the
accepted design, see the role CLAUDE.md), so these invariants are asserted against the AST.

WHY THESE THREE. Phase C's whole sequencing rests on slice 3 collecting a false-failure rate
BEFORE anything depends on the answer (docs/staging-phase-c.md). The slice is also the one most
likely to be quietly skipped, because once it works, enforcing is one flag away. So the
advisory property is pinned by a check rather than by intent:

  1. `consult_staging` returns no verdict, so no caller can branch on one by accident.
  2. Its subprocess work is inside a broad `except`, so a wedged guest or a missing script
     cannot propagate into the prod deploy.
  3. `main()` calls it as a bare statement before `deploy_k8s`, never in a condition.

If a later slice makes the gate blocking, these tests are the ones to change deliberately —
which is the point.
"""

from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).with_name("gitops_deploy.py")
_TREE = ast.parse(_SRC.read_text())


def _fn(name: str) -> ast.FunctionDef:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() is gone from gitops_deploy.py")


def test_consult_staging_returns_no_verdict() -> None:
    """A returned verdict is a verdict something can branch on. Advisory means there is nothing
    to branch on, enforced here rather than left to a reader's discretion."""
    returns = [
        node
        for node in ast.walk(_fn("consult_staging"))
        if isinstance(node, ast.Return) and node.value is not None
    ]
    assert not returns, (
        "consult_staging returns a value, so a caller can gate on it — that is slice 4, and it "
        "must be a deliberate change to this test rather than a side effect."
    )


def test_the_staging_subprocesses_cannot_escape() -> None:
    """Every subprocess call in the gate sits inside a try with a broad except.

    A missing script, an ssh outage or a wedged guest must not reach the prod deploy. This is
    the property that makes the gate safe to leave enabled while its false-failure rate is
    still unknown.
    """
    fn = _fn("consult_staging")
    guarded = set()
    for handler_parent in ast.walk(fn):
        if not isinstance(handler_parent, ast.Try):
            continue
        broad = any(
            h.type is None
            or (
                isinstance(h.type, ast.Name)
                and h.type.id in {"Exception", "BaseException"}
            )
            for h in handler_parent.handlers
        )
        if broad:
            for node in ast.walk(handler_parent):
                guarded.add(id(node))

    calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
    ]
    assert calls, "consult_staging no longer runs the staging scripts at all"
    unguarded = [c for c in calls if id(c) not in guarded]
    assert not unguarded, (
        f"{len(unguarded)} subprocess call(s) in consult_staging sit outside a broad except, "
        f"so a staging outage could fail the prod deploy that follows."
    )


def test_main_calls_the_gate_as_a_bare_statement_before_deploying_prod() -> None:
    """Advisory by position as well as by return type.

    The call must be a bare expression — not assigned, not a condition — and it must come
    before `deploy_k8s`, since a gate consulted afterwards would have nothing left to gate.
    """
    main = _fn("main")
    stmts = list(ast.walk(main))

    bare_calls = [
        node
        for node in stmts
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "consult_staging"
    ]
    assert bare_calls, (
        "main() does not call consult_staging as a bare statement — either it stopped calling "
        "it, or it is now using the result, which would make the gate blocking."
    )

    def _line_of(pred) -> int:
        return min(n.lineno for n in stmts if pred(n))

    gate_line = min(node.lineno for node in bare_calls)
    deploy_line = _line_of(
        lambda n: (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "deploy_k8s"
        )
    )
    assert gate_line < deploy_line, (
        f"consult_staging is called at line {gate_line}, after deploy_k8s at {deploy_line} — "
        f"a gate consulted after the deploy gates nothing."
    )


def test_the_gate_is_off_by_default() -> None:
    """A slice that silently added wall-clock to every k8s tick on merge would be a behaviour
    change nobody opted into. The default lives in the source, so it is checkable here."""
    src = _SRC.read_text()
    assert 'C.get("STAGING_GATE", "false")' in src, (
        "STAGING_GATE no longer defaults to false — enabling the gate for every host on merge "
        "is a deliberate decision, not a default."
    )
