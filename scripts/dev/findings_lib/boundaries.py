"""Every process boundary `findings.py` crosses, as one injectable object.

A test replaces one field and never a module attribute, so no function in `findings.py` is
pinned to the module a test imported it from. The defaults are the real implementations.

THREE BOUNDARIES, AND NO SHELL. `gh` and `gh_json` are the GitHub Issues register;
`worktree_facts` is the git read that decides whether a claim is still live. There is
deliberately nothing here that executes a command: a verify-by is prose describing how to check
a finding, and `findings.py verify` prints it rather than running it.

`worktree_facts` arrived fourth because its absence was measurable: `monkeypatch_allowlist.txt`
carried 8 `monkeypatch.setattr` calls across two test modules, every one of them standing in
for this missing field. A patch pins the module a test imported it from, so `cmd_claims` and
`cmd_next` could only be driven from a module attribute; injecting the read here retires all 8.
"""

import dataclasses
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.prune_worktrees import _worktree_facts
from lib.gh import gh, gh_json
from lib.json_types import JsonValue

# (worktrees, dirty, merged, ok) — `prune_worktrees._worktree_facts`'s own return, named here
# so the field below reads as one thing rather than a four-element tuple spelled out.
WorktreeFacts = tuple[list[Any], Callable[[str], bool], Callable[[Any], bool], bool]

# The registers `--repo` can aim at, each with the checkout whose worktrees decide whether a
# claim on it is live. None is this repo's own primary checkout. A repo missing here can still
# be read and written, but `claim`, `claims`, `reap` and `next` refuse it: judged against
# THIS repo's worktrees, every claim on it would read as stale and `reap` would release them.
REGISTER_CHECKOUTS: dict[str, str | None] = {
    "DanielH2018/server": None,
    "DanielH2018/dotfiles": str(_Path.home() / ".local/share/chezmoi"),
}


@dataclass(frozen=True)
class FindingsTools:
    """The gh reads, the gh writes and the worktree read.

    Each is replaceable on its own, which is what lets a test drive `claims`, `reap`, `claim`
    or `next` against invented worktree state without patching a module attribute.
    """

    gh_json: Callable[..., JsonValue] = gh_json
    gh: Callable[..., subprocess.CompletedProcess[str]] = gh
    worktree_facts: Callable[..., WorktreeFacts] = _worktree_facts
    # The register every gh call names with `--repo`, or None for this repo. Read by
    # `gh_calls.run` and the create path, which append it to each write, so a `--dry-run`
    # prints the repo it would write to. `aimed` sets it and wraps the reads to match.
    repo: str | None = None


def aimed(tools: FindingsTools, repo: str | None) -> FindingsTools:
    """``tools`` with every gh read and write sent to ``repo``'s register.

    Reads and writes move together. A dedup read against this repo while the create went
    elsewhere re-files the finding on every run, and issue numbers collide across repos: a
    server PR saying `Closes #750` read by `next` would withhold dotfiles #750.

    The worktree read moves with them, to the checkout `REGISTER_CHECKOUTS` names, since a
    claim names a branch in the repo that owns the issue. A repo with no entry keeps this
    repo's read; `findings.main` refuses one on the subcommands that read worktrees.
    """
    if repo is None:
        return tools
    read, facts = tools.gh_json, tools.worktree_facts
    checkout = REGISTER_CHECKOUTS.get(repo)
    return dataclasses.replace(
        tools,
        repo=repo,
        gh_json=lambda *argv, **kw: read(*argv, "--repo", repo, **kw),
        worktree_facts=facts if checkout is None else lambda: facts(checkout),
    )
