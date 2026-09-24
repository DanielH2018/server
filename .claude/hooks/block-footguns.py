#!/usr/bin/env python3
# gen-hooks: library
#   reason: an arm of bash-pretool.py, which bash-pretool.sh runs through `uv run python`
"""PreToolUse(Bash) guard: five commands that fail silently on this machine.

Each has a deterministic signature, a recorded cost, and a one-line fix — which is what makes
them worth a hook rather than a paragraph. What they share is that none of them ERRORS: seven
produce a plausible-looking result that is wrong, and the last two succeed outright while
bypassing a gate the repo requires, so nothing downstream notices either way.

  1. `kubectl rollout restart`. Plain kubectl authenticates as the read-only ServiceAccount,
     which is Forbidden on this verb — and a Forbidden restart still prints "successfully
     rolled out". The refusal and the success read identically. Ansible is the write path.

  2. `ssh daniel-<host> '<git ...>'` with no `cd`. A non-interactive ssh lands in $HOME, not the
     repo, so the git command runs somewhere else entirely — usually reporting "not a git
     repository", sometimes finding a different repo.

  3. A load generator aimed at the PUBLIC hostname. That name egresses to Cloudflare and comes
     back through the homelab's own CrowdSec edge, so a burst looks like an attack from this
     address. 120 requests on 2026-08-06 tripped two scenarios at once and 403'd every
     `*.daniel-hunter.com` for everyone at home. The `.local.` name stays on the LAN and still
     traverses the full route.

  4. `gh issue create` by hand. The issue lands, but without the `claude` label, the
     fingerprint trailer and the title/file dedup that `findings.py open` supplies — so the
     register's `list`, `next` and re-observation matching never see it, and a second session
     files the same finding again. CLAUDE.md said "never by hand" and nothing enforced it
     (#2160). `findings.py` itself calls `gh` from Python, which never reaches this hook, so
     there is no exemption to write.

  5. `deploy.sh --skip-staleness-check` typed by a session. deploy.sh refuses a tree behind
     `origin/master` (exit 4, nothing deployed) because a stale tree renders stale templates
     and reverts live config while every repo-side check reads green — and the flag makes that
     deploy succeed with a green recap. The deploy skill and `docs/deploying.md` said "never";
     `auto-mode-bridge.py` nudged in prose; nothing denied it (#2170). The one correct use is
     INSIDE `scripts/deploy_tools/staging_gate_remote.sh`, whose tree is pinned behind master
     by construction: that flag is in the script's own text, never in a Bash tool command, so
     the script's invocation needs no exemption here.

Four more rules lived here until dotfiles #628 moved them into `claude_guard.footguns`, which
every repo's PreToolUse hook runs: ugrep's `-Z`/`-z`, a bare `git stash pop`, a self-matching
`pgrep -f` and a partial `security_and_analysis` PATCH. They key on a host tool or on
GitHub, not on this repo.

Reads the hook JSON on stdin. Emits a PreToolUse "deny" decision carrying the fix; otherwise no
output -> normal permission flow. The hook can only ever DENY.
"""

import json
import re
import sys
from urllib.parse import urlsplit

from _hook_common import (
    Unsplittable,
    emit_pretooluse_decision,
    invokes,
    split_stages,
    strip_shell_keywords,
)

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


def issue_create_by_hand_problem(stage: list[str]) -> str | None:
    """`gh issue create` typed directly, bypassing `findings.py open`.

    Matched on the argv (`invokes` tolerates a global flag such as `--repo` before the
    subcommand), never as a substring, so `gh issue list --search 'gh issue create'` stays
    clean. `findings.py open` runs `gh` through `subprocess` inside Python, so its own call
    never reaches a PreToolUse(Bash) hook and needs no exemption here.
    """
    words = strip_shell_keywords(stage)
    if not invokes(words, ("gh", "issue", "create")):
        return None
    return (
        "A hand-filed `gh issue create` lands without the `claude` label, the fingerprint "
        "trailer and the title/file dedup the register keys on, so `findings.py list`/`next` "
        "never see it and a later session files the same finding again. File it with "
        "`uv run python scripts/dev/findings.py open --title '<title>' ...` instead "
        "(flags: docs/reference/scripts.md)."
    )


_STALENESS_FLAG = "--skip-staleness-check"


def skip_staleness_problem(stage: list[str]) -> str | None:
    """`deploy.sh --skip-staleness-check` typed as a command.

    Keyed on the command word's basename so `./scripts/deploy.sh`, `scripts/deploy.sh` and
    the absolute path all match, and so a `grep`/`sed` naming the flag as an argument stays
    clean. `staging_gate_remote.sh` passes the flag from inside its own text; a session's
    invocation of it carries no flag, so it never reaches this rule.
    """
    words = strip_shell_keywords(stage)
    names = [word.rsplit("/", 1)[-1] for word in words]
    # The shim execs `deploy_run.py` (#2412), so an interpreter handed that module is the same
    # deploy by another door. Keyed on the interpreter as the command word, so a `grep` that
    # names the module and the flag as arguments stays clean.
    runs_the_module = (
        names[:1]
        and names[0] in ("uv", "python", "python3")
        and ("deploy_run.py" in names)
    )
    if not names or (names[0] != "deploy.sh" and not runs_the_module):
        return None
    if _STALENESS_FLAG not in words[1:]:
        return None
    return (
        f"`deploy.sh {_STALENESS_FLAG}` deploys a tree behind origin/master: stale templates "
        "render and live config reverts while every check reads green. Pull first (a landing "
        "goes through `land.sh --at <sha>`), then deploy without the flag. The only sanctioned "
        "use is inside scripts/deploy_tools/staging_gate_remote.sh, whose tree is pinned "
        "behind master on purpose."
    )


_RULES = (
    rollout_restart_problem,
    remote_git_problem,
    burst_public_hostname_problem,
    issue_create_by_hand_problem,
    skip_staleness_problem,
)


# The binaries the rules key on, as words in the raw text. The gate for asking when the
# splitter cannot read the command: text naming none of them cannot reach a rule however it
# is split, so a typo in a command about something else costs no prompt, and a host without
# the `claude_guard` deploy prompts only on the commands this hook exists for.
_RULE_BINARIES = frozenset({"kubectl", "ssh", "gh"}) | _BURST_TOOLS
_RULE_BINARY_RE = re.compile(
    r"(?<![\w/.-])(" + "|".join(sorted(_RULE_BINARIES)) + r")(?![\w.-])"
)


def could_fire(command: str) -> bool:
    """True if `command` names a binary one of `_RULES` keys on, read off the raw text.

    Rule 9 keys on a flag rather than a binary: `deploy.sh` is almost always typed behind a
    `./scripts/` path, which the regex's lookbehind refuses, so the flag literal is the gate.
    """
    return _RULE_BINARY_RE.search(command) is not None or _STALENESS_FLAG in command


def problem(command: str) -> str | None:
    """The first footgun this command trips, or None.

    Leading shell keywords are stripped ONCE here rather than per rule. Every rule decides on
    the first word — `stage[0]` directly, or `invokes()`, which does the same — so a keyword
    in front of the binary made all four of the original rules miss: measured 2026-08-30,
    `! git stash pop`, `time git stash pop`, `! kubectl rollout restart …` and `command grep
    -Z …` were each allowed while the bare form was denied. `! git stash pop` is the one that
    matters: a bare pop can apply another session's work-in-progress into this tree, and a
    negation is exactly what someone writes when they expect the pop to fail.

    Stripping at the dispatch site rather than in each rule means a rule added later inherits
    it instead of having to remember. The two rules that already call `strip_shell_keywords`
    keep the call — it is idempotent, and a rule that only works when its caller strips first
    is a trap for whoever reuses it.
    """
    return _first_problem(split_stages(command))


def _first_problem(stages: list[list[str]]) -> str | None:
    for stage in stages:
        words = strip_shell_keywords(stage)
        for rule in _RULES:
            found = rule(words)
            if found:
                return found
    return None


def decide(command: str, split=split_stages) -> tuple[str, str] | tuple[None, None]:
    """The (decision, reason) pair for `command`, or (None, None).

    A `deny` carries what `problem` found. An `ask` is emitted where the splitter cannot read
    a command that names a rule's binary: the package's contract for a non-ok parse is a
    refusal, never "nothing here". It is gated on `could_fire` the way
    `block-protected-bash.py` gates its own on a writer shape — a deny guard that prompts on
    every unreadable command, or on every command where the package is not deployed, is a
    guard the operator turns off.
    """
    try:
        stages = split(command)
    except Unsplittable as exc:
        if not could_fire(command):
            return None, None
        if exc.missing:
            return (
                "ask",
                f"block-footguns cannot split this command: the `claude_guard` package is "
                f"not deployed ({exc.detail}), so none of its rules ran. Run `chezmoi apply` "
                f"on this host, or review the command against the rules yourself.",
            )
        return (
            "ask",
            f"block-footguns cannot read this command ({exc.status}), so none of its rules "
            f"ran. Fix the quoting, or review the command against the rules yourself.",
        )
    found = _first_problem(stages)
    if found:
        return "deny", found
    return None, None


def decision(payload) -> tuple[str, str] | None:
    """The `(decision, reason)` pair `decide` returns for this payload, or None.

    The arm entry point `bash-pretool.py` calls. `main()` below is the same arm run as its
    own process, which is what the tests and a hand invocation use.
    """
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        return None
    verdict, reason = decide(command)
    return (verdict, reason) if verdict and reason else None


def main() -> int:
    """Read the hook payload from stdin and emit the decision `decision` returns.

    A deny names the flagged footgun and its fix; an ask names why the rules could not run.
    Otherwise emits nothing. Always returns 0 (a decision is expressed through emitted JSON,
    not the exit code).
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    verdict = decision(payload)
    if verdict:
        emit_pretooluse_decision(*verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
