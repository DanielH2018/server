#!/usr/bin/env python3
"""Fast-forward daniel-box's primary checkout to the newest green first-parent commit of master.

`homelab-checkout-sync.service` runs this as `sys_user` every 10 minutes, inside `flock` on the
git-tree lock, so it never runs beside the GitOps deployer's tick or another tree job. The design
is docs/target-architecture/host/h7-delete-ansible.md, *The deployer retires last, and
checkout-sync replaces its fast-forward* (H7.51). The run:

  1. refuses an empty `CI_CONTEXTS`, or a walk bound that is not a positive integer
     (reason `ci`), and a missing `github_token` credential (reason `token`), before any request;
  2. refuses a branch other than `master` (reason `branch`) and a dirty tree, untracked files
     included (reason `dirty`);
  3. fetches `origin master`, and refuses a checkout whose HEAD is not an ancestor of the tip
     (reason `ancestry`), because only a fast-forward is allowed;
  4. grades the tip with `ci_verdict`. When it is not green it grades the first-parent commits
     below it, newest first, up to the bound, and stops at the first green one. A walk past
     `WALK_BUDGET_S` stops grading, so a slow GitHub cannot run into the unit's timeout;
  5. merges the chosen commit with `git merge --ff-only`. With no green commit it merges nothing,
     and the next run tries again.

It writes `checkout_sync.prom` in the node-exporter textfile directory: the plan's
`homelab_checkout_*` series and M3's four `homelab_check_*` series for check `checkout-sync`.
A refusal exits 0 with `homelab_check_success` 0, which CheckoutSyncFailed pages on for as long
as it lasts. A run that crashes exits 1, and the unit's `OnFailure=homelab-alert@%n.service`
pages that.

Run: uv run --no-project --python <host_python_version> checkout_sync.py
`--help` prints this text and exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from builder_ci_grade import read_check_pages, token_refused
from ci_verdict import ci_verdict, ci_walk_candidates
from host_lib import ERR, github_get, github_token, write_textfile
from host_lib import log as host_log

TAG = "homelab-checkout-sync"
CHECK = "checkout-sync"
STATE_DIR = "/var/lib/homelab-checkout-sync"
TEXTFILE_DIR = "/var/lib/node-exporter-textfile"
PROM = "checkout_sync.prom"
# Two hours: twelve 10-minute runs, so a docs-refresh or eval sweep holding the tree lock for
# its whole run does not read as a stopped sync.
DEADLINE_S = 7200
REASONS = ("dirty", "branch", "ci", "token", "ancestry")
# Seconds of grading before the walk stops, under the unit's TimeoutStartSec=150.
WALK_BUDGET_S = 100


class Refused(Exception):
    """A run that changes nothing for `reason`, one of `REASONS`."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def log(message: str, err: bool = False) -> None:
    if err:
        host_log(message, ERR, tag=TAG, stream=sys.stderr)
    else:
        host_log(message, tag=TAG)


class _NoGh:
    returncode = 1
    stdout = ""


def _no_gh(*_args, **_kwargs) -> _NoGh:
    """A `run` for `github_token` that never asks `gh`, whose login is the operator's own."""
    return _NoGh()


@dataclass
class CheckoutSync:
    """One run over `checkout`. Every path and transport is a field, so a test can point it elsewhere."""

    checkout: Path
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    state_dir: Path = Path(STATE_DIR)
    textfile_dir: Path = Path(TEXTFILE_DIR)
    run: Callable[..., Any] = subprocess.run
    opener: Callable[..., Any] | None = None
    now: Callable[[], float] = time.time
    requests: int = 0
    started: float = 0.0

    def git(self, *args: str) -> str:
        proc = self.run(
            ["git", *args],
            cwd=self.checkout,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def main(self) -> int:
        self.started = self.now()
        dirty = None
        try:
            contexts, bound, token = self.settings()
            branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
            status = self.git("status", "--porcelain")
            dirty = bool(status)
            if branch != "master":
                raise Refused("branch", f"the checkout is on {branch!r}, not master")
            if dirty:
                paths = ", ".join(line[3:] for line in status.splitlines()[:12])
                raise Refused(
                    "dirty", f"working tree dirty (untracked files count): {paths}"
                )
            self.git("fetch", "--quiet", "origin", "master")
            self.sync(contexts, bound, token)
        except Refused as exc:
            log(f"refused ({exc.reason}): {exc}")
            self.write_prom(exc.reason, dirty)
            return 0
        except RuntimeError as exc:
            # A git error, most often a fetch GitHub did not answer. No verdict is written, so
            # a failure that persists ages out the check's deadline instead of paging every run.
            log(f"no verdict: {exc}", err=True)
            return 0
        self.write_prom(None, dirty)
        return 0

    def settings(self) -> tuple[frozenset[str], int, str]:
        contexts = frozenset(
            c.strip() for c in self.env.get("CI_CONTEXTS", "").split(",") if c.strip()
        )
        bound = self.env.get("CI_ANCESTOR_WALK_MAX", "").strip()
        # ci_verdict reads an empty set as a disarmed gate and returns `pass`, so refuse it here.
        if not contexts:
            raise Refused("ci", "CI_CONTEXTS is empty")
        if not bound.isdigit() or int(bound) <= 0:
            raise Refused(
                "ci", f"CI_ANCESTOR_WALK_MAX={bound!r} is not a positive integer"
            )
        source = {}
        cred = Path(self.env.get("CREDENTIALS_DIRECTORY", "/-"), "github_token")
        if cred.is_file():
            source["GH_TOKEN"] = cred.read_text()
        token = github_token(source, _no_gh)
        if token is None:
            raise Refused("token", "no github_token credential")
        return contexts, int(bound), token

    def sync(self, contexts: frozenset[str], bound: int, token: str) -> None:
        head = self.git("rev-parse", "HEAD")
        tip = self.git("rev-parse", "refs/remotes/origin/master")
        if head == tip:
            log(f"up to date at {tip[:12]}")
            return
        if self.run(
            ["git", "merge-base", "--is-ancestor", head, tip],
            cwd=self.checkout,
            capture_output=True,
            check=False,
        ).returncode:
            raise Refused(
                "ancestry",
                f"HEAD {head[:12]} is not an ancestor of origin/master {tip[:12]}; "
                "only a fast-forward is allowed",
            )
        revs = self.git("rev-list", "--first-parent", f"{head}..{tip}").split()
        candidates = [(0, tip), *ci_walk_candidates(revs, None, bound)]
        for distance, sha in candidates:
            if self.now() - self.started > WALK_BUDGET_S:
                log(f"ci: walk stopped after {WALK_BUDGET_S}s, before {sha[:12]}")
                break
            verdict = self.verdict(sha, contexts, token)
            if verdict == "pass":
                self.git("merge", "--ff-only", "--quiet", sha)
                log(
                    f"fast-forwarded {head[:12]} to {sha[:12]}, {distance} behind the tip {tip[:12]}"
                )
                return
            log(f"ci: {sha[:12]} is {verdict}, {distance} behind the tip")
        log(
            f"no green commit in the {len(candidates)} graded below {tip[:12]}; staying at {head[:12]}"
        )

    def verdict(self, sha: str, contexts: frozenset[str], token: str) -> str:
        repo = self.env.get("GITHUB_REPO", "")

        def read(listing: str, key: str) -> list[dict]:
            def page(query: str) -> object:
                self.requests += 1
                return github_get(
                    f"repos/{repo}/commits/{sha}/{listing}{query}",
                    token,
                    user_agent=TAG,
                    opener=self.opener,
                )

            return read_check_pages(page, key)

        try:
            runs = read("check-runs", "check_runs")
        except urllib.error.HTTPError as exc:
            if token_refused(exc):
                raise Refused(
                    "token", f"GitHub refused the token reading {sha[:12]}: {exc}"
                ) from exc
            log(f"ci: {sha[:12]} unreadable, read as pending: {exc}")
            return "pending"
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            log(f"ci: {sha[:12]} unreadable, read as pending: {exc}")
            return "pending"
        return ci_verdict(runs, contexts, lambda: read("check-suites", "check_suites"))

    def write_prom(self, reason: str | None, dirty: bool | None) -> None:
        now = self.now()
        try:
            last_ok = float(
                json.loads((self.state_dir / "success.json").read_text())["ts"]
            )
        except OSError, ValueError, KeyError, TypeError:
            last_ok = 0.0
        if reason is None:
            last_ok = now
            try:
                write_textfile(
                    self.state_dir / "success.json", json.dumps({"ts": now}), 0o600
                )
            except OSError as exc:
                log(f"could not write success.json: {exc}", err=True)
        lines = [
            "# HELP homelab_checkout_sync_success_timestamp_seconds End of the last run that refused nothing.",
            "# TYPE homelab_checkout_sync_success_timestamp_seconds gauge",
            f"homelab_checkout_sync_success_timestamp_seconds {last_ok}",
            "# HELP homelab_checkout_sync_refused 1 for the reason the last run refused on.",
            "# TYPE homelab_checkout_sync_refused gauge",
            *(
                f'homelab_checkout_sync_refused{{reason="{r}"}} {int(r == reason)}'
                for r in REASONS
            ),
        ]
        if dirty is not None:
            lines += [
                "# HELP homelab_checkout_dirty 1 when git status --porcelain listed anything.",
                "# TYPE homelab_checkout_dirty gauge",
                f"homelab_checkout_dirty {int(dirty)}",
            ]
        lines += [
            "# HELP homelab_check_success 1 when the check's last verdict was up, 0 when down.",
            "# TYPE homelab_check_success gauge",
            f'homelab_check_success{{check="{CHECK}"}} {int(reason is None)}',
            "# HELP homelab_check_last_run_timestamp_seconds Unix time the check's last verdict was taken.",
            "# TYPE homelab_check_last_run_timestamp_seconds gauge",
            f'homelab_check_last_run_timestamp_seconds{{check="{CHECK}"}} {now}',
            "# HELP homelab_check_deadline_seconds Longest legitimate gap between two of the check's writes.",
            "# TYPE homelab_check_deadline_seconds gauge",
            f'homelab_check_deadline_seconds{{check="{CHECK}"}} {DEADLINE_S}',
            "# HELP homelab_check_duration_seconds Seconds the check's run took to reach its verdict.",
            "# TYPE homelab_check_duration_seconds gauge",
            f'homelab_check_duration_seconds{{check="{CHECK}"}} {now - self.started}',
        ]
        try:
            write_textfile(self.textfile_dir / PROM, "\n".join(lines) + "\n")
        except OSError as exc:
            log(f"could not write {PROM}: {exc}", err=True)


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(CheckoutSync(Path.cwd()).main())
