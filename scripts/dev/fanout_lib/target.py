"""Which repo a batch works: its register, its primary checkout and the branch it merges to.

`fanout_place.py launch --repo` picks one. Every repo except this one is read from
`REGISTER_CHECKOUTS` in `scripts/dev/findings_lib/boundaries.py`, the table `findings.py --repo`
already judges claims against, so a repo the dispatcher can launch in is exactly a repo whose
claims `reap` can judge. There is no second list to keep in step.
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from lib.repo_paths import PRIMARY_CHECKOUT

SERVER = "DanielH2018/server"
# The agent user cannot read /home/ubuntu, and its login profile
# (roles/setup/claude_code/templates/agent-user-profile.j2) points RUN_HOOK_PROJECT_DIR at its
# own clone instead (#3627). The repo's hook shim, .claude/hooks/run-hook.sh, reads the same
# variable. Unset, the checkout is the primary one this module lives under, which is the
# operator's /home/ubuntu/server for `ubuntu`.
PROJECT_DIR_ENV = "RUN_HOOK_PROJECT_DIR"


def server_checkout(env: Mapping[str, str] = os.environ) -> str:
    """This repo's primary checkout for the running user: its own clone, or the operator's."""
    return env.get(PROJECT_DIR_ENV) or str(PRIMARY_CHECKOUT)


SERVER_CHECKOUT = server_checkout()


@dataclass(frozen=True)
class Target:
    """One repo a batch can be launched in.

    Attributes:
        repo: the GitHub `OWNER/NAME`, which every `gh` call about the batch names.
        checkout: the primary checkout's absolute path. It is the same path on every host
            for one user, and the batch's worktree goes under its `.claude/worktrees/`. The
            server repo's checkout differs between the operator and the agent user.
        base: the remote default branch the worktree starts from and merges into, such as
            `origin/master`.
    """

    repo: str
    checkout: str
    base: str

    @property
    def is_server(self) -> bool:
        """Whether this is the homelab repo, the only one `land.sh` and the deployer serve."""
        return self.repo == SERVER

    @property
    def base_branch(self) -> str:
        """`base` without its remote, the name `git fetch origin <branch>` takes."""
        return self.base.split("/", 1)[1]

    @property
    def unit_prefix(self) -> str:
        """The prefix of every transient unit a batch in this repo runs under.

        A batch id is only issue numbers, and issue numbers collide across repos, so a
        dotfiles batch `763` and a server batch `763` would otherwise both be `fanout-763`
        and the second `systemd-run` would fail with "unit already exists".
        """
        return "fanout" if self.is_server else f"fanout-{self.repo.split('/', 1)[1]}"


SERVER_TARGET = Target(SERVER, SERVER_CHECKOUT, "origin/master")

# The agent user's login profile (roles/setup/claude_code/templates/agent-user-profile.j2) sets
# this to `claude_code_agent_worktree_prefix`. The operator's environment leaves it unset.
WORKTREE_PREFIX_ENV = "CLAUDE_WORKTREE_PREFIX"


def branch_name(batch: str) -> str:
    """The branch a batch's worktree is created on, claimed under and pushed from.

    The agent user's GitHub account may push only `worktree-<prefix>+**` (the "agent branch
    fence" ruleset, #3618), the branch EnterWorktree gives a `<prefix>/<slug>` worktree. When
    the running user's environment names a prefix, the batch branch takes that shape.
    Otherwise it is `worktree-fanout-<batch>`.
    """
    prefix = os.environ.get(WORKTREE_PREFIX_ENV, "")
    return f"worktree-{prefix}+fanout-{batch}" if prefix else f"worktree-fanout-{batch}"


def registered_repos() -> list[str]:
    """Every repo `--repo` accepts: the registers `findings.py` can judge claims for."""
    from dev.findings_lib.boundaries import REGISTER_CHECKOUTS

    return sorted(REGISTER_CHECKOUTS)


def resolve(
    repo: str, default_ref: Callable[[str], str | None] | None = None
) -> Target:
    """The target for `repo`.

    Args:
        repo: a key of `REGISTER_CHECKOUTS`.
        default_ref: reads a checkout's remote default branch; a seam, defaulting to the
            `claude_worktree` reader `findings.py` uses for the same question.

    Raises:
        ValueError: `repo` is not a register `findings.py` knows, or its checkout has no
            default branch to read. Without a merge target, `clean` could never show a
            batch landed.
    """
    if repo == SERVER:
        return SERVER_TARGET
    from dev.findings_lib.boundaries import REGISTER_CHECKOUTS

    if repo not in REGISTER_CHECKOUTS:
        raise ValueError(f"{repo} is not a register findings.py can judge claims for")
    checkout = REGISTER_CHECKOUTS[repo]
    if checkout is None:
        raise ValueError(f"{repo} has no checkout in REGISTER_CHECKOUTS")
    if default_ref is None:
        from lib.worktrees import default_ref
    base = default_ref(checkout)
    if not base:
        raise ValueError(f"{checkout} has no remote default branch to merge into")
    return Target(repo, checkout, base)


def for_launch(
    repo: str,
    host: str | None,
    local_host: str,
    default_ref: Callable[[str], str | None] | None = None,
) -> tuple[Target, str | None]:
    """The target `launch --repo` names, and the host its batches are pinned to.

    Another repo's batches are pinned to `local_host`. `findings.py` judges a claim in that
    register against this host's checkout of the repo, so a tree on any other host reads as
    gone, and `reap` would release the claim while the agent works.

    Args:
        repo: the `--repo` value.
        host: the `--host` value, or None to let placement choose.
        local_host: the host `launch` runs on.
        default_ref: as `resolve` takes it.

    Returns:
        The target, and the host to pin to: `host` unchanged for this repo.

    Raises:
        ValueError: `resolve` refused the repo, or `host` names a host other than
            `local_host` for another repo's batch.
    """
    target = resolve(repo, default_ref)
    if target.is_server:
        return target, host
    if host and host != local_host:
        raise ValueError(
            f"a {target.repo} batch runs on this host ({local_host}) only — findings.py "
            f"judges its claim against this host's checkout of {target.checkout}"
        )
    return target, local_host
