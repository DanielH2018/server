# ansible/roles/setup/gitops_deploy/files/test_gitops_discord_contract.py
"""Source-level guard for gitops_deploy.discord()'s delivery contract.

gitops_deploy.py can't be imported in CI (module-level `C = cfg()` reads /etc config that doesn't
exist there — the accepted design, see the role CLAUDE.md), so its discord() I/O contract has no
behavioural test the way renovate_notify's does (test_renovate_notify.py). A regression that dropped
the Cloudflare-1010 User-Agent header or loosened the 2xx success bound would silently advance a
per-SHA dedupe marker on a FAILED post and permanently suppress a real rollback alert, with no CI
signal. This AST assertion is the narrow non-import guard: it proves both invariants still live in
the discord() body without executing the un-importable module.
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
