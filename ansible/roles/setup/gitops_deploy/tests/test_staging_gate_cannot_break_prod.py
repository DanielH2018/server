"""The staging gate must not be able to break a prod deploy, blocking or not.

Source-level guards on consult_staging()'s subprocess launches, plus the ordering invariants in
main(). Each rejecting half parses a pre-fix shape of the function, so a check that stopped
matching fails rather than passing vacuously.

WHAT CHANGED AT SLICE 4. Three of these tests pinned the gate as ADVISORY: consult_staging
returned nothing, and main() called it as a bare statement. Slice 4 made it return a verdict and
made main() branch on it, so those three were rewritten here deliberately — which is what the
slice-3 versions asked for. What did NOT change is the property that matters in both modes:
a wedged guest, a missing script or a bug in the gate must never reach the prod deploy. That is
now carried by the broad `except` PLUS `staging_blocks`, which blocks on a rejection and nothing
else; test_staging_blocking.py owns the second half.

Still pinned here:

  1. Its subprocess work is inside a broad `except`, so a wedged guest or a missing script
     cannot propagate into the prod deploy.
  2. `main()` reads the verdict through `staging_blocks` rather than testing it inline, so the
     NO_VERDICT decision lives in one checked place.
  3. `main()` consults the gate before `deploy_k8s`, and before the ff-merge.
"""

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


def test_consult_staging_returns_a_verdict_on_every_path(handlers_fn) -> None:
    """Every exit from the gate hands back a word, including the broad `except`.

    A bare `return` there would give main() None, which `staging_blocks` reads as "does not
    block" — correct by luck rather than by construction, and silent: the same path also has to
    reach the alert. It returned bare until slice 4, which was harmless only while nothing
    branched on the answer.
    """
    fn = handlers_fn("consult_staging")
    bare = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Return) and node.value is None
    ]
    assert not bare, (
        "consult_staging has a bare `return`, so one path hands main() None instead of a "
        "verdict — and skips the alert that makes a non-PASS visible."
    )


def test_the_staging_subprocesses_cannot_escape(deploy_io_fn) -> None:
    """Every subprocess call in the gate sits inside a try with a broad except.

    A missing script, an ssh outage or a wedged guest must not reach the prod deploy. This is
    the property that makes the gate safe to leave enabled while its false-failure rate is
    still unknown. The calls live in `deploy_io.run_staging_scripts`; `consult_staging` above
    keeps the guard that every path still returns a word.
    """
    fn = deploy_io_fn("run_staging_scripts")
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
    assert calls, "run_staging_scripts no longer runs the staging scripts at all"
    unguarded = [c for c in calls if id(c) not in guarded]
    assert not unguarded, (
        f"{len(unguarded)} subprocess call(s) in run_staging_scripts sit outside a broad except, "
        f"so a staging outage could fail the prod deploy that follows."
    )


def test_the_k8s_phase_decides_through_staging_blocks_rather_than_inline(
    handlers_fn,
) -> None:
    """The verdict must be routed through the checked decision, not compared in main().

    `staging_blocks` is where the NO_VERDICT decision lives and where test_staging_blocking.py
    can reach it. An inline `if verdict == "rejected"` in main() would be the same behaviour
    today and unreviewable the next time someone widens what blocks.
    """
    main = handlers_fn("handle_k8s")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "staging_blocks"
        for node in ast.walk(main)
    ), (
        "handle_k8s() does not call staging_blocks — the blocking decision left its check"
    )
    inline = [
        ast.unparse(node)
        for node in ast.walk(main)
        if isinstance(node, ast.Compare)
        and any(
            isinstance(c, ast.Constant)
            and c.value in {"rejected", "no_verdict", "pass"}
            for c in node.comparators
        )
    ]
    assert not inline, f"handle_k8s() compares a staging verdict inline: {inline}"


def test_the_k8s_phase_consults_the_gate_before_deploying_prod(handlers_fn) -> None:
    """A gate consulted after the deploy gates nothing."""
    main = handlers_fn("handle_k8s")
    stmts = list(ast.walk(main))

    gate_calls = [
        node
        for node in stmts
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "consult_staging"
    ]
    assert gate_calls, "handle_k8s() no longer calls consult_staging at all"

    def _line_of(pred) -> int:
        return min(n.lineno for n in stmts if pred(n))

    gate_line = min(node.lineno for node in gate_calls)
    # Either spelling: `deploy_k8s(...)` while it lived here, `deploy_io.deploy_k8s(...)` now.
    deploy_line = _line_of(
        lambda n: (
            isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Name) and n.func.id == "deploy_k8s")
                or (isinstance(n.func, ast.Attribute) and n.func.attr == "deploy_k8s")
            )
        )
    )
    assert gate_line < deploy_line, (
        f"consult_staging is called at line {gate_line}, after deploy_k8s at {deploy_line} — "
        f"a gate consulted after the deploy gates nothing."
    )


def test_the_staging_scripts_run_under_the_repos_pinned_env(deploy_io_fn) -> None:
    """Both scripts import yaml and jinja2, so the interpreter has to carry the repo's deps.

    `sys.executable` does not, reliably: this unit's ExecStart is `uv run --no-project`, which
    never creates or syncs a venv, so `sys.executable` is whichever venv happens to sit in
    WorkingDirectory. A missing or unsynced one makes staging_expectations.py die at import
    with exit 1 — indistinguishable from a genuine expectation mismatch, so
    staging_verdict_summary reports REJECTED. Staging would then reject every gated tick for a
    reason that has nothing to do with the change, which is precisely the false-failure this
    slice is supposed to be measuring rather than manufacturing.
    """
    offenders = sys_executable_launches(deploy_io_fn("run_staging_scripts"))
    assert not offenders, (
        f"consult_staging starts {offenders} with sys.executable — use _UV_PYTHON, the same "
        f"pinned env deploy_k8s runs ansible-playbook in."
    )


def test_the_pinned_env_check_rejects_a_sys_executable_launch(deploy_io_fn) -> None:
    """The rejecting half: the pre-fix call shape, verbatim, must fail the check above."""
    before_the_fix = ast.parse(
        "def run_staging_scripts(repo, sha, tags, gate_timeout_s, expect_timeout_s):\n"
        "    subprocess.run([sys.executable, STAGING_EXPECT_SCRIPT], check=False)\n"
    )
    assert sys_executable_launches(
        deploy_io_fn("run_staging_scripts", before_the_fix)
    ), (
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


def test_the_staging_scripts_run_from_the_repo(deploy_io_fn) -> None:
    """Pinning the interpreter is not enough on its own — `uv run` resolves by cwd.

    Observed 2026-08-28: with `_UV_PYTHON` already in place, driving consult_staging from a
    scratch directory still died on `ModuleNotFoundError: No module named 'yaml'`, because uv
    found no project there and fell back to a bare interpreter. That is the same exit 1 the
    interpreter fix was written to prevent, reached by the other half of the same mechanism.
    """
    offenders = cwdless_launches(deploy_io_fn("run_staging_scripts"))
    assert not offenders, (
        f"{offenders} in consult_staging inherit cwd, so `uv run` resolves whatever project "
        f"the caller was standing in. Pass cwd=REPO."
    )


def test_the_cwd_check_rejects_an_inherited_cwd(deploy_io_fn) -> None:
    """The rejecting half: the pre-fix call shape, verbatim, must fail the check above."""
    before_the_fix = ast.parse(
        "def run_staging_scripts(repo, sha, tags, gate_timeout_s, expect_timeout_s):\n"
        "    subprocess.run([*_UV_PYTHON, STAGING_EXPECT_SCRIPT], check=False)\n"
    )
    assert cwdless_launches(deploy_io_fn("run_staging_scripts", before_the_fix)), (
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
        if outer not in inner or "INNER_TIMEOUT_MARGIN_S" not in inner:
            offenders.append(f"child timeout {inner} is not {outer} minus the margin")
    return offenders


def test_each_staging_child_times_out_before_its_wrapper(deploy_io_fn) -> None:
    """A slow staging must page, not just log.

    staging_gate.py and staging_expectations.py each catch their own TimeoutExpired and return
    SSH_FAILURE, which classify() maps to NO VERDICT — so the EXISTING alert_once fires with the
    right verdict word. That only happens if the child's deadline is the one that expires. Until
    this check, consult_staging passed no --timeout at all, leaving staging_gate.py on its
    argparse default of 1800s inside a 1200s wrapper: a race the child could not win, making its
    own NO_VERDICT path dead code from its only caller.
    """
    offenders = children_that_cannot_time_out_first(deploy_io_fn("run_staging_scripts"))
    assert not offenders, (
        f"a wedged staging would be logged and never alerted: {offenders}"
    )


def test_the_inner_timeout_check_rejects_a_child_with_no_deadline(deploy_io_fn) -> None:
    """The rejecting half: the pre-fix call shape, verbatim, must fail the check above."""
    before_the_fix = ast.parse(
        "def run_staging_scripts(repo, sha, tags, gate_timeout_s, expect_timeout_s):\n"
        "    subprocess.run(\n"
        "        [*_UV_PYTHON, STAGING_GATE_SCRIPT, origin, '--tags', tags],\n"
        "        timeout=STAGING_GATE_TIMEOUT_S,\n"
        "    )\n"
    )
    offenders = children_that_cannot_time_out_first(
        deploy_io_fn("run_staging_scripts", before_the_fix)
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


def test_blocking_is_off_by_default(gitops_deploy) -> None:
    """The switch that decides whether a rejection stops prod must default to advisory.

    Its sibling above covers STAGING_GATE. This one is the more consequential default: a host
    that merged slice 4 without opting in must not start refusing deploys, and the entry
    condition in docs/staging-phase-c.md is what the opt-in waits on. Asserted directly, so a
    flipped default fails with a message about the default rather than about a missing merge.
    """
    assert gitops_deploy.STAGING_GATE_BLOCKING is False, (
        "STAGING_GATE_BLOCKING no longer defaults to false — a staging rejection would start "
        "blocking prod deploys on merge, which is a decision the entry condition gates."
    )


def merge_precedes_the_gate(fn: ast.FunctionDef) -> bool:
    """Does a `git merge --ff-only` run BEFORE consult_staging() in this function?

    The verdict all three halves of the red-proof below share. True is the defect: the gate blocks
    for up to STAGING_GATE_TIMEOUT_S + STAGING_EXPECT_TIMEOUT_S, so a process death inside that
    window used to leave local == origin with nothing deployed — next_action() then returns noop
    forever and both Kuma tiles stay green over a permanently stranded deploy (2026-08-29 H-2).

    Scoped to the code that actually holds the gate. While the whole tick was one `main()`, that
    meant the enclosing `if cs.k8s_deploy:` — main() ff-merges on several paths (docker, broad,
    defer) and comparing across all of them would compare a merge in one branch against a gate in
    another. `handle_k8s()` IS that branch, so its own body is the scope, and an `ast.If`-only
    walk would find no block and return False for every input. Both scopes are checked: an `If`
    holding the gate when there is one, the function body otherwise.
    """

    def _is_ff_merge(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            # `run(...)` while it lived in main(), `deploy_io.run(...)` from a phase function.
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "run")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "run")
            )
            and bool(node.args)
            and isinstance(node.args[0], ast.List)
            and {"merge", "--ff-only"}
            <= {e.value for e in node.args[0].elts if isinstance(e, ast.Constant)}
        )

    def _calls_gate(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id == "consult_staging")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "consult_staging"
            )
        )

    def _out_of_order(scope: ast.AST) -> bool:
        gate_lines = [n.lineno for n in ast.walk(scope) if _calls_gate(n)]
        if not gate_lines:
            return False
        merge_lines = [n.lineno for n in ast.walk(scope) if _is_ff_merge(n)]
        return bool(merge_lines) and min(merge_lines) < min(gate_lines)

    blocks = [b for b in ast.walk(fn) if isinstance(b, ast.If)]
    if any(_calls_gate(n) for b in blocks for n in ast.walk(b)):
        return any(_out_of_order(b) for b in blocks)
    return _out_of_order(fn)


def test_the_gate_is_consulted_before_the_ff_merge(handlers_fn) -> None:
    """The accepting half: handle_k8s() consults the gate, THEN merges."""
    assert not merge_precedes_the_gate(handlers_fn("handle_k8s")), (
        "a `git merge --ff-only` runs before consult_staging() in handle_k8s() — a death in the "
        "gate window then strands the promoted SHA as a permanent noop with every monitor green"
    )


def test_the_check_is_not_vacuous_on_the_live_function(handlers_fn) -> None:
    """Non-vacuity. `merge_precedes_the_gate` returns False both for correct ordering and for a
    function it found neither call in, so the accepting half above passes either way — which is
    exactly what happened when the k8s branch moved out of main() into its own function and the
    walk stopped finding an enclosing `ast.If`."""
    body = handlers_fn("handle_k8s")
    calls = [n for n in ast.walk(body) if isinstance(n, ast.Call)]
    merges = [
        n
        for n in calls
        if isinstance(n.func, ast.Attribute)
        and n.func.attr == "run"
        and n.args
        and isinstance(n.args[0], ast.List)
        and "--ff-only"
        in {e.value for e in n.args[0].elts if isinstance(e, ast.Constant)}
    ]
    gates = [
        n
        for n in calls
        if isinstance(n.func, ast.Name) and n.func.id == "consult_staging"
    ]
    assert merges and gates, (
        "handle_k8s() no longer holds both a `git merge --ff-only` and a consult_staging call, "
        "so the ordering check above has nothing to compare and passes on nothing"
    )


def test_a_merge_before_the_gate_is_flagged(gitops_fn) -> None:
    """The rejecting half, in the shape the code had before the split: the branch inside main()."""
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


def test_a_merge_before_the_gate_is_flagged_in_a_phase_function(handlers_fn) -> None:
    """The rejecting half in TODAY's shape: no enclosing `if`, so only the body-scope walk sees it.

    Without this the guard could go back to an `ast.If`-only walk and still pass both halves —
    the one above supplies its own `if`, so it cannot tell the two implementations apart.
    """
    before_the_fix = ast.parse(
        "def handle_k8s(target, plan):\n"
        "    deploy_io.run(['git', 'merge', '--ff-only', origin], cwd=REPO)\n"
        "    verdict = consult_staging(cs.k8s_deploy, origin)\n"
        "    deploy_io.deploy_k8s(REPO, cs.k8s_deploy, K8S_DEPLOY_TIMEOUT_S)\n"
    )
    assert merge_precedes_the_gate(handlers_fn("handle_k8s", before_the_fix)), (
        "the check no longer sees a merge that precedes the gate in a phase function"
    )
