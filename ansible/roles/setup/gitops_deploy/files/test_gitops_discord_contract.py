# ansible/roles/setup/gitops_deploy/files/test_gitops_discord_contract.py
"""Source-level guards for gitops_deploy.py's I/O-shell contracts (discord() delivery + the
transient-fetch skip).

gitops_deploy.py can't be imported in CI (module-level `C = cfg()` reads /etc config that doesn't
exist there — the accepted design, see the role CLAUDE.md), so its I/O-shell invariants have no
behavioural test the way renovate_notify's does (test_renovate_notify.py). These AST assertions are
the narrow non-import guard: they prove the invariants still live in the source without executing the
un-importable module.

Contracts guarded here:
  1. discord(): a regression dropping the Cloudflare-1010 User-Agent header or loosening the 2xx
     success bound would silently advance a per-SHA dedupe marker on a FAILED post and permanently
     suppress a real rollback alert.
  2. transient `git fetch` skip: a retryable fetch failure must NOT double-page (crash Discord +
     OnFailure) and must NOT refresh last_run — else a one-off GitHub blip pages every tick, or a
     persistent fetch break hides behind a green GitOps-Alive. See RetryableFetchError.
"""

import ast
import pathlib
import re

import yaml

_SRC = pathlib.Path(__file__).with_name("gitops_deploy.py")


def _discord_fn() -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text())
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "discord"
        ),
        None,
    )
    assert fn is not None, "discord() not found in gitops_deploy.py"
    return fn


def _str_constants(fn: ast.FunctionDef) -> set[str]:
    return {
        c.value
        for c in ast.walk(fn)
        if isinstance(c, ast.Constant) and isinstance(c.value, str)
    }


def test_discord_delegates_to_shared_discord_post():
    # The Cloudflare-1010 User-Agent + 2xx-only-success contract now lives in host_lib.discord_post,
    # which IS importable and is behaviourally tested (common/files/test_host_lib.py) — strictly
    # stronger than the old AST proxy that pinned the "User-Agent"/200/300 constants inside this
    # un-importable module. Guard here only that gitops's discord() still ROUTES through it (a
    # regression inlining a UA-less POST would drop the call) and passes its own User-Agent.
    fn = _discord_fn()
    assert _calls(fn, "discord_post"), (
        "discord() must delegate to host_lib.discord_post (the UA + 2xx contract lives there)"
    )
    assert "gitops-deploy" in _str_constants(fn), (
        "discord() must pass its own User-Agent ('gitops-deploy') to discord_post"
    )


# A retryable fetch failure raises RetryableFetchError, which __main__ turns into a CLEAN skip:
# exit 0 (no OnFailure page), no in-script Discord crash-post, and — critically — no last_run
# refresh (so a persistent fetch break still surfaces via GitOps-Alive going stale).


def _tree() -> ast.Module:
    return ast.parse(_SRC.read_text())


def _main_guard_try() -> ast.Try:
    """The `try:` under `if __name__ == '__main__':`."""
    for node in ast.walk(_tree()):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        ):
            for child in node.body:
                if isinstance(child, ast.Try):
                    return child
    raise AssertionError("no try/except under `if __name__ == '__main__'`")


def _handler(try_node: ast.Try, exc_name: str) -> ast.ExceptHandler:
    for h in try_node.handlers:
        if isinstance(h.type, ast.Name) and h.type.id == exc_name:
            return h
    raise AssertionError(f"no `except {exc_name}` handler in __main__")


def _calls(node: ast.AST, fn_name: str) -> bool:
    return any(
        isinstance(c, ast.Call)
        and (
            (isinstance(c.func, ast.Name) and c.func.id == fn_name)
            or (isinstance(c.func, ast.Attribute) and c.func.attr == fn_name)
        )
        for c in ast.walk(node)
    )


def test_retryable_fetch_error_defined():
    assert any(
        isinstance(n, ast.ClassDef) and n.name == "RetryableFetchError"
        for n in ast.walk(_tree())
    ), "RetryableFetchError must be defined"


def test_fetch_failure_raises_retryable_error():
    # The fetch-failure path must raise RetryableFetchError — not fall through run()'s RuntimeError,
    # which would reach the generic crash-page (the double-page this fix removes).
    assert any(
        isinstance(n, ast.Raise)
        and isinstance(n.exc, ast.Call)
        and isinstance(n.exc.func, ast.Name)
        and n.exc.func.id == "RetryableFetchError"
        for n in ast.walk(_tree())
    ), "the fetch-failure path must `raise RetryableFetchError(...)`"


def test_retryable_handler_does_not_page_or_refresh_liveness():
    handler = _handler(_main_guard_try(), "RetryableFetchError")
    assert not _calls(handler, "discord"), (
        "the retryable-fetch handler must not post a Discord crash alert (no double-page)"
    )
    assert not _calls(handler, "_write_marker"), (
        "the retryable-fetch handler must not write last_run — else a persistent fetch break "
        "hides behind a green GitOps-Alive"
    )
    assert any(  # exit 0 → systemd sees success → OnFailure alert unit doesn't fire
        isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and c.func.attr == "exit"
        and c.args
        and isinstance(c.args[0], ast.Constant)
        and c.args[0].value == 0
        for c in ast.walk(handler)
    ), "the retryable-fetch handler must sys.exit(0)"


def test_retryable_handler_precedes_generic_crash_handler():
    # Order matters: except-clauses match top-down, so RetryableFetchError must precede the bare
    # `except Exception` or it's dead code (Exception would catch it first and page).
    names = [
        h.type.id for h in _main_guard_try().handlers if isinstance(h.type, ast.Name)
    ]
    assert names.index("RetryableFetchError") < names.index("Exception"), (
        "`except RetryableFetchError` must precede `except Exception`"
    )


def test_generic_crash_handler_still_pages():
    # Regression guard: the fix must not have silenced GENUINE crashes — the generic handler must
    # still Discord-page on an unexpected exception.
    assert _calls(_handler(_main_guard_try(), "Exception"), "discord"), (
        "the generic crash handler must still post a Discord alert"
    )


# main() can't be imported (module-level `C = cfg()` reads /etc config absent in CI), so these AST
# guards pin two source invariants that no behavioural test can reach.


def _fn(name: str) -> ast.FunctionDef:
    fn = next(
        (
            n
            for n in ast.walk(_tree())
            if isinstance(n, ast.FunctionDef) and n.name == name
        ),
        None,
    )
    assert fn is not None, f"{name}() not found in gitops_deploy.py"
    return fn


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


def test_every_ff_merge_targets_the_pinned_sha_not_the_ref():
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
    merges = _git_merge_calls(_fn("main"))
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


def test_write_hold_precedes_every_rollback_reset():
    # The 2026-07-14 run-4 M1 fix: write_hold(origin) must run BEFORE the `git reset --hard` + rollback
    # deploy() in BOTH failure paths, so a hung/SIGTERMed rollback still parks the bad commit on
    # skip_hold instead of re-merging + redeploying it every tick. A refactor moving write_hold after
    # the reset would otherwise reintroduce the strand-the-bad-commit loop and pass every other test.
    main = _fn("main")
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


def test_deploy_uses_frozen():
    # A dropped `--frozen` would let a deploy mutate uv.lock on the host, dirtying the tree and wedging
    # the dirty-skip. deploy() isn't unit-tested either, so guard the invariant at the source level.
    assert "--frozen" in _str_constants(_fn("deploy")), (
        "deploy() must run ansible via `uv run --frozen`"
    )


def test_drain_pending_runs_before_short_circuits():
    # The ff-merged secrets/tasks/meta/combined paths never re-reach their alert code on the next
    # (noop) tick, so a transient webhook failure is only recoverable by draining the queue at the TOP
    # of every tick — before the noop/hold/dirty returns. Guard that drain_pending() is called in
    # main() ahead of its first `return`.
    main = _fn("main")
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


def test_deliver_queues_undelivered_for_retry():
    # deliver() must persist an alert that failed to send (else H1's whole point — surviving a
    # transient webhook blip — is lost) and must actually attempt delivery via discord().
    fn = _fn("deliver")
    assert _calls(fn, "_write_pending"), (
        "deliver() must persist an undelivered alert for retry"
    )
    assert _calls(fn, "discord"), "deliver() must attempt delivery via discord()"


# The 2026-08-31 review M-1. alert_once advances its per-SHA marker BEFORE calling deliver(), and
# discord() blocks for up to 10s inside urlopen. While deliver() queued only AFTER that call, a
# process death in the window (a reboot, a `systemctl stop`, the UPS shutdown chain) left a durable
# "already alerted" marker with nothing delivered and nothing queued — and the ff-merged channels
# never re-reach their alert code on a later tick, so the alert was gone for good with every monitor
# green. Queue-first makes the same death recoverable: drain_pending() runs at the top of the next
# tick, ahead of every short-circuit, and reposts it.
def _first_call_line(fn: ast.FunctionDef, name: str) -> int | None:
    lines = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == name
    ]
    return min(lines) if lines else None


def sends_before_queueing(fn: ast.FunctionDef) -> bool:
    """Does this deliver() POST before it persists the queue? The verdict both halves share."""
    write_line = _first_call_line(fn, "_write_pending")
    send_line = _first_call_line(fn, "discord")
    if write_line is None or send_line is None:
        return False
    return send_line < write_line


def test_deliver_queues_before_it_sends():
    """The accepting half: the live deliver() writes the queue ahead of the POST."""
    assert not sends_before_queueing(_fn("deliver")), (
        "deliver() calls discord() before _write_pending() — a death inside the 10s POST then "
        "drops the alert permanently, because alert_once has already advanced its marker"
    )


def test_a_deliver_that_sends_first_is_flagged():
    """The rejecting half: the pre-fix shape must come back True, or the check above is inert."""
    before_the_fix = ast.parse(
        "def deliver(key, content):\n"
        "    pending = _read_pending()\n"
        "    delivered = discord(content)\n"
        "    updated = apply_send_result(pending, key, content, delivered)\n"
        "    updated, dropped = cap_pending(updated)\n"
        "    if updated != pending:\n"
        "        _write_pending(updated)\n"
        "    return delivered\n"
    )
    fn = next(
        n
        for n in ast.walk(before_the_fix)
        if isinstance(n, ast.FunctionDef) and n.name == "deliver"
    )
    assert sends_before_queueing(fn), (
        "the check no longer sees a deliver() that POSTs before it queues"
    )


def test_deliver_clears_against_the_queued_baseline():
    """Queue-first has one trap, and it turns the fix into a repost-every-tick bug.

    The post-send persist is guarded by an inequality. Compared against the PRE-queue dict, a
    successful send produces a dict equal to it, the guard is False, the entry never leaves the
    file, and drain_pending() reposts that alert on every tick forever. The baseline must be the
    dict that was actually written before the POST.
    """
    fn = _fn("deliver")
    written = {
        n.args[0].id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_write_pending"
        and n.args
        and isinstance(n.args[0], ast.Name)
    }
    # Only the guard AFTER the POST matters. The pre-send write is legitimately guarded by
    # `queued != pending` — that one compares against the pre-queue dict by design.
    send_line = _first_call_line(fn, "discord") or 0
    baselines = {
        cmp.comparators[0].id
        for cmp in ast.walk(fn)
        if isinstance(cmp, ast.Compare)
        and isinstance(cmp.comparators[0], ast.Name)
        and any(isinstance(op, ast.NotEq) for op in cmp.ops)
        and cmp.lineno > send_line
    }
    assert "queued" in written, (
        "deliver() no longer writes a pre-send `queued` dict — the queue-first fix is gone"
    )
    assert "pending" not in baselines, (
        "deliver()'s persist guard compares against the pre-queue `pending` dict, so a delivered "
        "alert is never removed from the queue and drain_pending() reposts it every tick"
    )


def test_rollback_return_is_gated_on_delivered_post():
    # 2026-07-14 run-5 L2: each rollback path must `return 0 if posted else 1` — exit 0 when the
    # detailed Discord post was delivered so systemd's OnFailure generic curl doesn't ALSO fire
    # (double-page), exit 1 only if the post failed so OnFailure is the guaranteed backstop.
    # Collapsing either terminal return to a bare `return 1` reintroduces the double-page this fix
    # removes; a bare `return 0` drops the OnFailure backstop. main() is un-importable, so pin the
    # invariant at the source like the sibling write_hold-ordering guard above.
    main = _fn("main")
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


# The pure is_diverged() (test_deploy_logic_git.py) and the read side (check_gitops_status,
# test_check_gitops.py) are covered, but the WRITE — that main() emits the diverged-SHA marker every tick,
# gated on is_diverged, ahead of the action short-circuits — lives only in the un-importable main().
# A refactor dropping it or stranding it behind an early `return` would silently lose the watchdog
# (a diverged tree noops forever while origin's commits never deploy, both other GitOps signals
# green) and pass every other test. Pin it at the source like the write_hold-ordering guard above.


def test_diverged_marker_write_is_gated_and_precedes_action_branching():
    main = _fn("main")
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


# "The rollback survives max flock contention" is an invariant split across two templates:
#   config.env.j2            -> RUN_BUDGET_S (health-gate budget) + HEALTH_TIMEOUT_S (rollback redeploy)
#   gitops-deploy.service.j2 -> flock -w <N> (max lock wait) + TimeoutStartSec (systemd hard kill)
# RUN_START is measured AFTER flock acquires, but TimeoutStartSec counts from unit activation and so
# INCLUDES the flock wait — so the worst case flock_wait + RUN_BUDGET_S + HEALTH_TIMEOUT_S must fit
# inside TimeoutStartSec, else systemd SIGTERMs the deployer mid-rollback and the bad commit is
# stranded live (the failure 1ba4fbb2 sized these four values to avoid, down to zero slack). Nothing
# else pins the cross-file sum, so a later bump to any one value would silently reopen it while every
# other test stays green — the same class the write_hold / divergence-marker guards above pin.

_TEMPLATES = pathlib.Path(__file__).parents[1] / "templates"


def _search1(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.MULTILINE)
    assert m is not None, f"pattern {pattern!r} did not match — template renamed?"
    return m.group(1)


def _systemd_seconds(span: str) -> int:
    # Parse the systemd time spans this unit actually uses (Nmin / Ns / bare seconds).
    m = re.fullmatch(r"(\d+)\s*(min|m|sec|s|)", span.strip())
    assert m is not None, f"unrecognized systemd time span {span!r}"
    return int(m.group(1)) * (60 if m.group(2) in ("min", "m") else 1)


# daniel-box fell into an empty ChangeSet -> the docs-only silent ff-merge, on the only host
# where every one of 41 services is platform: k8s). deploy_logic's ChangeSet.k8s / _ACTIVE_K8S are
# covered behaviourally (test_deploy_logic_diff.py); main() itself is un-importable (module-level
# cfg() reads /etc config absent in CI), so this pins that alert_deferred() — the sole call site
# reached on BOTH the no-services branch and the post-deploy branch — actually reads cs.k8s,
# instead of silently never alerting on it.


def test_alert_deferred_handles_k8s_channel():
    fn = _fn("alert_deferred")
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "k8s" for n in ast.walk(fn)
    ), "alert_deferred() must alert on cs.k8s (the k8s-role defer-and-alert channel)"


def test_deploy_timeout_budget_survives_max_flock_contention():
    env = (_TEMPLATES / "config.env.j2").read_text()
    unit = (_TEMPLATES / "gitops-deploy.service.j2").read_text()
    flock_wait = int(_search1(r"^ExecStart=.*?flock\s+-w\s+(\d+)", unit))
    run_budget = int(_search1(r"^RUN_BUDGET_S=(\d+)", env))
    health_timeout = int(_search1(r"^HEALTH_TIMEOUT_S=(\d+)", env))
    timeout_start = _systemd_seconds(_search1(r"^TimeoutStartSec=(\S+)", unit))
    budget = flock_wait + run_budget + health_timeout
    assert budget <= timeout_start, (
        f"flock -w {flock_wait} + RUN_BUDGET_S {run_budget} + HEALTH_TIMEOUT_S {health_timeout} "
        f"= {budget}s must fit inside TimeoutStartSec {timeout_start}s, or a slow health-gate under "
        f"max flock contention gets SIGTERMed mid-rollback and the bad commit is stranded live "
        f"(see 1ba4fbb2)."
    )


# A second, independent invariant from the Docker one above: on the k8s path, a failed forward
# deploy and its rollback redeploy run SEQUENTIALLY inside one systemd unit activation, each
# bounded by its own K8S_DEPLOY_TIMEOUT_S / K8S_ROLLBACK_TIMEOUT_S rather than by RUN_BUDGET_S.
# Both values are Jinja references in config.env.j2, not literals, so this reads their source —
# defaults/main.yml — instead of the rendered template.

_DEFAULTS = pathlib.Path(__file__).parents[1] / "defaults" / "main.yml"


def _worst_lock_hold(defaults: dict) -> int:
    """Longest one gitops-deploy activation can hold the git-tree lock, EXCLUDING its own flock
    wait (which is spent before the lock is held).

    All four terms are on the SAME path and are additive, not alternative: consult_staging runs
    inside `if cs.k8s_deploy:` in main(), ahead of deploy_k8s, so an activation that stalls the
    staging gate and then stalls both playbook budgets spends all four in sequence. The BROAD arm
    returns before that block and so cannot stack with any of them.

    The staging terms are counted even though gitops_deploy_staging_gate is false by default. The
    host that has the gate ON is the one whose budget has to fit, and a budget that only holds
    while a feature is off is not a budget — that reading is exactly how the 2026-08-29 review's
    H-1 got in: 1200 + 180 sat unsummed inside a 2700s ceiling and every check read green.
    """
    return (
        int(defaults["gitops_deploy_staging_gate_timeout_s"])
        + int(defaults["gitops_deploy_staging_expect_timeout_s"])
        + int(defaults["gitops_deploy_k8s_timeout_s"])
        + int(defaults["gitops_deploy_k8s_rollback_timeout_s"])
    )


def _budget_fits(defaults: dict, flock_wait: int, timeout_start: int) -> bool:
    return flock_wait + _worst_lock_hold(defaults) <= timeout_start


def test_k8s_deploy_timeout_budget_survives_max_flock_contention():
    unit = (_TEMPLATES / "gitops-deploy.service.j2").read_text()
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    flock_wait = int(_search1(r"^ExecStart=.*?flock\s+-w\s+(\d+)", unit))
    timeout_start = _systemd_seconds(_search1(r"^TimeoutStartSec=(\S+)", unit))
    hold = _worst_lock_hold(defaults)
    assert _budget_fits(defaults, flock_wait, timeout_start), (
        f"flock -w {flock_wait} + the worst-case lock hold {hold}s (staging gate + staging "
        f"expectations + K8S_DEPLOY_TIMEOUT_S + K8S_ROLLBACK_TIMEOUT_S) = "
        f"{flock_wait + hold}s must fit inside TimeoutStartSec {timeout_start}s, or a stalled "
        f"forward deploy followed by a stalled rollback gets SIGTERMed mid-rollback, stranding "
        f"the bad commit live with the volume revert possibly half-done (task 6b)."
    )


def test_an_uncounted_staging_budget_is_caught():
    # Red proof for the three budget tests that share _worst_lock_hold. They can only ever be
    # observed passing, so this drives the same verdict function with the pre-fix numbers: the
    # 2026-08-29 review's H-1, where a 1200s gate and a 180s expectation check sat inside a
    # 2700s ceiling that nothing summed them into.
    sized = {
        "gitops_deploy_staging_gate_timeout_s": 600,
        "gitops_deploy_staging_expect_timeout_s": 120,
        "gitops_deploy_k8s_timeout_s": 900,
        "gitops_deploy_k8s_rollback_timeout_s": 1320,
    }
    assert _worst_lock_hold(sized) == 2940
    assert _budget_fits(sized, 180, 3600)

    h1 = {
        **sized,
        "gitops_deploy_staging_gate_timeout_s": 1200,
        "gitops_deploy_staging_expect_timeout_s": 180,
    }
    assert _worst_lock_hold(h1) == 3600
    assert not _budget_fits(h1, 180, 2700), (
        "the budget check must REJECT the H-1 shape (180 + 1200 + 180 + 900 + 1320 = 3780s "
        "against TimeoutStartSec 2700s); a check that passes it is measuring nothing."
    )


_SECRET_ROTATE = (
    pathlib.Path(__file__).parents[2]
    / "initial_setup"
    / "templates"
    / "secret-rotate.sh.j2"
)


def test_secret_rotate_lock_wait_clears_the_deployers_worst_case_hold():
    # 2026-08-22 review M4. gitops-deploy.service wraps its whole ExecStart in
    # /var/lock/server-git-tree.lock, and one activation can run the forward deploy budget and
    # then, in the failure path, the rollback budget — sequentially, inside that one hold. The
    # weekly secret-rotate cron waits on the same lock.
    #
    # At `flock -w 1200` against a 2220s worst case the cron gave up mid-incident and SKIPPED
    # that week's rotation. crons.yml installs one weekly entry with no retry, and
    # ROTATE_LEAD_DAYS=8 against a 7-day cadence means a token usually gets exactly one eligible
    # run — so a skipped week can put a token overdue.
    #
    # Derived from the same sources the two budgets above read, so bumping either deploy timeout
    # fails this test instead of silently re-opening the gap. A single failing service reaches
    # the worst case; it does not need a batch.
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    worst_hold = _worst_lock_hold(defaults)

    cron_wait = int(_search1(r"^flock\s+-w\s+(\d+)\s+9", _SECRET_ROTATE.read_text()))
    assert cron_wait >= worst_hold, (
        f"secret-rotate's `flock -w {cron_wait}` must clear gitops-deploy's worst-case lock hold "
        f"({worst_hold}s: the staging gate and expectation check, then K8S_DEPLOY_TIMEOUT_S and "
        f"K8S_ROLLBACK_TIMEOUT_S), or a legitimate long rollback makes the weekly rotation skip a week "
        f"with no retry (2026-08-22 review M4)."
    )


# K8S_ROLLBACK_TIMEOUT_S must cover one full rollback cycle for the most expensive currently-
# promoted (k8s_autodeploy: true) service that also declares k8s_autodeploy_snapshot_pvcs: the
# pre-revert snapshot wait, the revert itself, the forward apply's own rollout wait, and the
# post-rollout stabilisation soak — all inside the SAME playbook run, on one continuous timeline
# where nothing fails (a failure aborts the whole play immediately, so it can never compound with
# an independent failure elsewhere — see gitops_deploy/CLAUDE.md's rollback-timeout section).
#
# Deliberately a PER-SERVICE bound, not a per-batch one: co-batched claim-declaring services
# stack their snapshot+revert phases (only the rollout WAIT is deduped across a batch, via
# roles/k8s/rollout-drain), so a multi-service batch is NOT covered here — that gap is recorded
# in gitops_deploy/CLAUDE.md ("the batch-abort blast radius") and in this same defaults/main.yml
# comment, deliberately not modeled by this test.
#
# Computed from role SOURCES, not pinned numbers, so a future rollout-timeout bump or a new
# promoted claim-declaring role fails this test instead of silently under-sizing the budget.

_K8S_ROLES_DIR = pathlib.Path(__file__).parents[3] / "k8s"
_ALL_VARS = pathlib.Path(__file__).parents[4] / "inventory" / "group_vars" / "all.yml"
_MANIFESTS_ROLLOUT_DEFAULT_S = (
    300  # k8s/manifests default: manifests_rollout_timeout | default('300s')
)


def _rollout_timeout_s(role: str) -> int:
    tasks_path = _K8S_ROLES_DIR / role / "tasks" / "main.yml"
    text = tasks_path.read_text() if tasks_path.exists() else ""
    m = re.search(r"manifests_rollout_timeout:\s*(\d+)s", text)
    return int(m.group(1)) if m else _MANIFESTS_ROLLOUT_DEFAULT_S


def test_k8s_rollback_budget_covers_the_worst_single_promoted_service():
    revert_defaults = yaml.safe_load(
        (_K8S_ROLES_DIR / "volume-revert" / "defaults" / "main.yml").read_text()
    )
    snapshot_defaults = yaml.safe_load(
        (_K8S_ROLES_DIR / "volume-snapshot" / "defaults" / "main.yml").read_text()
    )
    all_vars = yaml.safe_load(_ALL_VARS.read_text())
    defaults = yaml.safe_load(_DEFAULTS.read_text())

    state_timeout = int(revert_defaults["volume_revert_state_timeout"])
    api_timeout = int(revert_defaults["volume_revert_api_timeout"])
    snapshot_timeout = int(snapshot_defaults["volume_snapshot_timeout"])
    stabilise = int(all_vars["k8s_rollout_stabilise_seconds"])
    rollback_timeout = int(defaults["gitops_deploy_k8s_rollback_timeout_s"])
    per_claim = snapshot_timeout + 3 * state_timeout + 3 * api_timeout

    worst_role, worst_ceiling, worst_claims = None, 0, 0
    for role_defaults_path in sorted(_K8S_ROLES_DIR.glob("*/defaults/main.yml")):
        role = role_defaults_path.parent.parent.name
        role_defaults = yaml.safe_load(role_defaults_path.read_text()) or {}
        if not role_defaults.get("k8s_autodeploy"):
            continue
        claims = role_defaults.get("k8s_autodeploy_snapshot_pvcs") or []
        if not claims:
            continue
        ceiling = len(claims) * per_claim + _rollout_timeout_s(role) + stabilise
        if ceiling > worst_ceiling:
            worst_role, worst_ceiling, worst_claims = role, ceiling, len(claims)

    assert worst_role is not None, (
        "no promoted (k8s_autodeploy: true), claim-declaring k8s role found — the sizing model "
        "this test encodes no longer matches the repo; update it rather than deleting it"
    )
    assert worst_ceiling <= rollback_timeout, (
        f"{worst_role} needs {worst_ceiling}s for one full rollback cycle "
        f"({worst_claims} claim(s), "
        f"{_rollout_timeout_s(worst_role)}s rollout), which exceeds "
        f"gitops_deploy_k8s_rollback_timeout_s ({rollback_timeout}s) — its rollback can be "
        f"SIGTERMed mid-revert. Raise that default (and TimeoutStartSec, and re-check this "
        f"test's own comment on the batch-summation gap it does not cover)."
    )


_DEPLOY_SH = pathlib.Path(__file__).parents[5] / "scripts" / "deploy.sh"


def test_deploy_sh_lock_wait_clears_the_deployers_worst_case_hold():
    # 2026-08-23b review M13. The sibling above pins the weekly secret-rotate cron's wait
    # against the same worst case. deploy.sh computes the identical quantity by hand, and its
    # own comment records that the hand-derived value already rotted once: 1500 stayed put
    # through two TimeoutStartSec bumps. Deriving it from the same defaults the deployer reads
    # means the next bump fails here instead of silently shortening an operator's wait.
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    worst_hold = _worst_lock_hold(defaults)

    lock_wait = int(_search1(r"^LOCK_WAIT=(\d+)", _DEPLOY_SH.read_text()))
    assert lock_wait >= worst_hold, (
        f"deploy.sh's LOCK_WAIT={lock_wait} must clear gitops-deploy's worst-case lock hold "
        f"({worst_hold}s: the staging gate and expectation check, then K8S_DEPLOY_TIMEOUT_S and "
        f"K8S_ROLLBACK_TIMEOUT_S), or an operator deploy queued behind a legitimately long rollback exits "
        f"75 having deployed nothing (2026-08-23b review M13)."
    )


_ROLES = pathlib.Path(__file__).parents[3]

# A Jinja interpolation naming a secret. Matched on the VARIABLE NAME, not on "does this
# ExecStart interpolate anything" — the broad form flags six legitimate units that interpolate
# a path or a username (claude-rc.service.j2, both gitops-deploy units, both renovate-notify
# units, one retired archive/ template). Only the secret-named form isolates a real leak.
_SECRET_VAR = re.compile(
    r"\{\{[^}]*\b\w+(?:_webhook|_token|_password|_secret|_key)\b[^}]*\}\}"
)


def _unit_templates() -> list[pathlib.Path]:
    """Every systemd unit template in the repo, minus retired code.

    A TREE WALK, not an enumeration. The enumeration this replaces named two paths and so
    could only ever prove the two units its own fix had touched — `claude-rc-alert.service.j2`
    landed the same morning carrying the identical embed and the guard could not see it
    (2026-08-24 review M-2). A guard written alongside its fix inherits the fix's scope unless
    it derives its own corpus.
    """
    return sorted(
        p
        for p in _ROLES.rglob("*.service.j2")
        if "archive" not in p.parts  # roles/containers/archive/ is retired code
    )


def test_no_unit_template_embeds_a_secret_in_execstart():
    # 2026-08-23b review M5, re-scoped by 2026-08-24 review M-2. Units used to interpolate the
    # SOPS webhook straight into ExecStart and rely on `mode: 0600` to protect it. The mode is
    # real and irrelevant: systemd serves unit content over the system bus, so
    # `systemctl show <unit> -p ExecStart` printed the full webhook URL to any local user with
    # no sudo. Reproduced on every affected unit, and reproduced again after each fix to confirm
    # EnvironmentFile makes the same command print the literal ${ALERT_WEBHOOK}.
    #
    # A comment claiming the protection is the least reliable evidence in the file, so the claim
    # gets a test — and the test derives which files it covers rather than being told.
    units = _unit_templates()
    assert units, (
        f"No *.service.j2 found under {_ROLES} — the walk is broken, not the tree."
    )
    for unit_path in units:
        unit = unit_path.read_text()
        exec_start = re.search(
            r"^ExecStart=.*?(?=\n(?!\s)|\Z)", unit, re.MULTILINE | re.DOTALL
        )
        if not exec_start:
            continue  # a .timer-adjacent or Type=oneshot-less unit; nothing to leak
        leaked = _SECRET_VAR.search(exec_start.group(0))
        assert not leaked, (
            f"{unit_path.relative_to(_ROLES)} interpolates {leaked.group(0)} into ExecStart. "
            f"`systemctl show` will print it to any local user regardless of the unit file's "
            f'mode. Reference it as "${{ALERT_WEBHOOK}}" and supply it with EnvironmentFile= '
            f"instead."
        )


def test_alert_units_read_a_dedicated_webhook_file():
    # The other half of the same contract: an alert unit must not fall back to its role's
    # config.env. That file is exactly what can be unreadable when the thing this unit alerts
    # for has failed, which would leave the alert unable to page.
    alerts = [p for p in _unit_templates() if p.name.endswith("-alert.service.j2")]
    assert alerts, "No *-alert.service.j2 found — the walk is broken, not the tree."
    for unit_path in alerts:
        unit = unit_path.read_text()
        assert re.search(
            r"^EnvironmentFile=\S*alert-webhook\.env$", unit, re.MULTILINE
        ), (
            f"{unit_path.relative_to(_ROLES)} does not read a dedicated alert-webhook.env. It "
            f"must NOT fall back to the role's config.env — that file is exactly what can be "
            f"unreadable when the thing this unit alerts for has failed."
        )


def _main_fn() -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("gitops_deploy.py has no main()")


def test_the_k8s_autodeploy_branch_alerts_on_a_bundled_secrets_change():
    """A secrets push riding along with an image bump must not be ff-merged silently.

    The k8s auto-deploy branch ff-merges, deploys the promoted service and returns. Until
    2026-08-24 it never read cs.secrets, so a rotation push and a Renovate image PR landing in
    one 30-minute window arrived as a single ChangeSet: the secret was fast-forwarded, its real
    consumer was never redeployed, and no later tick re-evaluated it because the merge had
    already happened.

    Guarded at the AST rather than behaviourally because gitops_deploy.py cannot be imported in
    CI (module-level `C = cfg()` reads /etc) — the same constraint the rest of this file works
    under.
    """
    for node in ast.walk(_main_fn()):
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


# ── contract 3: deliver() actually bounds the pending queue ────────────────────────────────────
#
# cap_pending() has its own behavioural tests in test_deploy_logic_health.py, but a pure function
# nobody calls is inert — the failure mode this repo has already paid for twice (volume-claim's
# short-circuit fired for 0 of 25 claims behind 16 passing tests). These assert the CALL SITE,
# which is the half those tests structurally cannot see.


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text())
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == name
        ),
        None,
    )
    assert fn is not None, f"{name}() not found in gitops_deploy.py"
    return fn


def test_deliver_caps_the_pending_queue():
    """Without this the queue is unbounded: nothing reads the file back except drain_pending(),
    so a permanently broken webhook grows it every 30 minutes forever."""
    called = {
        n.func.id
        for n in ast.walk(_fn("deliver"))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "cap_pending" in called, "deliver() no longer bounds the pending-alert queue"


def test_deliver_logs_every_dropped_alert():
    """An undelivered alert discarded without a trace is the failure the queue exists to prevent.
    The drop must reach the journal, which is what Loki indexes."""
    body = ast.dump(_fn("deliver"))
    assert "log" in body, "deliver() no longer logs"
    src = ast.get_source_segment(_SRC.read_text(), _fn("deliver")) or ""
    assert "dropping oldest undelivered" in src, (
        "deliver() drops queue entries without logging which ones"
    )


def test_cap_pending_is_imported_from_the_pure_module():
    """It must be the tested implementation, not a second copy that can drift from it."""
    tree = ast.parse(_SRC.read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "deploy_logic"
        for alias in node.names
    }
    assert {"cap_pending", "PENDING_ALERTS_MAX"} <= imported


# ── the staging timeouts exist in three places, and only one of them gates production ─────────
# defaults/main.yml is the source; config.env.j2 renders it onto the host; gitops_deploy.py
# carries a `C.get(name, "<literal>")` fallback for a host whose config predates that render.
# Until 2026-08-29 the render was missing entirely, so the FALLBACK was what production ran on
# and editing the defaults would have moved nothing. These two pin all three together.


def _env_fallbacks(source: str) -> dict[str, int]:
    """The literal defaults gitops_deploy.py falls back to when config.env lacks a key."""
    return {
        name: int(value)
        for name, value in re.findall(
            r'C\.get\(\s*"([A-Z0-9_]+)"\s*,\s*"(\d+)"\s*\)', source
        )
    }


def _rendered_env_keys(template: str) -> set[str]:
    """The keys config.env.j2 actually emits (`KEY={{ ... }}` or `KEY=literal`)."""
    return set(re.findall(r"^([A-Z0-9_]+)=", template, re.MULTILINE))


def test_staging_timeout_fallbacks_match_the_ansible_defaults():
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    fallbacks = _env_fallbacks(_SRC.read_text())
    for env_key, default_key in (
        ("STAGING_GATE_TIMEOUT_S", "gitops_deploy_staging_gate_timeout_s"),
        ("STAGING_EXPECT_TIMEOUT_S", "gitops_deploy_staging_expect_timeout_s"),
    ):
        assert env_key in fallbacks, (
            f"{env_key} lost its C.get fallback in gitops_deploy.py"
        )
        assert fallbacks[env_key] == int(defaults[default_key]), (
            f"gitops_deploy.py falls back to {env_key}={fallbacks[env_key]} while "
            f"{default_key} is {defaults[default_key]}. A host whose config.env predates the "
            f"render uses the fallback, so the two disagreeing means the budget the unit is "
            f"sized against is not the budget that host spends."
        )


def test_a_drifted_fallback_is_caught():
    # Red proof for the pair above: the same parser, driven with a source whose literal
    # disagrees with the default it shadows.
    assert _env_fallbacks('X = int(C.get("STAGING_GATE_TIMEOUT_S", "600"))') == {
        "STAGING_GATE_TIMEOUT_S": 600
    }
    drifted = _env_fallbacks('X = int(C.get("STAGING_GATE_TIMEOUT_S", "1200"))')
    assert drifted["STAGING_GATE_TIMEOUT_S"] != 600, (
        "the parser must read the literal rather than the name, or a drifted fallback "
        "reads as agreeing with whatever the defaults say."
    )


def test_staging_timeouts_are_rendered_into_config_env():
    keys = _rendered_env_keys((_TEMPLATES / "config.env.j2").read_text())
    for env_key in ("STAGING_GATE_TIMEOUT_S", "STAGING_EXPECT_TIMEOUT_S"):
        assert env_key in keys, (
            f"config.env.j2 must emit {env_key}, or gitops_deploy.py's C.get fallback is what "
            f"gates production and changing the Ansible default moves nothing on the host."
        )


def test_an_unrendered_key_is_caught():
    # Red proof for the test above, on the same extractor. The rejected input is the shape
    # config.env.j2 actually had until 2026-08-29: STAGING_GATE rendered, its two timeouts not.
    assert _rendered_env_keys("STAGING_GATE={{ x }}\nSTAGING_GATE_TIMEOUT_S=600\n") == {
        "STAGING_GATE",
        "STAGING_GATE_TIMEOUT_S",
    }
    assert "STAGING_GATE_TIMEOUT_S" not in _rendered_env_keys(
        "STAGING_GATE={{ x }}\n# STAGING_GATE_TIMEOUT_S is only a comment\n"
    ), "a key named only in a comment must not count as rendered"
