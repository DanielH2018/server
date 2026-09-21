"""Guard: every prek hook that runs through `uv run` carries `--frozen` (#2151).

The unattended production paths (`gitops_deploy`, `secret-rotate`, `secret-rotation-audit`)
run `uv run --frozen`, which installs the committed `uv.lock` as-is. A bare `uv run` in a
hook re-resolves instead, so the gate could pass against an environment the deployer never
builds. `uv lock --check` catches the lock drifting; this keeps the hooks resolving from the
same lock the deployer does, so the two cannot disagree on which environment a gate ran in.

Run: uv run pytest ansible/tests/repo/test_prek_uv_entries_are_frozen.py
"""

import tomllib

from _helpers import REPO

# A census that returns nothing passes vacuously; the file carries well over this many.
MIN_UV_ENTRIES = 15


def uv_run_entries(prek_toml: str) -> list[tuple[str, str]]:
    """(hook id, entry) for every hook whose entry starts with `uv run`."""
    data = tomllib.loads(prek_toml)
    return [
        (hook.get("id", "?"), hook["entry"])
        for repo in data.get("repos", [])
        for hook in repo.get("hooks", [])
        if str(hook.get("entry", "")).startswith("uv run")
    ]


def unfrozen(entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The entries whose `uv run` is not followed by `--frozen`."""
    return [
        (hook_id, entry)
        for hook_id, entry in entries
        if not entry.startswith("uv run --frozen ")
    ]


def test_every_uv_run_hook_is_frozen() -> None:
    entries = uv_run_entries((REPO / "prek.toml").read_text())
    assert len(entries) >= MIN_UV_ENTRIES, (
        f"census found only {len(entries)} `uv run` hooks"
    )
    assert unfrozen(entries) == []


def test_a_frozen_entry_is_clean() -> None:
    toml = '[[repos]]\nrepo = "local"\n[[repos.hooks]]\nid = "a"\nentry = "uv run --frozen python x.py"\n'
    assert unfrozen(uv_run_entries(toml)) == []


def test_a_bare_uv_run_entry_is_flagged() -> None:
    toml = '[[repos]]\nrepo = "local"\n[[repos.hooks]]\nid = "a"\nentry = "uv run python x.py"\n'
    assert unfrozen(uv_run_entries(toml)) == [("a", "uv run python x.py")]
