#!/usr/bin/env python3
"""Renovate manual-action notifier — runs once per daily systemd-timer tick.

Queries the GitHub REST API (authenticated through host_lib.github_token when a token is
found, anonymous otherwise) for open Renovate PRs, classifies each (notify_logic), and posts a Discord digest ONLY
when the actionable set changes. Writes a last_run timestamp for the monitor-bridge
"Renovate Notifier — Alive" monitor.

Config from /etc/renovate-notify/config.env (KEY=VALUE): REPO, DISCORD_WEBHOOK, STATE_DIR,
GITHUB_TOKEN or GH_TOKEN (optional). Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notify_logic import (
    PR,
    actionable,
    ci_rollup,
    dashboard_body,
    dashboard_headers_unrecognized,
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
    DASHBOARD_UNPARSEABLE_MSG,
)
from pending_logic import (
    churn_fingerprint,
    churning_pending,
    parse_pending,
    pending_fingerprint,
    pending_reset_fingerprint,
    pending_section_unreadable,
    render_churning,
    render_pending,
    render_pending_reset,
    stale_pending,
    update_pending_seen,
)
from host_lib import (
    atomic_write,
    discord_post,
    github_get,
    github_token,
    parse_env_file,
)

CONFIG = "/etc/renovate-notify/config.env"
USER_AGENT = "renovate-notify"
# The token `main` looks up once per run; None reads anonymously. A module binding rather than
# an argument because every fetch below, build_pr and dead_paths included, goes through `get`.
_token: str | None = None
DASHBOARD_STALE_MSG = (
    "⚠️ Renovate looks DOWN — its Dependency Dashboard is stale or missing. The Renovate "
    "App or renovate.json may be broken, so dependency/security updates have silently "
    "stopped (the 'Renovate Notifier — Alive' monitor only watches this notifier, not "
    "Renovate itself). Check https://github.com/%s/issues"
)


def cfg() -> dict[str, str]:
    return parse_env_file(CONFIG)


def log(msg: str) -> None:
    print(msg, flush=True)


def get(path: str):
    """GET one REST path with the token `main` looked up once; see host_lib.github_get."""
    return github_get(path, _token, user_agent=USER_AGENT)


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
    detail = get("repos/%s/pulls/%d" % (repo, n))
    # mergeable_state "dirty" = conflicting; mergeable False likewise. null = unknown -> not conflicting.
    conflicting = (
        detail.get("mergeable_state") == "dirty" or detail.get("mergeable") is False
    )
    sha = pr["head"]["sha"]
    runs = get("repos/%s/commits/%s/check-runs" % (repo, sha)).get("check_runs", [])
    statuses = get("repos/%s/commits/%s/status" % (repo, sha)).get("statuses", [])
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
        files = get("repos/%s/pulls/%d/files?per_page=100" % (repo, n))
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
            get("repos/%s/contents/%s?ref=%s" % (repo, path, base))
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


def read_pending_seen(path: str) -> dict[str, float]:
    """The {branch: first-seen epoch} map for pending dashboard items.

    Any unreadable or malformed file degrades to an empty map, which restarts every clock at
    the current run rather than raising: a corrupt state file must delay this check, never take
    the daily digest down with it.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except OSError, ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}


def pending_state_lost(seen_path: str, last_run_path: str) -> bool:
    """True when the dwell state is unusable on a host that has already completed a run.

    `stale_pending` treats an item with no first-seen entry as first seen now — deliberately,
    so the first run after the feature ships does not page for the whole section at once. A
    LOST state file hands it the same input, so every clock silently restarts at zero and the
    check can find nothing for up to soak + grace. This is the one signal that tells the two
    cases apart, and it needs a witness that the notifier has run here before: `last_run` is
    written on every clean non-dry run, so its absence is a genuine bootstrap.

    Reads the file itself rather than calling `read_pending_seen`, which degrades a corrupt
    file to `{}` — the same value a legitimately empty section writes. `{}` with `last_run`
    present is a healthy quiet day, never a loss.
    """
    if not os.path.exists(last_run_path):
        return False
    try:
        with open(seen_path) as fh:
            data = json.load(fh)
    except OSError, ValueError:
        return True
    return not isinstance(data, dict)


def write_pending_seen(path: str, seen: dict[str, float]) -> None:
    """Persist the first-seen map, unconditionally — NOT behind Discord delivery.

    The `last_notified` fingerprint beside it is written only on a confirmed post, because a
    failed post must be retried. This map is the opposite: it is a clock, and most runs post
    nothing. Gating it on delivery would reset every item's dwell on each quiet day and the
    check could never reach its threshold.
    """
    atomic_write(path, json.dumps(seen, sort_keys=True))


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
    # fine-grained read-only PAT lifts the ceiling to 5000/hr. No token = stay unauthenticated.
    global _token
    _token = github_token(c, subprocess.run)
    state_dir = c.get("STATE_DIR", "/var/lib/renovate-notify")
    state_file = os.path.join(state_dir, "last_notified")

    pulls = get("repos/%s/pulls?state=open&per_page=100" % repo)
    prs = [build_pr(repo, p) for p in pulls if is_renovate(p)]
    items = actionable(prs)

    # Fail-loud backstop: Renovate rewrites its Dependency Dashboard issue every run, so a
    # stale/missing dashboard means Renovate itself stopped (broken App or renovate.json) —
    # a state with NO PRs, which the digest alone would read as a healthy "backlog cleared".
    # Fold it into the fingerprint so it notifies on transition, not every daily tick.
    issues = get("repos/%s/issues?state=open&per_page=100" % repo)
    stale = dashboard_stale(find_dashboard(issues))
    # Repository Problems (per-package lookup failures, config warnings) get no PR and
    # don't touch dashboard staleness — a package can silently stop updating forever
    # otherwise (karakeep's gcr.io image, 2026-08). Problem strings go straight into the
    # fingerprint so a NEW problem re-pages even while an old one is still unresolved.
    problems = find_dashboard_problems(issues)
    # Updates that soaked past their minimumReleaseAge and never got a PR (issue #886). Neither
    # of the two checks above can see this one: the dashboard keeps updating (not stale) and a
    # held update is not a lookup failure (no Repository Problem) — the item just sits in
    # "Pending Status Checks" indefinitely, which is how promtail ran 3.3.0 for 111 days.
    body = dashboard_body(issues)
    unparseable = body is not None and (
        dashboard_headers_unrecognized(body) or pending_section_unreadable(body)
    )
    pending = parse_pending(body or "")
    now = time.time()
    seen_file = os.path.join(state_dir, "pending_seen.json")
    run_file = os.path.join(state_dir, "last_run")
    # A lost dwell file restarts every clock at zero, which reads exactly like the intended
    # first-run bootstrap and leaves the arm above able to find nothing for up to 14 days
    # (issue #1526). Report the reset instead of completing healthy through that window.
    pending_reset = pending_state_lost(seen_file, run_file)
    seen = update_pending_seen(read_pending_seen(seen_file), pending, now)
    stuck_pending = stale_pending(seen, pending, now)
    # A tag re-pushed faster than its own soak + grace resets the dwell above forever, so the
    # branch clock is the floor under it (#3076).
    churning = churning_pending(seen, pending, now)
    cur_fp = (
        fingerprint(items)
        + ("|dashboard-stale" if stale else "")
        + ("|dashboard-unparseable" if unparseable else "")
        + ("|problems:" + problems_fingerprint(problems) if problems else "")
        + ("|pending:" + pending_fingerprint(stuck_pending) if stuck_pending else "")
        + ("|pending-churn:" + churn_fingerprint(churning) if churning else "")
        # Keyed on the date the clocks become usable, so the same loss pages once. The run
        # after this one rewrites the file, the component drops, and the fingerprint moves
        # again — so expect one follow-up digest (or CLEARED_MSG) the next day. That is the
        # cost of folding this arm into the same dedupe as the other three, not a bug.
        + ("|pending-reset:" + pending_reset_fingerprint(now) if pending_reset else "")
    )
    prev_fp = read_state(state_file)
    notify, kind = should_notify(prev_fp, cur_fp)
    log(
        "actionable=%d dashboard_stale=%s problems=%d pending=%d stuck_pending=%d "
        "churning=%d unparseable=%s pending_reset=%s fp=%r prev=%r -> %s"
        % (
            len(items),
            stale,
            len(problems),
            len(pending),
            len(stuck_pending),
            len(churning),
            unparseable,
            pending_reset,
            cur_fp,
            prev_fp,
            kind,
        )
    )

    if notify:
        if (
            stale
            or problems
            or stuck_pending
            or churning
            or unparseable
            or pending_reset
        ):
            parts = []
            if stale:
                parts.append(DASHBOARD_STALE_MSG % repo)
            if unparseable:
                parts.append(DASHBOARD_UNPARSEABLE_MSG % repo)
            if pending_reset:
                parts.append(render_pending_reset(now, repo))
            if problems:
                parts.append(render_problems(problems))
            if stuck_pending:
                parts.append(render_pending(stuck_pending))
            if churning:
                parts.append(render_churning(churning))
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
        # The dwell clock, written every run regardless of what was posted — see
        # write_pending_seen for why it must not ride the delivery gate.
        write_pending_seen(seen_file, seen)
        # Liveness marker for monitor-bridge — only on a clean completion (a fetch
        # exception propagates and skips this, so a broken notifier goes stale). Atomic
        # (via write_state) so a torn read can't false-page Renovate Notifier — Alive.
        write_state(os.path.join(state_dir, "last_run"), str(time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
