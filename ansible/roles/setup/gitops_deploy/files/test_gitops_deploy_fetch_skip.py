"""A transient `git fetch` failure must be a clean skip, not a page.

A retryable fetch failure must NOT double-page (crash Discord + OnFailure) and must NOT
refresh last_run -- else a one-off GitHub blip pages every tick, or a persistent fetch break
hides behind a green GitOps-Alive. gitops_deploy.py cannot be imported in CI (module-level
`C = cfg()` reads /etc config that does not exist there), so the handler chain under
`if __name__ == "__main__"` is pinned at the AST. See RetryableFetchError.
"""

# ansible/roles/setup/gitops_deploy/files/test_gitops_deploy_fetch_skip.py

import ast
import pathlib

_SRC = pathlib.Path(__file__).with_name("gitops_deploy.py")


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
