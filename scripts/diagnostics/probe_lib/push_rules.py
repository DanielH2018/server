"""`probe.py push-rules <branch>` — which GitHub ruleset a push of HEAD to a branch meets, and why.

GitHub words every ruleset refusal and every bypass notice the same way, and names neither the
ruleset nor the reason: `remote: - Cannot update this protected ref.` Sessions spent two or
three calls each time working out which of this repo's three rulesets had answered and whether
the push had landed at all (#6327). The causes seen were an unsigned commit (the CI ruleset's
`required_signatures`), the agent branch fence (`creation`/`update` outside the agent's own
prefix) and a `docs-refresh/*` branch the fence also covers.

This reads, in order:

- `GET repos/<slug>/rules/branches/<branch>`: every active rule whose conditions match the
  branch, each tagged with its `ruleset_id`. GitHub does the ref-pattern matching, so the probe
  never re-implements `include`/`exclude` globbing.
- `GET repos/<slug>/rulesets/<id>` once per distinct ruleset: its `name`, `enforcement` and
  `current_user_can_bypass`. That last field answers for the `gh` login, which is printed,
  because `git push` may authenticate as another account. An anonymous read omits it, and the
  probe then reports the bypass as unknown and counts the rule as refusing.
- Local git state for the push itself: whether the branch is on the remote (`creation` versus
  `update`), whether the remote head is an ancestor of the pushed revision (`non_fast_forward`),
  and which commits not on any remote-tracking ref carry no signature header
  (`required_signatures`). GitHub verifies a signature against the pusher's registered key; a
  commit with no `gpgsig` header fails that check whatever key it would have used, so the
  header's absence is the part a local read can decide.

Exit codes: 0 when no rule refuses the push (bypassed rules are listed, since GitHub still
prints them as `Bypassed rule violations` on a push that lands), 1 when at least one rule
refuses it, 75 when a read failed.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import json
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field

from lib.exit_codes import PROBE_FAILED, PROBE_OK, PROBE_UNREADABLE
from lib.gh import gh
from lib.git import git

# `git remote get-url` in both of GitHub's shapes: scp-style ssh and https.
_SLUG = re.compile(r"github\.com[:/](?P<slug>[^/\s]+/[^/\s]+?)(?:\.git)?/?$")

# Only `always` lets a direct push through. `pull_requests_only` bypasses the rule when a PR
# merges, never on a push; `never` and an absent field (an anonymous read) do not bypass.
BYPASS_ON_PUSH = "always"


class Unreadable(Exception):
    """A git or gh read failed, so the probe observed nothing and exits 75."""


@dataclass
class PushFacts:
    """What a push of `rev` to `branch` would do, read from the local checkout."""

    branch: str
    exists: bool
    fast_forward: bool
    unsigned: list[str] = field(default_factory=list)
    merges: list[str] = field(default_factory=list)


def _checked(argv, proc):
    """`proc`'s stdout; a non-zero exit raises `Unreadable` naming the command and its stderr."""
    if proc.returncode != 0:
        raise Unreadable(
            f"{' '.join(argv[:3])} exited {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout


def run_git(*args, input=None, ok_codes=(0,)):
    """Run one git read; an exit outside `ok_codes` raises `Unreadable` with its stderr."""
    try:
        proc = git(*args, check=False, timeout=30, input=input)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unreadable(f"git {' '.join(args[:2])}: {exc}") from exc
    if proc.returncode not in ok_codes:
        _checked(
            ["git", *args], proc
        )  # raises: a code outside ok_codes is never 0 here
    return proc


def run_gh(*args):
    """Run one `gh` read and return its stdout; a failure raises `Unreadable`."""
    try:
        proc = gh(*args, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unreadable(f"gh {' '.join(args[:2])}: {exc}") from exc
    return _checked(["gh", *args], proc)


def repo_slug(remote_url):
    """`owner/name` from a GitHub remote URL, or None for a remote that is not GitHub."""
    match = _SLUG.search(remote_url.strip())
    return match.group("slug") if match else None


def unsigned_commits(raw_batch):
    """The SHAs in `git cat-file --batch` output whose commit object carries no signature.

    An SSH or GPG signature is a `gpgsig` (or `gpgsig-sha256`) header line, which sits before
    the blank line that ends the header block.
    """
    unsigned = []
    pos = 0
    while pos < len(raw_batch):
        eol = raw_batch.index("\n", pos)
        sha, _kind, size = raw_batch[pos:eol].split(" ")
        body = raw_batch[eol + 1 : eol + 1 + int(size)]
        pos = eol + 1 + int(size) + 1
        headers = body.split("\n\n", 1)[0].splitlines()
        if not any(h.startswith(("gpgsig ", "gpgsig-sha256 ")) for h in headers):
            unsigned.append(sha)
    return unsigned


def read_push_facts(branch, rev, remote):
    """Read the push's shape from git: remote head, ancestry, and the commits it would add."""
    listing = run_git("ls-remote", remote, f"refs/heads/{branch}").stdout.split()
    remote_head = listing[0] if listing else None
    pushed = run_git("rev-parse", "--verify", f"{rev}^{{commit}}").stdout.strip()
    fast_forward = True
    if remote_head:
        # 1 is "not an ancestor"; anything else, an object this checkout never fetched
        # included, is a read that answered nothing.
        try:
            probe = run_git(
                "merge-base", "--is-ancestor", remote_head, pushed, ok_codes=(0, 1)
            )
        except Unreadable as exc:
            raise Unreadable(f"{exc} (fetch {remote} first)") from exc
        fast_forward = probe.returncode == 0
    new = run_git("rev-list", pushed, "--not", f"--remotes={remote}").stdout.split()
    unsigned = []
    if new:
        batch = run_git("cat-file", "--batch", input="\n".join(new) + "\n")
        unsigned = unsigned_commits(batch.stdout)
    merges = run_git(
        "rev-list", "--merges", pushed, "--not", f"--remotes={remote}"
    ).stdout.split()
    return PushFacts(branch, remote_head is not None, fast_forward, unsigned, merges)


def why_it_binds(rule_type, facts):
    """Why a rule of `rule_type` refuses this push, or None when this push does not trip it."""
    if rule_type == "creation":
        return None if facts.exists else "creating this branch is restricted"
    if rule_type == "update":
        return "updating this branch is restricted" if facts.exists else None
    if rule_type == "deletion":
        return None  # only a delete trips it, and the probe reads a push
    if rule_type == "non_fast_forward":
        if facts.exists and not facts.fast_forward:
            return "the push is not a fast-forward of the remote head (a force-push)"
        return None
    if rule_type == "required_signatures":
        if facts.unsigned:
            shas = ", ".join(s[:9] for s in facts.unsigned)
            return f"unsigned commits: {shas}"
        return None
    if rule_type == "required_linear_history":
        if facts.merges:
            return f"merge commits: {', '.join(s[:9] for s in facts.merges)}"
        return None
    if rule_type == "pull_request":
        return "changes must arrive through a pull request"
    if rule_type == "required_status_checks":
        return "required status checks must pass first, which a direct push cannot"
    return f"a `{rule_type}` rule, which this probe does not evaluate locally"


def evaluate(rules, rulesets, facts):
    """Split the rules that match the branch into refusing and bypassed rows.

    `rules` is the `rules/branches` listing; `rulesets` maps a ruleset id to its GET body. A
    ruleset whose enforcement is not `active` (an `evaluate` dry run) refuses nothing.

    Returns (refusing, bypassed): lists of (ruleset name, ruleset id, rule type, reason, bypass).
    """
    refusing, bypassed = [], []
    for rule in rules:
        rid = rule["ruleset_id"]
        body = rulesets[rid]
        if body.get("enforcement") != "active":
            continue
        reason = why_it_binds(rule["type"], facts)
        if reason is None:
            continue
        bypass = body.get("current_user_can_bypass")
        row = (body.get("name", "?"), rid, rule["type"], reason, bypass)
        (bypassed if bypass == BYPASS_ON_PUSH else refusing).append(row)
    return refusing, bypassed


def format_report(facts, refusing, bypassed, login, remote_url):
    """The text view: one line per binding rule, then who the bypass answers were for."""
    state = "on the remote" if facts.exists else "not on the remote (a push creates it)"
    lines = [f"branch {facts.branch}: {state}"]
    if not refusing and not bypassed:
        lines.append("no active ruleset rule binds this push")
    for name, rid, rtype, reason, bypass in refusing:
        why_not = (
            "bypass unknown (anonymous read)"
            if bypass is None
            else f"bypass for this login: {bypass}"
        )
        lines.append(
            f"  REFUSES   ruleset {name!r} ({rid}) rule {rtype}: {reason}; {why_not}"
        )
    for name, rid, rtype, reason, _bypass in bypassed:
        lines.append(f"  bypassed  ruleset {name!r} ({rid}) rule {rtype}: {reason}")
    if bypassed:
        lines.append(
            "GitHub still prints a bypassed rule as `remote: Bypassed rule violations for "
            f"refs/heads/{facts.branch}:` on a push that lands."
        )
    lines.append(
        f"Bypass answers are for gh login {login or '(anonymous)'}; git push authenticates "
        f"through {remote_url}, which may be another account."
    )
    return "\n".join(lines)


def run_push_rules(ns) -> int:
    """Read the rules for `ns.branch`, evaluate them against a push of `ns.rev`, print them."""
    try:
        remote_url = run_git("remote", "get-url", ns.remote).stdout.strip()
        slug = repo_slug(remote_url)
        if slug is None:
            raise Unreadable(
                f"remote {ns.remote} ({remote_url}) is not a GitHub remote"
            )
        branch_path = urllib.parse.quote(ns.branch, safe="")
        rules = json.loads(run_gh("api", f"repos/{slug}/rules/branches/{branch_path}"))
        rulesets = {
            rid: json.loads(run_gh("api", f"repos/{slug}/rulesets/{rid}"))
            for rid in dict.fromkeys(r["ruleset_id"] for r in rules)
        }
        facts = read_push_facts(ns.branch, ns.rev, ns.remote)
    except (Unreadable, json.JSONDecodeError, KeyError) as exc:
        print(f"probe push-rules: could not read: {exc}", file=sys.stderr)
        return PROBE_UNREADABLE
    try:
        login = run_gh("api", "user", "--jq", ".login").strip()
    except Unreadable:
        login = ""
    refusing, bypassed = evaluate(rules, rulesets, facts)
    print(format_report(facts, refusing, bypassed, login, remote_url))
    return PROBE_FAILED if refusing else PROBE_OK
