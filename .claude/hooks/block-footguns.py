#!/usr/bin/env python3
"""PreToolUse(Bash) guard: six commands that fail silently on this machine.

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

  5. A load generator aimed at the PUBLIC hostname. That name egresses to Cloudflare and comes
     back through the homelab's own CrowdSec edge, so a burst looks like an attack from this
     address. 120 requests on 2026-08-06 tripped two scenarios at once and 403'd every
     `*.daniel-hunter.com` for everyone at home. The `.local.` name stays on the LAN and still
     traverses the full route.

  6. `pgrep -f <pattern>`. The shell running the check has the pattern in its own /proc cmdline,
     so pgrep matches the waiter itself and the check reads as "still running" forever. Five
     such waiters were left spinning on 2026-08-17 and none ever fired — two of them created
     because the earlier ones seemed not to work.

Reads the hook JSON on stdin. Emits a PreToolUse "deny" decision carrying the fix; otherwise no
output -> normal permission flow. The hook can only ever DENY.
"""

from __future__ import annotations

import json
import sys
from urllib.parse import urlsplit

from _hook_common import (
    emit_pretooluse_decision,
    invokes,
    short_flags,
    split_stages,
    strip_shell_keywords,
)

# ugrep's spelling of each GNU flag people reach for. The values are what to write instead.
_UGREP_FLAG_FIXES = {
    "Z": "-Z is --fuzzy here (approximate matching), not a NUL separator. Use --null (-0).",
    "z": "-z is --decompress here, not --null-data. Use --null-data.",
}

_SSH_HOSTS = ("daniel-server", "daniel-pi", "daniel-box", "daniel-stage")
_REPO_PATH = "/home/ubuntu/server"

# Load generators, by the name you type. A single `curl` is deliberately absent: one request to
# the public name is ordinary and must stay clean, and "many requests" is not visible in the
# text of a `curl` call the way it is in the name of a tool built to make them.
_BURST_TOOLS = frozenset(
    {
        "ab",
        "wrk",
        "wrk2",
        "hey",
        "siege",
        "vegeta",
        "k6",
        "bombardier",
        "autocannon",
        "locust",
    }
)
_PUBLIC_SUFFIX = ".daniel-hunter.com"
_LAN_SUFFIX = ".local" + _PUBLIC_SUFFIX


def _host_of(word: str) -> str:
    """The hostname of `word`, whether it is a URL or a bare host argument.

    Matched on the HOST and by suffix, never as a substring of the whole argument. Both halves
    matter: `https://n8n.daniel-hunter.com/x/.local./y` contains `.local.` in its PATH, and a
    substring test read that as a LAN target and let the burst through — verified against this
    rule's first draft.
    """
    if "://" in word:
        return (urlsplit(word).hostname or "").lower()
    return word.split("/", 1)[0].split(":", 1)[0].lower()


def _is_public_target(word: str) -> bool:
    host = _host_of(word)
    return host.endswith(_PUBLIC_SUFFIX) and not host.endswith(_LAN_SUFFIX)


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


def burst_public_hostname_problem(stage: list[str]) -> str | None:
    """A load-test tool aimed at the PUBLIC hostname, which bans the homelab's own address."""
    words = strip_shell_keywords(stage)
    if not words or words[0] not in _BURST_TOOLS:
        return None
    targets = [w for w in words if _is_public_target(w)]
    if not targets:
        return None
    host = _host_of(targets[0])
    fixed = targets[0].replace(host, host.replace(_PUBLIC_SUFFIX, _LAN_SUFFIX), 1)
    return (
        f"Burst-testing {targets[0]} egresses to Cloudflare and back through the homelab's own "
        "CrowdSec edge, so it looks like an attack from this address — a 2026-08-06 run of 120 "
        "requests tripped http-crawl-non_statics and http-probing at once and 403'd every "
        f"*{_PUBLIC_SUFFIX} for everyone at home. Use the `.local.` name, which stays on the LAN "
        "and still traverses the full route: " + fixed
    )


def pgrep_self_match_problem(stage: list[str]) -> str | None:
    """`pgrep -f <pattern>` whose pattern matches the shell running it."""
    words = strip_shell_keywords(stage)
    if not words or words[0] != "pgrep":
        return None
    if "f" not in short_flags(words):
        return None
    # A character class breaks the self-match, which is the documented fix. Its presence is
    # the signal the author already knows about this.
    if any("[" in word for word in words):
        return None
    return (
        "`pgrep -f` matches the shell running it, because this command's own /proc cmdline "
        "contains the pattern — so the check reads as 'still running' forever. Five such "
        "waiters were left spinning on 2026-08-17 and none ever fired. Wait on the thing "
        "itself: prefer `run_in_background: true` and let the harness notify on exit, or match "
        "the PID (`while kill -0 <pid> 2>/dev/null`), or break the self-match with a character "
        "class: `pgrep -f 'b2_[w]ipe_prefixes'`."
    )


_RULES = (
    ugrep_flag_problem,
    bare_stash_problem,
    rollout_restart_problem,
    remote_git_problem,
    burst_public_hostname_problem,
    pgrep_self_match_problem,
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
