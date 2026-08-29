#!/usr/bin/env python3
"""PreToolUse(Bash) guard: four commands that fail silently on this machine.

Each has a deterministic signature, a recorded cost, and a one-line fix — which is what makes
them worth a hook rather than a paragraph. What they share is that none of them ERRORS: every
one produces a plausible-looking result that is wrong, so nothing downstream notices.

  1. `grep -Z` / `grep -z`. This host's grep is ugrep 7.8, where `-Z` is `--fuzzy` (approximate
     matching, not a NUL separator) and `-z` is `--decompress` (not `--null-data`). A
     `grep -lZ ... | xargs -0` therefore reads as a NUL-safe sweep and is neither: it fuzzy-
     matches, and emits newline-separated names. One such sweep rewrote part of this tree and
     left 49 stale paths behind, with every gate green. The NUL flags here are `--null` and
     `--null-data`.

  2. `git stash pop` / `apply` with no ref. The stash stack is per-REPOSITORY, not per-worktree,
     so a bare pop in one worktree takes whatever another session pushed last. It applied
     another session's 25-file work-in-progress into the wrong tree. Pop by explicit ref.

  3. `kubectl rollout restart`. Plain kubectl authenticates as the read-only ServiceAccount,
     which is Forbidden on this verb — and a Forbidden restart still prints "successfully
     rolled out". The refusal and the success read identically. Ansible is the write path.

  4. `ssh daniel-<host> '<git ...>'` with no `cd`. A non-interactive ssh lands in $HOME, not the
     repo, so the git command runs somewhere else entirely — usually reporting "not a git
     repository", sometimes finding a different repo.

Reads the hook JSON on stdin. Emits a PreToolUse "deny" decision carrying the fix; otherwise no
output -> normal permission flow. The hook can only ever DENY.
"""

from __future__ import annotations

import json
import sys

from _hook_common import emit_pretooluse_decision, invokes, short_flags, split_stages

# ugrep's spelling of each GNU flag people reach for. The values are what to write instead.
_UGREP_FLAG_FIXES = {
    "Z": "-Z is --fuzzy here (approximate matching), not a NUL separator. Use --null (-0).",
    "z": "-z is --decompress here, not --null-data. Use --null-data.",
}

_SSH_HOSTS = ("daniel-server", "daniel-pi", "daniel-box", "daniel-stage")
_REPO_PATH = "/home/ubuntu/server"


def ugrep_flag_problem(stage: list[str]) -> str | None:
    """A GNU grep flag whose ugrep meaning is different and silent."""
    if not stage or stage[0] != "grep":
        return None
    for letter in sorted(short_flags(stage) & set(_UGREP_FLAG_FIXES)):
        return f"This host's grep is ugrep, not GNU grep. {_UGREP_FLAG_FIXES[letter]}"
    return None


def bare_stash_problem(stage: list[str]) -> str | None:
    """`git stash pop`/`apply` with no explicit stash ref."""
    if not (
        invokes(stage, ("git", "stash", "pop"))
        or invokes(stage, ("git", "stash", "apply"))
    ):
        return None
    if any(word.startswith("stash@") for word in stage):
        return None
    return (
        "The git stash stack is per-repository, not per-worktree, so a bare pop can apply "
        "another session's work-in-progress into this tree. Run `git stash list` and pop the "
        "ref you meant: `git stash pop 'stash@{0}'`."
    )


def rollout_restart_problem(stage: list[str]) -> str | None:
    """`kubectl rollout restart`, which is Forbidden here and prints success anyway."""
    if not invokes(stage, ("kubectl", "rollout", "restart")):
        return None
    return (
        "Plain kubectl authenticates as the read-only ServiceAccount, which is Forbidden on "
        'rollout restart — and a Forbidden restart still prints "successfully rolled out", so '
        "the refusal and a real restart are indistinguishable. Deploy the role instead: "
        "`./scripts/deploy.sh --tags <service>`."
    )


def remote_git_problem(stage: list[str]) -> str | None:
    """`ssh daniel-<host> '<git ...>'` with no cd into the repo."""
    if not stage or stage[0] != "ssh":
        return None
    if not any(host in stage for host in _SSH_HOSTS):
        return None
    # The remote command is one argument after the host, so the git call is inside a single
    # token rather than split across the stage.
    remote = " ".join(word for word in stage[1:] if word not in _SSH_HOSTS)
    if not remote.lstrip().startswith("git "):
        return None
    if _REPO_PATH in remote or remote.lstrip().startswith("cd "):
        return None
    return (
        "A non-interactive ssh starts in $HOME, not the repo, so this git command runs "
        f"somewhere else. Prefix it: `cd {_REPO_PATH}; <git ...>`."
    )


_RULES = (
    ugrep_flag_problem,
    bare_stash_problem,
    rollout_restart_problem,
    remote_git_problem,
)


def problem(command: str) -> str | None:
    """The first footgun this command trips, or None."""
    for stage in split_stages(command):
        for rule in _RULES:
            found = rule(stage)
            if found:
                return found
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        return 0
    found = problem(command)
    if found:
        emit_pretooluse_decision("deny", found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
