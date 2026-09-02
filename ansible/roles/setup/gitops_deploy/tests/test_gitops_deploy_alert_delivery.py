"""Source-level guards on how gitops_deploy.py gets an alert out.

discord() must route through host_lib.discord_post, where the User-Agent + 2xx contract is
behaviourally tested. deliver() must queue BEFORE it posts -- alert_once advances its per-SHA
marker first, so a process death inside the POST would otherwise lose the alert for good --
and must cap the queue it writes, because the pure cap_pending() has its own tests and a pure
function nobody calls is inert. drain_pending() must run ahead of every short-circuit return
in main(), since the ff-merged channels never re-reach their alert code on a later tick.

AST guards rather than behavioural ones: gitops_deploy.py cannot be imported in CI
(module-level `C = cfg()` reads /etc config that does not exist there).
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_alert_delivery.py

import ast


def test_discord_delegates_to_shared_discord_post(ast_calls, gitops_fn, str_constants):
    # The Cloudflare-1010 User-Agent + 2xx-only-success contract now lives in host_lib.discord_post,
    # which IS importable and is behaviourally tested (common/tests/test_host_lib.py) — strictly
    # stronger than the old AST proxy that pinned the "User-Agent"/200/300 constants inside this
    # un-importable module. Guard here only that gitops's discord() still ROUTES through it (a
    # regression inlining a UA-less POST would drop the call) and passes its own User-Agent.
    fn = gitops_fn("discord")
    assert ast_calls(fn, "discord_post"), (
        "discord() must delegate to host_lib.discord_post (the UA + 2xx contract lives there)"
    )
    assert "gitops-deploy" in str_constants(fn), (
        "discord() must pass its own User-Agent ('gitops-deploy') to discord_post"
    )


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


def test_deliver_queues_undelivered_for_retry(ast_calls, gitops_fn):
    # deliver() must persist an alert that failed to send (else H1's whole point — surviving a
    # transient webhook blip — is lost) and must actually attempt delivery via discord().
    fn = gitops_fn("deliver")
    assert ast_calls(fn, "_write_pending"), (
        "deliver() must persist an undelivered alert for retry"
    )
    assert ast_calls(fn, "discord"), "deliver() must attempt delivery via discord()"


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


def test_deliver_queues_before_it_sends(gitops_fn):
    """The accepting half: the live deliver() writes the queue ahead of the POST."""
    assert not sends_before_queueing(gitops_fn("deliver")), (
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


def test_deliver_clears_against_the_queued_baseline(gitops_fn):
    """Queue-first has one trap, and it turns the fix into a repost-every-tick bug.

    The post-send persist is guarded by an inequality. Compared against the PRE-queue dict, a
    successful send produces a dict equal to it, the guard is False, the entry never leaves the
    file, and drain_pending() reposts that alert on every tick forever. The baseline must be the
    dict that was actually written before the POST.
    """
    fn = gitops_fn("deliver")
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


# ── contract 3: deliver() actually bounds the pending queue ────────────────────────────────────
#
# cap_pending() has its own behavioural tests in test_deploy_health.py, but a pure function
# nobody calls is inert — the failure mode this repo has already paid for twice (volume-claim's
# short-circuit fired for 0 of 25 claims behind 16 passing tests). These assert the CALL SITE,
# which is the half those tests structurally cannot see.


def test_deliver_caps_the_pending_queue(gitops_fn):
    """Without this the queue is unbounded: nothing reads the file back except drain_pending(),
    so a permanently broken webhook grows it every 30 minutes forever."""
    called = {
        n.func.id
        for n in ast.walk(gitops_fn("deliver"))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "cap_pending" in called, "deliver() no longer bounds the pending-alert queue"


def test_deliver_logs_every_dropped_alert(gitops_fn, gitops_src):
    """An undelivered alert discarded without a trace is the failure the queue exists to prevent.
    The drop must reach the journal, which is what Loki indexes."""
    body = ast.dump(gitops_fn("deliver"))
    assert "log" in body, "deliver() no longer logs"
    src = ast.get_source_segment(gitops_src.read_text(), gitops_fn("deliver")) or ""
    assert "dropping oldest undelivered" in src, (
        "deliver() drops queue entries without logging which ones"
    )


def test_cap_pending_is_imported_from_the_pure_module(gitops_src):
    """It must be the tested implementation, not a second copy that can drift from it."""
    tree = ast.parse(gitops_src.read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "deploy_logic"
        for alias in node.names
    }
    assert {"cap_pending", "PENDING_ALERTS_MAX"} <= imported
