#!/usr/bin/env python3
"""Renovate manual-action notifier — runs once per daily systemd-timer tick.

Queries the GitHub REST API (unauthenticated by default, authenticated when GITHUB_TOKEN is
set) for open Renovate PRs, classifies each (notify_logic), and posts a Discord digest ONLY
when the actionable set changes. Writes a last_run timestamp for the monitor-bridge
"Renovate Notifier — Alive" monitor.

Config from /etc/renovate-notify/config.env (KEY=VALUE): REPO, DISCORD_WEBHOOK, STATE_DIR,
GITHUB_TOKEN (optional). Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notify_logic import (
    PR,
    actionable,
    ci_rollup,
    dashboard_stale,
    find_dashboard,
    find_dashboard_problems,
    fingerprint,
    parse_automerge,
    problems_fingerprint,
    render_digest,
    render_problems,
    should_notify,
    CLEARED_MSG,
)
from host_lib import atomic_write, discord_post, parse_env_file

CONFIG = "/etc/renovate-notify/config.env"
API = "https://api.github.com"
HEADERS = {"User-Agent": "renovate-notify", "Accept": "application/vnd.github+json"}
DASHBOARD_STALE_MSG = (
    "⚠️ Renovate looks DOWN — its Dependency Dashboard is stale or missing. The Renovate "
    "App or renovate.json may be broken, so dependency/security updates have silently "
    "stopped (the 'Renovate Notifier — Alive' monitor only watches this notifier, not "
    "Renovate itself). Check https://github.com/%s/issues"
)


def cfg() -> dict[str, str]:
    return parse_env_file(CONFIG)


def github_token(config: dict[str, str], run) -> str:
    """The token for the REST calls: `GITHUB_TOKEN` from config.env, else `gh auth token`.

    Same shape as deploy_logic.github_token in roles/setup/gitops_deploy: the gh CLI on this
    host is logged in as the repo owner and this unit runs as that user (ProtectHome=read-only
    still lets it read ~/.config/gh). Anonymous GitHub is 60 req/hr PER SOURCE IP, shared by
    every caller on the host — the deployer's CI gate exhausted it on 2026-09-01 — so even this
    once-a-day run authenticates when it can. Empty string means anonymous, exactly as before.
    """
    token = config.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        proc = run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def log(msg: str) -> None:
    print(msg, flush=True)


def get(url: str):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def is_renovate(pr: dict) -> bool:
    return (pr.get("user") or {}).get("login") == "renovate[bot]" or (
        pr.get("head") or {}
    ).get("ref", "").startswith("renovate/")


def build_pr(repo: str, pr: dict) -> PR:
    """Build a PR record for one open Renovate pull, fetching its detail, CI, and dead paths.

    Args:
        repo: "owner/repo".
        pr: one entry from the pulls-list payload.

    Returns:
        The PR dataclass, with `dead_paths` populated only when the PR is conflicting.
    """
    n = pr["number"]
    detail = get("%s/repos/%s/pulls/%d" % (API, repo, n))
    # mergeable_state "dirty" = conflicting; mergeable False likewise. null = unknown -> not conflicting.
    conflicting = (
        detail.get("mergeable_state") == "dirty" or detail.get("mergeable") is False
    )
    sha = pr["head"]["sha"]
    runs = get("%s/repos/%s/commits/%s/check-runs" % (API, repo, sha)).get(
        "check_runs", []
    )
    statuses = get("%s/repos/%s/commits/%s/status" % (API, repo, sha)).get(
        "statuses", []
    )
    return PR(
        number=n,
        title=pr.get("title", "").strip(),
        url=pr.get("html_url", ""),
        automerge=parse_automerge(pr.get("body") or ""),
        ci=ci_rollup(runs, statuses),
        conflicting=conflicting,
        created_at=pr.get("created_at", ""),
        dead_paths=dead_paths(repo, n, pr) if conflicting else None,
    )


def dead_paths(repo: str, n: int, pr: dict) -> tuple[str, ...]:
    """The PR's changed files that no longer exist on the base branch, if ALL of them are gone.

    Only called for conflicting PRs — it is one extra API call each, and the question is
    meaningless for a PR that merges cleanly. Returns () when any changed file still exists,
    because then a rebase can genuinely resolve the conflict and the ordinary note is right.

    Fails to (), never raises: a lookup error must degrade to the existing "conflicting" note
    rather than lose the PR from the digest entirely. An unreadable answer and "nothing is
    deleted" would otherwise be indistinguishable, which is the failure shape this whole check
    exists to fix.
    """
    base = ((pr.get("base") or {}).get("ref")) or "master"
    try:
        files = get("%s/repos/%s/pulls/%d/files?per_page=100" % (API, repo, n))
    except Exception as exc:
        log("dead_paths: could not list files for #%d: %s" % (n, exc))
        return ()
    if not files:
        return ()
    gone = []
    for f in files:
        path = f.get("filename", "")
        if not path:
            continue
        try:
            get("%s/repos/%s/contents/%s?ref=%s" % (API, repo, path, base))
        except Exception:
            gone.append(path)
            continue
        # The file still exists on base: an ordinary conflict, resolvable by rebase.
        return ()
    return tuple(gone)


def discord(webhook: str, content: str) -> bool:
    """Post the digest via the shared host_lib.discord_post.

    See there for the Cloudflare-1010 User-Agent + 2xx-only-success contract the dedupe
    fingerprint gates on. Failure returns False so the digest is retried on the next daily run.
    """
    return discord_post(webhook, content, "renovate-notify", log=log)


def read_state(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return ""


def write_state(path: str, fp: str) -> None:
    atomic_write(path, fp)  # torn-write-safe temp+rename, see host_lib


def main() -> int:
    """Fetch open Renovate PRs and the dashboard, and post a digest on a fingerprint change.

    With `--dry-run`, logs what it would post instead of calling Discord and does not persist
    the fingerprint or the liveness marker. A fetch failure raises rather than returning
    non-zero, so the OnFailure alert unit pages on it. Otherwise always returns 0.
    """
    dry = "--dry-run" in sys.argv
    c = cfg()
    repo = c["REPO"]
    # Authenticate the REST calls when a token is configured. Unauthenticated GitHub is 60 req/hr/IP,
    # and each open Renovate PR costs ~3 calls (detail + check-runs + status) on top of the two list
    # calls, so a large backlog (~19+ PRs) can exhaust the limit in one run -> the fetch 403s, main()
    # raises, the OnFailure alert unit fires a *false* page, and that day's digest is skipped. A
    # fine-grained read-only PAT lifts the ceiling to 5000/hr. Empty token = stay unauthenticated.
    token = github_token(c, subprocess.run)
    if token:
        HEADERS["Authorization"] = "Bearer " + token
    state_dir = c.get("STATE_DIR", "/var/lib/renovate-notify")
    state_file = os.path.join(state_dir, "last_notified")

    pulls = get("%s/repos/%s/pulls?state=open&per_page=100" % (API, repo))
    prs = [build_pr(repo, p) for p in pulls if is_renovate(p)]
    items = actionable(prs)

    # Fail-loud backstop: Renovate rewrites its Dependency Dashboard issue every run, so a
    # stale/missing dashboard means Renovate itself stopped (broken App or renovate.json) —
    # a state with NO PRs, which the digest alone would read as a healthy "backlog cleared".
    # Fold it into the fingerprint so it notifies on transition, not every daily tick.
    issues = get("%s/repos/%s/issues?state=open&per_page=100" % (API, repo))
    stale = dashboard_stale(find_dashboard(issues))
    # Repository Problems (per-package lookup failures, config warnings) get no PR and
    # don't touch dashboard staleness — a package can silently stop updating forever
    # otherwise (karakeep's gcr.io image, 2026-08). Problem strings go straight into the
    # fingerprint so a NEW problem re-pages even while an old one is still unresolved.
    problems = find_dashboard_problems(issues)
    cur_fp = (
        fingerprint(items)
        + ("|dashboard-stale" if stale else "")
        + ("|problems:" + problems_fingerprint(problems) if problems else "")
    )
    prev_fp = read_state(state_file)
    notify, kind = should_notify(prev_fp, cur_fp)
    log(
        "actionable=%d dashboard_stale=%s problems=%d fp=%r prev=%r -> %s"
        % (len(items), stale, len(problems), cur_fp, prev_fp, kind)
    )

    if notify:
        if stale or problems:
            parts = []
            if stale:
                parts.append(DASHBOARD_STALE_MSG % repo)
            if problems:
                parts.append(render_problems(problems))
            content = "\n\n".join(parts)
            if items:
                content += "\n\n" + render_digest(items)
        elif kind == "cleared":
            content = CLEARED_MSG
        else:
            content = render_digest(items)
        if dry:
            log("--- DRY RUN, would post ---\n%s" % content)
        else:
            # Persist the dedupe fingerprint only on confirmed delivery, else retry next run.
            if discord(c.get("DISCORD_WEBHOOK", ""), content):
                write_state(state_file, cur_fp)

    if not dry:
        # Liveness marker for monitor-bridge — only on a clean completion (a fetch
        # exception propagates and skips this, so a broken notifier goes stale). Atomic
        # (via write_state) so a torn read can't false-page Renovate Notifier — Alive.
        write_state(os.path.join(state_dir, "last_run"), str(time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
