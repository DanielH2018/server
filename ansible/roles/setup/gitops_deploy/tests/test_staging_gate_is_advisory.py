"""Slice 3's staging gate must be advisory, and must not be able to break a prod deploy.

Source-level guards on consult_staging()'s subprocess launches, plus the one ordering
invariant in main() whose rejecting half parses a pre-fix shape of the function.

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


def test_consult_staging_returns_no_verdict(gitops_fn) -> None:
    """A returned verdict is a verdict something can branch on.

    Advisory means there is nothing to branch on, enforced here rather than left to a reader's
    discretion.
    """
    returns = [
        node
        for node in ast.walk(gitops_fn("consult_staging"))
        if isinstance(node, ast.Return) and node.value is not None
    ]
    assert not returns, (
        "consult_staging returns a value, so a caller can gate on it — that is slice 4, and it "
        "must be a deliberate change to this test rather than a side effect."
    )


def test_the_staging_subprocesses_cannot_escape(gitops_fn) -> None:
    """Every subprocess call in the gate sits inside a try with a broad except.

    A missing script, an ssh outage or a wedged guest must not reach the prod deploy. This is
    the property that makes the gate safe to leave enabled while its false-failure rate is
    still unknown.
    """
    fn = gitops_fn("consult_staging")
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


def test_main_calls_the_gate_as_a_bare_statement_before_deploying_prod(
    gitops_fn,
) -> None:
    """Advisory by position as well as by return type.

    The call must be a bare expression — not assigned, not a condition — and it must come
    before `deploy_k8s`, since a gate consulted afterwards would have nothing left to gate.
    """
    main = gitops_fn("main")
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


def test_the_staging_scripts_run_under_the_repos_pinned_env(gitops_fn) -> None:
    """Both scripts import yaml and jinja2, so the interpreter has to carry the repo's deps.

    `sys.executable` does not, reliably: this unit's ExecStart is `uv run --no-project`, which
    never creates or syncs a venv, so `sys.executable` is whichever venv happens to sit in
    WorkingDirectory. A missing or unsynced one makes staging_expectations.py die at import
    with exit 1 — indistinguishable from a genuine expectation mismatch, so
    staging_verdict_summary reports REJECTED. Staging would then reject every gated tick for a
    reason that has nothing to do with the change, which is precisely the false-failure this
    slice is supposed to be measuring rather than manufacturing.
    """
    offenders = sys_executable_launches(gitops_fn("consult_staging"))
    assert not offenders, (
        f"consult_staging starts {offenders} with sys.executable — use _UV_PYTHON, the same "
        f"pinned env deploy_k8s runs ansible-playbook in."
    )


def test_the_pinned_env_check_rejects_a_sys_executable_launch(gitops_fn) -> None:
    """The rejecting half: the pre-fix call shape, verbatim, must fail the check above."""
    before_the_fix = ast.parse(
        "def consult_staging(services, origin):\n"
        "    subprocess.run([sys.executable, STAGING_EXPECT_SCRIPT], check=False)\n"
    )
    assert sys_executable_launches(gitops_fn("consult_staging", before_the_fix)), (
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


def test_the_staging_scripts_run_from_the_repo(gitops_fn) -> None:
    """Pinning the interpreter is not enough on its own — `uv run` resolves by cwd.

    Observed 2026-08-28: with `_UV_PYTHON` already in place, driving consult_staging from a
    scratch directory still died on `ModuleNotFoundError: No module named 'yaml'`, because uv
    found no project there and fell back to a bare interpreter. That is the same exit 1 the
    interpreter fix was written to prevent, reached by the other half of the same mechanism.
    """
    offenders = cwdless_launches(gitops_fn("consult_staging"))
    assert not offenders, (
        f"{offenders} in consult_staging inherit cwd, so `uv run` resolves whatever project "
        f"the caller was standing in. Pass cwd=REPO."
    )


def test_the_cwd_check_rejects_an_inherited_cwd(gitops_fn) -> None:
    """The rejecting half: the pre-fix call shape, verbatim, must fail the check above."""
    before_the_fix = ast.parse(
        "def consult_staging(services, origin):\n"
        "    subprocess.run([*_UV_PYTHON, STAGING_EXPECT_SCRIPT], check=False)\n"
    )
    assert cwdless_launches(gitops_fn("consult_staging", before_the_fix)), (
        "the check no longer sees a cwd-less launch, so it has stopped being a check."
    )


def children_that_cannot_time_out_first(fn: ast.FunctionDef) -> list[str]:
    """Child launches whose own --timeout is not strictly under the subprocess.run timeout.

    The shared verdict for the pair below. A child that cannot time out first never gets to
    report its own NO VERDICT: the outer subprocess.run raises TimeoutExpired, the broad except
    logs and returns, and no alert is sent.
    """
    offenders = []
    for node in ast.walk(fn):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            continue
        argv = [ast.unparse(e) for e in node.args[0].elts]
        outer = next(
            (ast.unparse(kw.value) for kw in node.keywords if kw.arg == "timeout"), None
        )
        if outer is None:
            offenders.append(f"{argv} has no outer timeout at all")
            continue
        if "'--timeout'" not in argv:
            offenders.append(f"{argv} passes no --timeout to the child")
            continue
        inner = argv[argv.index("'--timeout'") + 1]
        if outer not in inner or "_INNER_TIMEOUT_MARGIN_S" not in inner:
            offenders.append(f"child timeout {inner} is not {outer} minus the margin")
    return offenders


def test_each_staging_child_times_out_before_its_wrapper(gitops_fn) -> None:
    """A slow staging must page, not just log.

    staging_gate.py and staging_expectations.py each catch their own TimeoutExpired and return
    SSH_FAILURE, which classify() maps to NO VERDICT — so the EXISTING alert_once fires with the
    right verdict word. That only happens if the child's deadline is the one that expires. Until
    this check, consult_staging passed no --timeout at all, leaving staging_gate.py on its
    argparse default of 1800s inside a 1200s wrapper: a race the child could not win, making its
    own NO_VERDICT path dead code from its only caller.
    """
    offenders = children_that_cannot_time_out_first(gitops_fn("consult_staging"))
    assert not offenders, (
        f"a wedged staging would be logged and never alerted: {offenders}"
    )


def test_the_inner_timeout_check_rejects_a_child_with_no_deadline(gitops_fn) -> None:
    """The rejecting half: the pre-fix call shape, verbatim, must fail the check above."""
    before_the_fix = ast.parse(
        "def consult_staging(services, origin):\n"
        "    subprocess.run(\n"
        "        [*_UV_PYTHON, STAGING_GATE_SCRIPT, origin, '--tags', tags],\n"
        "        timeout=STAGING_GATE_TIMEOUT_S,\n"
        "    )\n"
    )
    offenders = children_that_cannot_time_out_first(
        gitops_fn("consult_staging", before_the_fix)
    )
    assert offenders, "the check no longer sees a child that cannot time out first"


def test_the_gate_is_off_by_default(gitops_deploy) -> None:
    """A slice that silently added wall-clock to every k8s tick on merge would be a behaviour
    change nobody opted into. The canned config sets no STAGING_GATE, so the imported module
    shows the default."""
    assert gitops_deploy.STAGING_GATE is False, (
        "STAGING_GATE no longer defaults to false — enabling the gate for every host on merge "
        "is a deliberate decision, not a default."
    )


def merge_precedes_the_gate(fn: ast.FunctionDef) -> bool:
    """Does a `git merge --ff-only` run BEFORE consult_staging() in this function?

    The verdict both halves of the red-proof below share. True is the defect: the gate blocks for up
    to STAGING_GATE_TIMEOUT_S + STAGING_EXPECT_TIMEOUT_S, so a process death inside that window used
    to leave local == origin with nothing deployed — next_action() then returns noop forever and
    both Kuma tiles stay green over a permanently stranded deploy (2026-08-29 H-2).

    Scoped to the branch that actually holds the gate: main() ff-merges on several paths (docker,
    broad, defer), and comparing across all of them would compare a merge in one branch against a
    gate in another. Only the k8s branch containing consult_staging is the one under test.
    """

    def _is_ff_merge(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run"
            and bool(node.args)
            and isinstance(node.args[0], ast.List)
            and {"merge", "--ff-only"}
            <= {e.value for e in node.args[0].elts if isinstance(e, ast.Constant)}
        )

    def _calls_gate(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "consult_staging"
        )

    for block in ast.walk(fn):
        if not isinstance(block, ast.If):
            continue
        gate_lines = [n.lineno for n in ast.walk(block) if _calls_gate(n)]
        if not gate_lines:
            continue
        merge_lines = [n.lineno for n in ast.walk(block) if _is_ff_merge(n)]
        if merge_lines and min(merge_lines) < min(gate_lines):
            return True
    return False


def test_the_gate_is_consulted_before_the_ff_merge(gitops_fn) -> None:
    """The accepting half: main() consults the gate, THEN merges."""
    assert not merge_precedes_the_gate(gitops_fn("main")), (
        "a `git merge --ff-only` runs before consult_staging() in main() — a death in the gate "
        "window then strands the promoted SHA as a permanent noop with every monitor green"
    )


def test_a_merge_before_the_gate_is_flagged(gitops_fn) -> None:
    """The rejecting half: the pre-fix ordering must come back True, or the check above is inert."""
    before_the_fix = ast.parse(
        "def main():\n"
        "    if cs.k8s_deploy:\n"
        "        run(['git', 'merge', '--ff-only', origin])\n"
        "        consult_staging(cs.k8s_deploy, origin)\n"
        "        deploy_k8s(cs.k8s_deploy, K8S_DEPLOY_TIMEOUT_S)\n"
    )
    assert merge_precedes_the_gate(gitops_fn("main", before_the_fix)), (
        "the check no longer sees a merge that precedes the gate"
    )
