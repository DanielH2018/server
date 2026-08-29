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


def _fn(name: str, tree: ast.AST = _TREE) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() is gone from gitops_deploy.py")


def sys_executable_launches(fn: ast.FunctionDef) -> list[str]:
    """The staging scripts this function starts with `sys.executable`.

    The verdict both halves of the red-proof below share. Under this unit's
    `uv run --no-project` ExecStart, `sys.executable` is whatever venv sits in
    WorkingDirectory rather than the repo's pinned env — see _UV_PYTHON in gitops_deploy.py.
    """
    launched = []
    for node in ast.walk(fn):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and node.args
            and isinstance(node.args[0], ast.List)
            and node.args[0].elts
        ):
            continue
        head = node.args[0].elts[0]
        if (
            isinstance(head, ast.Attribute)
            and head.attr == "executable"
            and isinstance(head.value, ast.Name)
            and head.value.id == "sys"
        ):
            launched.append(ast.unparse(node.args[0]))
    return launched


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


def test_the_staging_scripts_run_under_the_repos_pinned_env() -> None:
    """Both scripts import yaml and jinja2, so the interpreter has to carry the repo's deps.

    `sys.executable` does not, reliably: this unit's ExecStart is `uv run --no-project`, which
    never creates or syncs a venv, so `sys.executable` is whichever venv happens to sit in
    WorkingDirectory. A missing or unsynced one makes staging_expectations.py die at import
    with exit 1 — indistinguishable from a genuine expectation mismatch, so
    staging_verdict_summary reports REJECTED. Staging would then reject every gated tick for a
    reason that has nothing to do with the change, which is precisely the false-failure this
    slice is supposed to be measuring rather than manufacturing.
    """
    offenders = sys_executable_launches(_fn("consult_staging"))
    assert not offenders, (
        f"consult_staging starts {offenders} with sys.executable — use _UV_PYTHON, the same "
        f"pinned env deploy_k8s runs ansible-playbook in."
    )


def test_the_pinned_env_check_rejects_a_sys_executable_launch() -> None:
    """The rejecting half: the pre-fix call shape, verbatim, must fail the check above."""
    before_the_fix = ast.parse(
        "def consult_staging(services, origin):\n"
        "    subprocess.run([sys.executable, STAGING_EXPECT_SCRIPT], check=False)\n"
    )
    assert sys_executable_launches(_fn("consult_staging", before_the_fix)), (
        "the check no longer sees a sys.executable launch, so it would pass whatever "
        "consult_staging does — it has stopped being a check."
    )


def cwdless_launches(fn: ast.FunctionDef) -> list[str]:
    """The subprocess calls in this function that do not pin cwd.

    `uv run` picks its project from the working directory, so a call that inherits cwd resolves
    the repo's env only while the caller happens to be standing in it.
    """
    return [
        ast.unparse(node.func)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and not any(kw.arg == "cwd" for kw in node.keywords)
    ]


def test_the_staging_scripts_run_from_the_repo() -> None:
    """Pinning the interpreter is not enough on its own — `uv run` resolves by cwd.

    Observed 2026-08-28: with `_UV_PYTHON` already in place, driving consult_staging from a
    scratch directory still died on `ModuleNotFoundError: No module named 'yaml'`, because uv
    found no project there and fell back to a bare interpreter. That is the same exit 1 the
    interpreter fix was written to prevent, reached by the other half of the same mechanism.
    """
    offenders = cwdless_launches(_fn("consult_staging"))
    assert not offenders, (
        f"{offenders} in consult_staging inherit cwd, so `uv run` resolves whatever project "
        f"the caller was standing in. Pass cwd=REPO."
    )


def test_the_cwd_check_rejects_an_inherited_cwd() -> None:
    """The rejecting half: the pre-fix call shape, verbatim, must fail the check above."""
    before_the_fix = ast.parse(
        "def consult_staging(services, origin):\n"
        "    subprocess.run([*_UV_PYTHON, STAGING_EXPECT_SCRIPT], check=False)\n"
    )
    assert cwdless_launches(_fn("consult_staging", before_the_fix)), (
        "the check no longer sees a cwd-less launch, so it has stopped being a check."
    )


def test_the_gate_is_off_by_default() -> None:
    """A slice that silently added wall-clock to every k8s tick on merge would be a behaviour
    change nobody opted into. The default lives in the source, so it is checkable here."""
    src = _SRC.read_text()
    assert 'C.get("STAGING_GATE", "false")' in src, (
        "STAGING_GATE no longer defaults to false — enabling the gate for every host on merge "
        "is a deliberate decision, not a default."
    )
