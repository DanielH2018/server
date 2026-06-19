#!/usr/bin/env python3
"""Renovate manual-action notifier — runs once per daily systemd-timer tick.

Queries the public GitHub REST API (unauthenticated) for open Renovate PRs, classifies
each (notify_logic), and posts a Discord digest ONLY when the actionable set changes.
Writes a last_run timestamp for the monitor-bridge "Renovate Notifier — Alive" monitor.

Config from /etc/renovate-notify/config.env (KEY=VALUE): REPO, DISCORD_WEBHOOK, STATE_DIR.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notify_logic import (  # noqa: E402
    PR, actionable, ci_rollup, fingerprint, parse_automerge,
    render_digest, should_notify, CLEARED_MSG,
)

CONFIG = "/etc/renovate-notify/config.env"
API = "https://api.github.com"
HEADERS = {"User-Agent": "renovate-notify", "Accept": "application/vnd.github+json"}


def cfg() -> dict[str, str]:
    out: dict[str, str] = {}
    with open(CONFIG) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    return out


def log(msg: str) -> None:
    print(msg, flush=True)


def get(url: str):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def is_renovate(pr: dict) -> bool:
    return ((pr.get("user") or {}).get("login") == "renovate[bot]"
            or (pr.get("head") or {}).get("ref", "").startswith("renovate/"))


def build_pr(repo: str, pr: dict) -> PR:
    n = pr["number"]
    detail = get("%s/repos/%s/pulls/%d" % (API, repo, n))
    # mergeable_state "dirty" = conflicting; mergeable False likewise. null = unknown -> not conflicting.
    conflicting = detail.get("mergeable_state") == "dirty" or detail.get("mergeable") is False
    sha = pr["head"]["sha"]
    runs = get("%s/repos/%s/commits/%s/check-runs" % (API, repo, sha)).get("check_runs", [])
    statuses = get("%s/repos/%s/commits/%s/status" % (API, repo, sha)).get("statuses", [])
    return PR(
        number=n,
        title=pr.get("title", "").strip(),
        url=pr.get("html_url", ""),
        automerge=parse_automerge(pr.get("body") or ""),
        ci=ci_rollup(runs, statuses),
        conflicting=conflicting,
    )


def discord(webhook: str, content: str) -> None:
    if not webhook:
        log("no DISCORD_WEBHOOK set; skipping post")
        return
    data = json.dumps({"content": content[:1900]}).encode()
    # User-Agent is required: Discord is behind Cloudflare, which 403s the default
    # Python-urllib UA (error code 1010) — without this the post silently fails.
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "renovate-notify"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # alerting must never crash the notifier
        log("discord post failed: %s" % e)


def read_state(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return ""


def write_state(path: str, fp: str) -> None:
    with open(path, "w") as fh:
        fh.write(fp)


def main() -> int:
    dry = "--dry-run" in sys.argv
    c = cfg()
    repo = c["REPO"]
    state_dir = c.get("STATE_DIR", "/var/lib/renovate-notify")
    state_file = os.path.join(state_dir, "last_notified")

    pulls = get("%s/repos/%s/pulls?state=open&per_page=100" % (API, repo))
    prs = [build_pr(repo, p) for p in pulls if is_renovate(p)]
    items = actionable(prs)
    cur_fp = fingerprint(items)
    prev_fp = read_state(state_file)
    notify, kind = should_notify(prev_fp, cur_fp)
    log("actionable=%d fp=%r prev=%r -> %s" % (len(items), cur_fp, prev_fp, kind))

    if notify:
        content = CLEARED_MSG if kind == "cleared" else render_digest(items)
        if dry:
            log("--- DRY RUN, would post ---\n%s" % content)
        else:
            discord(c.get("DISCORD_WEBHOOK", ""), content)
            write_state(state_file, cur_fp)

    if not dry:
        # Liveness marker for monitor-bridge — only on a clean completion (a fetch
        # exception propagates and skips this, so a broken notifier goes stale).
        with open(os.path.join(state_dir, "last_run"), "w") as fh:
            fh.write(str(time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
