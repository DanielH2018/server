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


def _int_constants(fn: ast.FunctionDef) -> set[int]:
    return {
        c.value
        for c in ast.walk(fn)
        if isinstance(c, ast.Constant) and isinstance(c.value, int)
    }


def test_discord_sends_user_agent_header():
    # Cloudflare 1010-blocks the default python-urllib UA; without a UA the POST 403s and the
    # `except` swallows it -> a silently-undelivered rollback alert. Assert a User-Agent is set.
    assert "User-Agent" in _str_constants(_discord_fn()), (
        "discord() must set a User-Agent header (Cloudflare 1010)"
    )


def test_discord_returns_true_only_on_2xx():
    # The per-SHA dedupe marker is gated on discord() returning True; it must return True ONLY on a
    # 2xx so a transient failure doesn't advance the marker and suppress the alert forever. Assert a
    # `200 <= status < 300`-shaped bound survives.
    assert {200, 300} <= _int_constants(_discord_fn()), (
        "discord() must bound success to 2xx (200 <= status < 300)"
    )


# --- transient `git fetch` skip contract -------------------------------------
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


# --- write_hold-before-rollback ordering (run-4 M1) + deploy --frozen ---------
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


# --- pending-alert queue (H1): no post-merge alert silently lost on a webhook blip ---


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
