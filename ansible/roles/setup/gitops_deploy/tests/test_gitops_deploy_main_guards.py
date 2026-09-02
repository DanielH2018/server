"""Source-level guards on main()'s ordering and targets.

main() shells out to git, queries GitHub over HTTP, posts to Discord and touches state files
under /var/lib/gitops-deploy, so the invariants that only live in main() are pinned at the
AST rather than by running it: every ff-merge lands the pinned `origin` SHA, write_hold
precedes every rollback reset, each rollback path returns on whether its post was delivered,
the diverged marker is written every tick ahead of the action branching, drain_pending()
runs ahead of every short-circuit return, and the k8s auto-deploy branch still reads
cs.secrets. The functions around main() are importable and tested by calling them:
entrypoint() in _fetch_skip, deliver()/drain_pending()/discord() in _alert_delivery.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_main_guards.py

import ast


def _git_merge_calls(fn: ast.FunctionDef) -> list[ast.Call]:
    """Every `run([... "git", "merge", "--ff-only", <target>])` call inside `fn`."""
    out = []
    for node in ast.walk(fn):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            continue
        consts = {e.value for e in node.args[0].elts if isinstance(e, ast.Constant)}
        if {"merge", "--ff-only"} <= consts:
            out.append(node)
    return out


def test_every_ff_merge_targets_the_pinned_sha_not_the_ref(gitops_fn):
    # 2026-08-22 review H1. main() pins the remote head ONCE (`origin = git rev-parse
    # origin/<branch>`) and gates on that pin: the CI verdict, the changed-path diff, the denylist
    # read and the broad marker all evaluate against that exact commit. Every merge must land the
    # SAME commit.
    #
    # Merging `f"origin/{BRANCH}"` re-resolves the ref, and `--ff-only` accepts a newer descendant
    # without complaint — so a concurrent fetch lands a commit whose CI was never checked and whose
    # paths were never classified. Worse than the bypass: the tree then equals origin, so
    # next_action() returns "noop" forever after and that commit is never deployed AND never
    # defer-and-alerted, while hold_sha, diverged_sha and behind_since all read green.
    #
    # The race is reachable — `scripts/deploy.sh` fetches (via deploy_staleness.py) BEFORE taking
    # /var/lock/server-git-tree.lock, and --dry-run returns without ever taking it.
    #
    # An AST guard rather than a behavioural one because gitops_deploy.py is not importable in CI
    # (module-level `C = cfg()` reads /etc), the same reason the rest of this file is source-level.
    merges = _git_merge_calls(gitops_fn("main"))
    assert merges, "no `git merge --ff-only` call found in main()"
    for call in merges:
        target = call.args[0].elts[-1]
        assert isinstance(target, ast.Name) and target.id == "origin", (
            "every `git merge --ff-only` in main() must merge the pinned `origin` SHA, not a "
            "re-resolved `origin/<branch>` ref — a concurrent fetch would otherwise absorb an "
            "un-CI'd commit that then reads as a permanent noop (2026-08-22 review H1)"
        )


def _is_git_reset_hard(node: ast.AST) -> bool:
    # A `run([... "git", "reset", "--hard", ...])` call — the rollback that reverts to the prior HEAD.
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run"
        and node.args
        and isinstance(node.args[0], ast.List)
    ):
        return False
    consts = {e.value for e in node.args[0].elts if isinstance(e, ast.Constant)}
    return {"reset", "--hard"} <= consts


def test_write_hold_precedes_every_rollback_reset(gitops_fn):
    # The 2026-07-14 run-4 M1 fix: write_hold(origin) must run BEFORE the `git reset --hard` + rollback
    # deploy() in BOTH failure paths, so a hung/SIGTERMed rollback still parks the bad commit on
    # skip_hold instead of re-merging + redeploying it every tick. A refactor moving write_hold after
    # the reset would otherwise reintroduce the strand-the-bad-commit loop and pass every other test.
    main = gitops_fn("main")
    reset_lines = [n.lineno for n in ast.walk(main) if _is_git_reset_hard(n)]
    # write_hold(<non-None>) linenos — write_hold(origin), NOT the write_hold(None) success-clear.
    hold_lines = [
        n.lineno
        for n in ast.walk(main)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "write_hold"
        and n.args
        and not (isinstance(n.args[0], ast.Constant) and n.args[0].value is None)
    ]
    assert len(reset_lines) >= 2, (
        "expected both rollback `git reset --hard` calls in main()"
    )
    for rl in reset_lines:
        assert any(0 < rl - hl <= 5 for hl in hold_lines), (
            "each rollback `git reset --hard` (line %d) must be immediately preceded by "
            "write_hold(origin)" % rl
        )


def test_deploy_uses_frozen(gitops_fn, str_constants):
    # A dropped `--frozen` would let a deploy mutate uv.lock on the host, dirtying the tree and wedging
    # the dirty-skip. deploy() isn't unit-tested either, so guard the invariant at the source level.
    assert "--frozen" in str_constants(gitops_fn("deploy")), (
        "deploy() must run ansible via `uv run --frozen`"
    )


def test_rollback_return_is_gated_on_delivered_post(gitops_fn):
    # 2026-07-14 run-5 L2: each rollback path must `return 0 if posted else 1` — exit 0 when the
    # detailed Discord post was delivered so systemd's OnFailure generic curl doesn't ALSO fire
    # (double-page), exit 1 only if the post failed so OnFailure is the guaranteed backstop.
    # Collapsing either terminal return to a bare `return 1` reintroduces the double-page this fix
    # removes; a bare `return 0` drops the OnFailure backstop. main() is un-importable, so pin the
    # invariant at the source like the sibling write_hold-ordering guard above.
    main = gitops_fn("main")
    reset_count = sum(1 for n in ast.walk(main) if _is_git_reset_hard(n))
    posted_returns = [
        n
        for n in ast.walk(main)
        if isinstance(n, ast.Return)
        and isinstance(n.value, ast.IfExp)
        and isinstance(n.value.test, ast.Name)
        and n.value.test.id == "posted"
        and isinstance(n.value.body, ast.Constant)
        and n.value.body.value == 0
        and isinstance(n.value.orelse, ast.Constant)
        and n.value.orelse.value == 1
    ]
    assert reset_count >= 2, (
        "expected both rollback paths (each a `git reset --hard`) in main()"
    )
    assert len(posted_returns) >= reset_count, (
        "each rollback path must `return 0 if posted else 1` so a delivered detailed post doesn't "
        "double-page via systemd OnFailure — one posted-gated return per rollback `git reset --hard`"
    )


# The pure is_diverged() (test_deploy_git.py) and the read side (check_gitops_status,
# test_check_gitops.py) are covered, but the WRITE — that main() emits the diverged-SHA marker every tick,
# gated on is_diverged, ahead of the action short-circuits — lives only in the un-importable main().
# A refactor dropping it or stranding it behind an early `return` would silently lose the watchdog
# (a diverged tree noops forever while origin's commits never deploy, both other GitOps signals
# green) and pass every other test. Pin it at the source like the write_hold-ordering guard above.


def test_diverged_marker_write_is_gated_and_precedes_action_branching(gitops_fn):
    main = gitops_fn("main")
    marker_writes = [
        n
        for n in ast.walk(main)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_write_marker"
        and n.args
        and isinstance(n.args[0], ast.Name)
        and n.args[0].id == "DIVERGED_FILE"
    ]
    assert marker_writes, (
        "main() must call _write_marker(DIVERGED_FILE, ...) every tick"
    )
    assert any(
        any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "is_diverged"
            for sub in ast.walk(w)
        )
        for w in marker_writes
    ), (
        "the DIVERGED_FILE marker write must be gated on is_diverged(...), not unconditional"
    )
    write_line = min(w.lineno for w in marker_writes)
    action_assign = next(
        (
            n.lineno
            for n in ast.walk(main)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "action" for t in n.targets)
        ),
        None,
    )
    assert action_assign is not None, "main() must assign `action = next_action(...)`"
    assert write_line < action_assign, (
        "the DIVERGED_FILE marker write must precede `action = next_action(...)` so it runs every "
        "tick regardless of the action short-circuit returns"
    )


# daniel-box fell into an empty ChangeSet -> the docs-only silent ff-merge, on the only host
# where every one of 41 services is platform: k8s). deploy_logic's ChangeSet.k8s / _ACTIVE_K8S are
# covered behaviourally (test_deploy_changes_services.py); main() itself is un-importable (module-level
# cfg() reads /etc config absent in CI), so this pins that alert_deferred() — the sole call site
# reached on BOTH the no-services branch and the post-deploy branch — actually reads cs.k8s,
# instead of silently never alerting on it.


def test_the_k8s_autodeploy_branch_alerts_on_a_bundled_secrets_change(gitops_fn):
    """A secrets push riding along with an image bump must not be ff-merged silently.

    The k8s auto-deploy branch ff-merges, deploys the promoted service and returns. Until
    2026-08-24 it never read cs.secrets, so a rotation push and a Renovate image PR landing in
    one 30-minute window arrived as a single ChangeSet: the secret was fast-forwarded, its real
    consumer was never redeployed, and no later tick re-evaluated it because the merge had
    already happened.

    Guarded at the AST because the branch is inside main(), which shells out to git; the
    helper it must call is tested by calling it in test_gitops_deploy_alert_channels.py.
    """
    for node in ast.walk(gitops_fn("main")):
        if not isinstance(node, ast.If):
            continue
        if ast.unparse(node.test) != "cs.k8s_deploy":
            continue
        called = {
            ast.unparse(n.func)
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "alert_secrets_deferred" in called, (
            "the cs.k8s_deploy branch returns without consulting cs.secrets. A rotation "
            "bundled with an auto-deployed image bump is then ff-merged and forgotten — the "
            "promoted service is image-bump-only by construction, so it is never the "
            "secret's consumer."
        )
        return
    raise AssertionError("main() no longer branches on cs.k8s_deploy")


def test_drain_pending_runs_before_short_circuits(gitops_fn):
    # The ff-merged secrets/tasks/meta/combined paths never re-reach their alert code on the next
    # (noop) tick, so a transient webhook failure is only recoverable by draining the queue at the TOP
    # of every tick — before the noop/hold/dirty returns. Guard that drain_pending() is called in
    # main() ahead of its first `return`.
    main = gitops_fn("main")
    drain_line = next(
        (
            n.lineno
            for n in ast.walk(main)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "drain_pending"
        ),
        None,
    )
    assert drain_line is not None, "main() must call drain_pending()"
    first_return = min(n.lineno for n in ast.walk(main) if isinstance(n, ast.Return))
    assert drain_line < first_return, (
        "drain_pending() must run before any short-circuit return in main()"
    )
