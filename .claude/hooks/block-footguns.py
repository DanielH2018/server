#!/usr/bin/env python3
# gen-hooks: library
#   reason: an arm of bash-pretool.py, which run-hook.sh runs through `uv run python`
"""PreToolUse(Bash) guard: eight commands that fail silently or cost a second write here.

Usage: `bash-pretool.py` loads this file as an arm on PreToolUse:Bash, and it also runs
standalone. It reads the hook JSON payload on stdin. It prints a `deny` (or `ask`) decision JSON
object or nothing, and it exits 0. `-h` or `--help` prints this text and exits 0 without reading
stdin.

Each has a deterministic signature, a recorded cost, and a one-line fix — which is what makes
them worth a hook rather than a paragraph. What they share is that none of them ERRORS: each
either produces a plausible-looking result that is wrong or succeeds outright while bypassing
a gate the repo requires, so nothing downstream notices either way.

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
     `auto-mode-bridge.py` nudged in prose; nothing denied it. There is no correct use
     left: the one exemption was inside the staging gate's runner, whose tree was pinned behind
     master by construction, and that runner went with the daniel-stage guest.

  6. `gh workflow run ci.yml` with no `--ref`. gh resolves an empty ref to the default branch,
     so the dispatch sweeps master's HEAD — and `deploy_git.ci_verdict` applies worst-wins over
     every required check-run on a SHA with no event filter, while GitHub's `filter=latest`
     does not merge same-named runs across check suites. So one red dispatch run sits beside
     the green push run and pins that master SHA's verdict to fail, which stops the GitOps
     deployer's walk at it. The dispatch itself reports nothing wrong.

  7. `gh pr create` or `gh pr edit` with a body that skips a `## ` heading of
     `.github/pull_request_template.md`. Nothing fails: the PR opens, and the agent reads the
     template afterwards and rewrites the body to fit it, which was 40 of the PR body edits
     read for #3963. Denying the create moves the fit to before the PR exists.

  8. An OCI tag move is denied, and a write that clears `DEPLOY_HOLD` or writes `PROD_DIGEST`
     asks the operator. Setting a hold passes; `_deploy_plane_rule.py` has the rule.

Rules that key on a host tool or on GitHub rather than on this repo live in
`claude_guard.footguns`, which every repo's PreToolUse hook runs.

Reads the hook JSON on stdin. Emits a PreToolUse "deny" carrying the fix, or rule 8's "ask";
otherwise no output -> normal permission flow.
"""

import functools
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from _hook_common import (
    Unsplittable,
    emit_pretooluse_decision,
    gh_repo,
    invokes,
    read_payload,
    split_stages,
    strip_shell_keywords,
)
from _deploy_plane_rule import TAG_MOVES, deploy_plane_variable_ask, tag_move_problem

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
# The checkout on the REMOTE host: the guard fires on `ssh daniel-<host> git ...`, and each
# `ansible_connection=local` host keeps its own operator checkout at this path.
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


@functools.cache
def _ssh_hosts() -> tuple[str, ...] | None:
    """Every host `hosts.ini` declares, read through the one parser `scripts/lib` shares.

    Derived rather than listed: a hand-kept tuple here still named daniel-stage weeks after its
    retirement (#3705). Deferred to the first `ssh` stage so the hot path never pays for the
    import.

    Returns:
        The host names, or None when `scripts/lib` or the inventory cannot be read beside this
        file. None is distinct from an empty inventory: `remote_git_problem` then matches every
        ssh host rather than none.
    """
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    try:
        from lib.ansible_inventory import host_names

        return host_names()
    except Exception:
        return None


def _remote_command(stage: list[str], hosts: tuple[str, ...] | None) -> str | None:
    """The command an `ssh` stage runs on the far side, or None when it names no known host.

    The remote command is one argument after the host, so the git call is inside a single
    token rather than split across the stage. With no inventory (`hosts` None) the host cannot
    be picked out, so the command starts at the first word that is a git call.
    """
    words = stage[1:]
    if hosts is not None:
        if not any(host in words for host in hosts):
            return None
        return " ".join(word for word in words if word not in hosts)
    starts = (i for i, w in enumerate(words) if w.lstrip().startswith("git "))
    start = next(starts, None)
    return None if start is None else " ".join(words[start:])


def remote_git_problem(stage: list[str]) -> str | None:
    """`ssh daniel-<host> '<git ...>'` with no cd into the repo.

    With no inventory the rule fails closed for this one shape (#4420). A review pipeline's held
    hook copy once had no `scripts/` beside it, so the inventory import failed, and the rule
    went inert without a word. Matching every host instead keeps the rule live. Nothing becomes
    unrunnable, because a remote git that cds into the repo still passes.
    """
    if not stage or stage[0] != "ssh":
        return None
    hosts = _ssh_hosts()
    remote = _remote_command(stage, hosts)
    if remote is None or not remote.lstrip().startswith("git "):
        return None
    if _REPO_PATH in remote or remote.lstrip().startswith("cd "):
        return None
    unread = (
        ""
        if hosts is not None
        else (
            f" This guard could not read the inventory through {_SCRIPTS}/lib, so it "
            "matched this git command on any ssh host rather than only the homelab's."
        )
    )
    return (
        "A non-interactive ssh starts in $HOME, not the repo, so this git command runs "
        f"somewhere else. Prefix it: `cd {_REPO_PATH}; <git ...>`." + unread
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

    A create aimed at another repo is denied too, and the reason names `findings.py open
    --repo` with that repo. The wrapper files there with the same labels and fingerprint
    dedup, so passing the hand-filed form would give that repo's register the duplicates
    this rule prevents here.
    """
    words = strip_shell_keywords(stage)
    if not invokes(words, ("gh", "issue", "create")):
        return None
    repo = gh_repo(words)
    repo_arg = f" --repo {repo}" if repo else ""
    return (
        "A hand-filed `gh issue create` lands without the `claude` label, the fingerprint "
        "trailer and the title/file dedup the register keys on, so `findings.py list`/`next` "
        "never see it and a later session files the same finding again. File it with "
        f"`uv run python scripts/dev/findings.py open{repo_arg} --title '<title>' ...` "
        "instead (flags: `findings.py open --help`)."
    )


_STALENESS_FLAG = "--skip-staleness-check"


def skip_staleness_problem(stage: list[str]) -> str | None:
    """`deploy.sh --skip-staleness-check` typed as a command.

    Keyed on the command word's basename so `./scripts/deploy.sh`, `scripts/deploy.sh` and
    the absolute path all match, and so a `grep`/`sed` naming the flag as an argument stays
    clean.
    """
    words = strip_shell_keywords(stage)
    names = [word.rsplit("/", 1)[-1] for word in words]
    # The shim execs `deploy_cli.py`, so an interpreter handed that module is the same
    # deploy by another door. `deploy_run.py` is what the shim execed before #4347, and an
    # older checkout still carries it. Keyed on the interpreter as the command word, so a
    # `grep` that names the module and the flag as arguments stays clean.
    runs_the_module = (
        names[:1]
        and names[0] in ("uv", "python", "python3")
        and ("deploy_cli.py" in names or "deploy_run.py" in names)
    )
    if not names or (names[0] != "deploy.sh" and not runs_the_module):
        return None
    if _STALENESS_FLAG not in words[1:]:
        return None
    return (
        f"`deploy.sh {_STALENESS_FLAG}` deploys a tree behind origin/master: stale templates "
        "render and live config reverts while every check reads green. Pull first (a landing "
        "goes through `land.sh --at <sha>`), then deploy without the flag."
    )


# The CI workflow, by every name `gh workflow run` accepts for it: the file, either extension,
# and the `name:` in it. NOT the numeric workflow id — gh accepts that too, and a literal here
# would drift the first time the workflow is recreated. Stated as a limit in the rule's
# docstring rather than papered over.
_CI_WORKFLOW_NAMES = frozenset({"ci.yml", "ci.yaml", "CI"})
# The refs that mean "master". An absent `--ref` means the same thing: gh resolves an empty ref
# to the repository's default branch (`pkg/cmd/workflow/run/run.go`).
_MASTER_REFS = frozenset({"master", "refs/heads/master"})


def _ref_arg(words: list[str]) -> str | None:
    """The `--ref` value in `words`, or None when the command names none.

    Both spellings of both forms, because gh takes all four: `--ref X`, `--ref=X`, `-r X`,
    `-r=X`. A flag with nothing after it reads as absent, which denies — the same answer as
    the bare form, and the one that fails safe.
    """
    for i, word in enumerate(words):
        if word in ("--ref", "-r"):
            return words[i + 1] if i + 1 < len(words) else None
        for prefix in ("--ref=", "-r="):
            if word.startswith(prefix):
                return word[len(prefix) :]
    return None


def ci_dispatch_on_master_problem(stage: list[str]) -> str | None:
    """`gh workflow run ci.yml` aimed at master, which pins that SHA's CI verdict to red.

    The dispatch run lands as a second check suite on the same SHA. `ci_verdict` takes the
    worst conclusion over every required-name check-run and filters on no event, and GitHub's
    default `filter=latest` does not merge same-named runs across suites — so a red dispatch
    beside a green push run reads as fail, and the deployer's walk stops at that SHA.

    Denies the ABSENT ref as well as the explicit one: an empty `--ref` resolves to the default
    branch, which is master here. A dispatch of a real branch stays clean, because a branch SHA
    is not one the deployer walks.

    Out of scope, deliberately: the workflow's numeric id (a literal here would drift), the
    Actions UI button, and a raw `gh api .../dispatches` call. The issue that filed this
    conceded all three; the alternative that covers them needs an `actions/runs?head_sha=`
    join on every verdict read and doubles the anonymous-quota requests.
    """
    words = strip_shell_keywords(stage)
    if not invokes(words, ("gh", "workflow", "run")):
        return None
    if not any(word in _CI_WORKFLOW_NAMES for word in words[1:]):
        return None
    ref = _ref_arg(words)
    if ref is not None and ref not in _MASTER_REFS:
        return None
    return (
        "A `gh workflow run ci.yml` with no --ref sweeps master's HEAD, and gh resolves an "
        "empty ref to the default branch. The dispatch run lands as a second check suite on "
        "that SHA, deploy_git.ci_verdict takes the worst conclusion over every required "
        "check-run with no event filter, and one red dispatch beside the green push run pins "
        "the SHA to fail — which stops the GitOps deployer's walk there. Dispatch the BRANCH: "
        "`gh workflow run ci.yml --ref <your-branch>`."
    )


_OWN_REPO = "danielh2018/server"
_PR_TEMPLATE = Path(".github") / "pull_request_template.md"
_HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_BODY_FLAGS = ("--body", "-b")
_BODY_FILE_FLAGS = ("--body-file", "-F")
_FILL_FLAGS = ("--fill", "-f", "--fill-first", "--fill-verbose")


def template_headings(root: Path) -> list[str]:
    """The `## ` headings of the PR template under `root`, in order, or [] when it has none."""
    try:
        text = (root / _PR_TEMPLATE).read_text(encoding="utf-8")
    except OSError:
        return []
    return _HEADING_RE.findall(text)


def _flag_value(words: list[str], flags: tuple[str, ...]) -> str | None:
    """The value of the last of `flags` in `words`, in either `--flag X` or `--flag=X` form."""
    value = None
    for i, word in enumerate(words):
        if word in flags and i + 1 < len(words):
            value = words[i + 1]
        for flag in flags:
            if flag.startswith("--") and word.startswith(flag + "="):
                value = word[len(flag) + 1 :]
    return value


def _repo_root(start: Path) -> Path | None:
    """The nearest directory at or above `start` holding a `.git` entry, or None."""
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _names_another_repo(words: list[str]) -> bool:
    """True when the stage aims at a repo other than this one, by flag or by PR URL."""
    repo = gh_repo(words)
    if repo is not None and not repo.lower().rstrip("/").endswith(_OWN_REPO):
        return True
    urls = [w for w in words if w.startswith("https://github.com/")]
    return any(f"github.com/{_OWN_REPO}/" not in w.lower() for w in urls)


def pr_body_problem(stage: list[str], cwd: Path, command: str) -> str | None:
    """`gh pr create`/`gh pr edit` with a body that skips a heading of the PR template.

    Agents opened the PR first and read `.github/pull_request_template.md` after, then rewrote
    the body to fit it: 40 of the PR body edits in the transcripts read for #3963. Refusing the
    create moves that fit to before the PR exists, where it costs a file edit rather than a
    second write to GitHub. The edit is held to the same headings so it is no way around them.

    The headings are read from the template of the repo `cwd` is in, so a repo with no
    template is never judged. The rule fails open where the body cannot be read: a relative
    `--body-file` that does not resolve (gh refuses that itself), a stage aimed at another repo
    with `--repo` or a PR URL, and a create or edit that passes no body at all. `--body-file -`
    reads the here-document, which the splitter drops, so that case judges the raw command
    text. `--fill` with no body fills from the commits and skips the template, so it is denied.

    Args:
        stage: The stage's words, shell keywords stripped.
        cwd: The directory the stage runs in, after any `cd` earlier in the command.
        command: The raw command text, for a body read from stdin.
    """
    create = invokes(stage, ("gh", "pr", "create"))
    if not (create or invokes(stage, ("gh", "pr", "edit"))):
        return None
    if _names_another_repo(stage):
        return None
    root = _repo_root(cwd)
    if root is None:
        return None
    headings = template_headings(root)
    if not headings:
        return None
    body = _flag_value(stage, _BODY_FLAGS)
    body_file = _flag_value(stage, _BODY_FILE_FLAGS)
    if body_file == "-":
        body = command
    elif body_file is not None:
        try:
            body = (cwd / body_file).read_text(encoding="utf-8")
        except OSError:
            return None
    if body is None:
        if not (create and any(word in _FILL_FLAGS for word in stage)):
            return None
        return (
            "`gh pr create --fill` with no body takes the body from the commits and skips "
            f"{root / _PR_TEMPLATE}, so the body gets rewritten after the PR opens (#3963). "
            "Write the template's sections to a file and pass it as well: "
            "`gh pr create --fill --body-file <path>`. gh keeps the commit title and uses the "
            "file for the body."
        )
    present = {heading.strip() for heading in _HEADING_RE.findall(body)}
    missing = [heading for heading in headings if heading not in present]
    if not missing:
        return None
    names = ", ".join(f"`## {heading}`" for heading in missing)
    return (
        f"This PR body skips {len(missing)} section(s) of {root / _PR_TEMPLATE}: {names}. "
        "Fit the body to the template before the PR opens rather than rewriting it after "
        "(#3963). Keep every `## ` heading verbatim and fill or delete the comments under "
        "each, then rerun the same command."
    )


_RULES = (
    rollout_restart_problem,
    remote_git_problem,
    burst_public_hostname_problem,
    issue_create_by_hand_problem,
    tag_move_problem,
    skip_staleness_problem,
    ci_dispatch_on_master_problem,
)


# The binaries the rules key on, as words in the raw text. The gate for asking when the
# splitter cannot read the command: text naming none of them cannot reach a rule however it
# is split, so a typo in a command about something else costs no prompt, and a host without
# the `claude_guard` deploy prompts only on the commands this hook exists for.
_RULE_BINARIES = (
    frozenset({"kubectl", "ssh", "gh", *(m[0] for m in TAG_MOVES)}) | _BURST_TOOLS
)
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
    in front of the binary (`! git stash pop`, `time git stash pop`, `command grep -Z …`)
    would make a rule miss. `! git stash pop` is the one that matters: a bare pop can apply
    another session's work-in-progress into this tree, and a negation is exactly what someone
    writes when they expect the pop to fail.

    Stripping at the dispatch site rather than in each rule means a rule added later inherits
    it instead of having to remember. The two rules that already call `strip_shell_keywords`
    keep the call — it is idempotent, and a rule that only works when its caller strips first
    is a trap for whoever reuses it.
    """
    return _first_problem(split_stages(command), command=command)


def _first_problem(
    stages: list[list[str]], cwd: str | None = None, command: str = ""
) -> str | None:
    """The first footgun across `stages`, or None.

    `cwd` is where the command starts, the hook payload's own. A `cd <dir>` stage moves it for
    the stages after it, which is what `pr_body_problem` resolves a relative body file against.
    """
    here = Path(cwd or Path.cwd())
    for stage in stages:
        words = strip_shell_keywords(stage)
        if len(words) == 2 and words[0] == "cd":
            here = here / Path(words[1]).expanduser()
            continue
        for rule in _RULES:
            found = rule(words)
            if found:
                return found
        found = pr_body_problem(words, here, command)
        if found:
            return found
    return None


def decide(
    command: str, split=split_stages, cwd: str | None = None
) -> tuple[str, str] | tuple[None, None]:
    """The (decision, reason) pair for `command`, or (None, None).

    A `deny` carries what `problem` found, and an `ask` what `deploy_plane_variable_ask` found.
    An `ask` is also emitted where the splitter cannot read a command naming a rule's binary,
    since a non-ok parse is a refusal, never "nothing here". It is gated on `could_fire` as
    `block-protected-bash.py` gates its own on a writer shape: a guard that prompts on every
    unreadable command, or wherever the package is not deployed, is one the operator turns off.
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
    found = _first_problem(stages, cwd=cwd, command=command)
    if found:
        return "deny", found
    asked = next(filter(None, map(deploy_plane_variable_ask, stages)), None)
    return ("ask", asked) if asked else (None, None)


def decision(payload) -> tuple[str, str] | None:
    """The `(decision, reason)` pair `decide` returns for this payload, or None.

    The arm entry point `bash-pretool.py` calls. `main()` below is the same arm run as its
    own process, which is what the tests and a hand invocation use.
    """
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        return None
    verdict, reason = decide(command, cwd=payload.get("cwd"))
    return (verdict, reason) if verdict and reason else None


def main() -> int:
    """Read the hook payload from stdin and emit the decision `decision` returns.

    A deny names the flagged footgun and its fix; an ask names why the rules could not run.
    Otherwise emits nothing. Always returns 0 (a decision is expressed through emitted JSON,
    not the exit code).
    """
    payload = read_payload()
    if payload is None:
        return 0
    verdict = decision(payload)
    if verdict:
        emit_pretooluse_decision(*verdict)
    return 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(main())
