"""Guard: every prek hook runs at the pre-commit stage and no other (#4018).

A hook without `stages` runs at EVERY stage, so a `.git/hooks/pre-push` shim an older
`prek install` left behind re-ran every commit hook on push, fixers included. `default_stages`
in prek.toml closes that for hooks defined here. It does not reach a hook whose upstream
manifest declares `stages` itself: pre-commit-hooks v6.0.0 lists `[pre-commit, pre-push,
manual]` on trailing-whitespace, end-of-file-fixer and check-added-large-files, and those three
still ran at pre-push with `default_stages` set. So a hook from a remote repo states its own.

Run: uv run pytest ansible/tests/repo/test_prek_hooks_run_only_at_commit.py
"""

import tomllib

from _helpers import REPO

COMMIT_ONLY = ["pre-commit"]

# One of the three hooks whose upstream manifest declares pre-push. A census that stops finding
# it has stopped reading the repo the bug lives in.
NAMED_MEMBER = "trailing-whitespace"


def remote_hooks_off_commit(prek_toml: str) -> list[str]:
    """Ids of hooks from a remote repo that do not pin `stages` to pre-commit only."""
    data = tomllib.loads(prek_toml)
    return [
        hook.get("id", "?")
        for repo in data.get("repos", [])
        if repo.get("repo") not in ("local", "meta")
        for hook in repo.get("hooks", [])
        if hook.get("stages") != COMMIT_ONLY
    ]


def remote_hook_ids(prek_toml: str) -> list[str]:
    data = tomllib.loads(prek_toml)
    return [
        hook.get("id", "?")
        for repo in data.get("repos", [])
        if repo.get("repo") not in ("local", "meta")
        for hook in repo.get("hooks", [])
    ]


def test_prek_defaults_every_hook_to_the_commit_stage() -> None:
    data = tomllib.loads((REPO / "prek.toml").read_text())
    assert data.get("default_stages") == COMMIT_ONLY


def test_every_remote_hook_pins_the_commit_stage() -> None:
    text = (REPO / "prek.toml").read_text()
    assert NAMED_MEMBER in remote_hook_ids(text), (
        f"{NAMED_MEMBER} is no longer a remote hook; the census reads the wrong repos"
    )
    assert remote_hooks_off_commit(text) == []


def test_a_remote_hook_without_stages_is_flagged() -> None:
    toml = (
        '[[repos]]\nrepo = "https://example.invalid/hooks"\nrev = "v1"\n'
        '[[repos.hooks]]\nid = "a"\n'
        '[[repos.hooks]]\nid = "b"\nstages = ["pre-commit"]\n'
        '[[repos]]\nrepo = "local"\n[[repos.hooks]]\nid = "c"\n'
    )
    assert remote_hooks_off_commit(toml) == ["a"]
