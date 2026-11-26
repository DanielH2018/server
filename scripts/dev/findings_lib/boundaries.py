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

`launched_branches` reads the fan-out run manifests under `~/.claude/fanout`. It is a file
read, not a command, and a field for the same reason `worktree_facts` is one.
`running_batch_holds` reads the same manifests and asks each batch's host whether its unit is
still running. It does run a command, `systemctl --user is-active`, over ssh for a peer host,
and it is a field so no test reads a real unit.
`peer_worktrees` reads the other host's `git worktree list` over ssh, and `tree_held` scans
/proc for a process working in a tree. They are the two fields whose default is NOT the real
implementation: the shared test fake leaves them unset, and a default that opened ssh or read
the host's processes would make a claim test's verdict depend on the machine running it.
`findings.py`'s entry point passes `read_peer_worktrees` and
`lib.worktrees.held_by_readable_process` instead.
`sync_board` writes the Project board through `gh api graphql`. It is a field of its own so
the tests that pin the exact `gh` calls a command makes do not also pin the board's queries.
"""

import dataclasses
import subprocess
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from dev.fanout_lib.batch_holds import BatchHold, running_batch_holds
from dev.findings_lib.board import sync_board
from dev.findings_lib import milestone
from dev.findings_lib.issue_model import PR_REPO
from dev.fanout_lib.manifest import branches_launched_by
from dev.fanout_lib.target import REGISTER_CHECKOUTS
from dev.findings_lib.peer_trees import peer_worktree_list
from dev.findings_lib.tracked_paths import tracked_files
from lib.repo_paths import REPO
from lib.worktrees import Worktree, primary_checkout
from lib.worktrees import worktree_facts as read_worktree_facts
from lib.gh import gh, gh_json
from lib.json_types import JsonValue

WorktreeFacts = tuple[list[Any], Callable[[str], bool], Callable[[Any], bool], bool]


def read_peer_worktrees(repo: str | None) -> list[Worktree] | None:
    """The worktrees the other host's checkout of ``repo`` registers, or None if unreadable.

    This repo's checkout is the one `worktree_facts` reads locally, at the same path on the
    peer: one user's clone sits at one path on both hosts.
    """
    checkout = REGISTER_CHECKOUTS.get(repo) if repo else None
    return peer_worktree_list(checkout or primary_checkout() or str(REPO))


def _no_peer_worktrees(_repo: str | None) -> list[Worktree] | None:
    return []


def _never_held(_path: str) -> bool:
    return False


@dataclass(frozen=True)
class FindingsTools:
    """The gh reads, the gh writes and the worktree read.

    Each is replaceable on its own, which is what lets a test drive `claims`, `reap`, `claim`
    or `next` against invented worktree state without patching a module attribute.
    """

    gh_json: Callable[..., JsonValue] = gh_json
    gh: Callable[..., subprocess.CompletedProcess[str]] = gh
    worktree_facts: Callable[..., WorktreeFacts] = read_worktree_facts
    # The register every gh call names with `--repo`, or None for this repo. Read by
    # `gh_calls.run` and the create path, which append it to each write, so a `--dry-run`
    # prints the repo it would write to. `aimed` sets it and wraps the reads to match.
    repo: str | None = None
    launched_branches: Callable[[str, str], set[str]] = branches_launched_by
    running_batch_holds: Callable[
        [str | None, Collection[int]], Mapping[int, BatchHold]
    ] = running_batch_holds
    peer_worktrees: Callable[[str | None], list[Worktree] | None] = _no_peer_worktrees
    tree_held: Callable[[str], bool] = _never_held
    sync_board: Callable[..., dict[int, tuple[str, str]]] = sync_board
    attach_milestone: Callable[..., str | None] = milestone.attach_target
    close_idle_milestones: Callable[..., list[str]] = milestone.close_idle
    # `() -> the register's tracked files`, which `issue_model.cited_paths` resolves an
    # issue's citations against. A field so a test's `paths` never depend on this tree.
    tracked: Callable[[], Collection[str]] = tracked_files

    @property
    def owner_name(self) -> str:
        """The register's OWNER/NAME, which a `gh api` path spells out."""
        return self.repo or PR_REPO

    def batch_holds(self, issues: Collection[int]) -> Mapping[int, BatchHold]:
        """`running_batch_holds` aimed at this register, as `claim_states` takes it."""
        return self.running_batch_holds(self.repo, issues)

    def peer_trees(self) -> list[Worktree] | None:
        """`peer_worktrees` aimed at this register, as `claim_states` takes it."""
        return self.peer_worktrees(self.repo)


def aimed(tools: FindingsTools, repo: str | None) -> FindingsTools:
    """``tools`` with every gh read and write sent to ``repo``'s register.

    Reads and writes move together. A dedup read against this repo while the create went
    elsewhere re-files the finding on every run, and issue numbers collide across repos: a
    server PR saying `Closes #12` read by `next` would withhold dotfiles #12.

    The worktree read moves with them, to the checkout `REGISTER_CHECKOUTS` names, since a
    claim names a branch in the repo that owns the issue. A repo with no entry keeps this
    repo's read; `findings.main` refuses one on the subcommands that read worktrees.

    A `gh api` read is left alone: gh rejects `--repo` there, and its path names the repo.
    """
    if repo is None:
        return tools
    read, facts = tools.gh_json, tools.worktree_facts
    checkout = REGISTER_CHECKOUTS.get(repo)

    def aimed_read(*argv: str, **kw: object) -> JsonValue:
        return (
            read(*argv, **kw)
            if argv[:1] == ("api",)
            else read(*argv, "--repo", repo, **kw)
        )

    return dataclasses.replace(
        tools,
        repo=repo,
        gh_json=aimed_read,
        worktree_facts=facts if checkout is None else lambda: facts(checkout),
        # DECIDED: another repo's citations resolve against nothing, as `place launch` has
        # it: this repo's files would resolve them to paths that repo does not have.
        tracked=tools.tracked if repo.lower() == PR_REPO.lower() else frozenset,
    )
