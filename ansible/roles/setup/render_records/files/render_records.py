#!/usr/bin/env python3
"""Render-record producer — managed by Ansible (render_records role); edits overwritten.

Writes `/var/lib/homelab/k8s-renders.d/<service>.json` for every renderable k8s service, once
an hour, so `probe.py releases --stale-only` can compare a fresh render's digest against the
release record's instead of guessing from the paths a merge touched (#2574, #2586, #2587).

ONE RUN:

  1. Pick the newest commit on `origin/master` whose CI is green. The ref is the primary
     checkout's `origin/master`, the one the GitOps deployer fetches every tick and the one the
     staleness reader resolves. This script never fetches: a second fetcher on the same refs
     would contend with the deployer's for the ref locks. The green rule is the deployer's own
     (`deploy_toolbox.fetch_ci_verdict`, `deploy_git.ci_walk_candidates`), imported from
     /opt/gitops-deploy rather than copied, so the two cannot disagree about "green".
  2. Move a dedicated detached worktree to that commit. The render reads that tree, never the
     primary checkout, so it takes no tree lock and cannot read a half fast-forwarded tree.
  3. Ask that tree which services to render (`scripts/deploy_tools/render_targets.py`), so the
     list belongs to the commit being rendered.
  4. Run `deploy.sh --dry-run` over them with `-e manifests_render_record=true`. A dry run
     applies nothing (`--dry-run=server`) and takes no deploy lock.
  5. Read every record back and push the verdict to Uptime Kuma.

THE VERDICT COMES FROM THE RECORDS, NOT FROM ANSIBLE'S EXIT CODE. `up` only when every target
has a record whose `commit` is the chosen SHA, whose tree was clean, whose `host` is this host,
and whose `rendered_at` falls inside this run. A playbook that exits 0 having rendered nothing
would otherwise read as a fresh fleet.

EXIT CODES are the kuma-check timer's contract (roles/setup/common/tasks/kuma_check_timer.yml):
0 after an `up` push, 1 after a `down` push. Two paths exit 1 WITHOUT a push, and the unit's
Restart=on-failure reruns them: the boot grace, and a walk that found no green commit. Neither
is a verdict, so neither may turn the tile green or red. A producer that keeps deferring goes
red anyway, because the monitor's interval expires with no push.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import syslog
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEPLOYER_DIR = os.environ.get("DEPLOYER_DIR", "/opt/gitops-deploy")
if DEPLOYER_DIR not in sys.path:
    sys.path.insert(0, DEPLOYER_DIR)

from deploy_config import load_config, read_config_file  # noqa: E402
from deploy_git import ci_walk_candidates  # noqa: E402
from deploy_toolbox import fetch_ci_verdict, github_authenticated  # noqa: E402

TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"


def choose_green(
    rev_list: list[str],
    verdict: Callable[[str], str],
    walk_max: int,
    may_walk: bool,
) -> str | None:
    """The newest commit in `rev_list` whose CI verdict is `pass`, or None.

    `rev_list` is `git rev-list --first-parent` from the tip, newest first, tip included. The
    tip is read first. Below it, `ci_walk_candidates` bounds the walk exactly as it bounds the
    deployer's, with no held SHA: a render is not a deploy, so a SHA the deployer holds is still
    a tree worth recording. `may_walk` is False on a host with no GitHub token, where the walk
    would spend the anonymous 60/hour budget every landing shares; the deployer makes the same
    refusal.
    """
    if not rev_list:
        return None
    if verdict(rev_list[0]) == "pass":
        return rev_list[0]
    if not may_walk:
        return None
    for _behind, sha in ci_walk_candidates(rev_list, None, walk_max):
        if verdict(sha) == "pass":
            return sha
    return None


def record_problems(
    targets: list[str],
    records: dict[str, dict | None],
    sha: str,
    host: str,
    started: str,
) -> dict[str, str]:
    """{service: what is wrong with its record} for every target this run did not refresh.

    `records` maps a service to its parsed record, or None when the file is missing or does
    not parse. `started` is this run's start in the record's own `rendered_at` format, which
    sorts as text.
    """
    problems: dict[str, str] = {}
    for svc in targets:
        rec = records.get(svc)
        if rec is None:
            problems[svc] = "no record"
        elif rec.get("commit") != sha:
            problems[svc] = f"commit {str(rec.get('commit'))[:8]}"
        elif rec.get("tree_dirty") is not False:
            problems[svc] = "dirty tree"
        elif rec.get("host") != host:
            problems[svc] = f"host {rec.get('host')}"
        elif str(rec.get("rendered_at", "")) < started:
            problems[svc] = "not refreshed by this run"
    return problems


def verdict_message(
    targets: list[str], problems: dict[str, str], sha: str, rc: int
) -> tuple[str, str]:
    """The Kuma (status, message) for one run."""
    if not problems:
        return "up", f"{len(targets)} render records at {sha[:8]}"
    named = ", ".join(f"{svc} ({why})" for svc, why in sorted(problems.items()))
    return (
        "down",
        f"{len(problems)} of {len(targets)} render records not refreshed at {sha[:8]} "
        f"(deploy.sh rc={rc}): {named}",
    )


@dataclass
class Settings:
    checkout: Path  # the producer's own detached worktree
    record_dir: Path
    host: str
    gitops_config: str
    boot_grace_s: int

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Settings:
        return cls(
            checkout=Path(env["CHECKOUT_DIR"]),
            record_dir=Path(env["RECORD_DIR"]),
            host=env["RENDER_HOST"],
            gitops_config=env.get("GITOPS_CONFIG", "/etc/gitops-deploy/config.env"),
            boot_grace_s=int(env.get("BOOT_GRACE_S", "0") or 0),
        )


def git(*args: str, cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def sync_checkout(repo: Path, checkout: Path, sha: str) -> None:
    """Point the producer's worktree at `sha`, creating it when it is missing.

    Locked, so neither `git worktree prune` nor scripts/dev/prune_worktrees.py takes it. A
    directory deleted by hand leaves a locked, missing registration behind, which `worktree
    add` refuses, so that one is unlocked and pruned first.
    """
    if not (checkout / ".git").exists():
        git("worktree", "unlock", str(checkout), cwd=repo, check=False)
        git("worktree", "prune", cwd=repo)
        git("worktree", "add", "--detach", str(checkout), sha, cwd=repo)
        git(
            "worktree",
            "lock",
            "--reason",
            "render_records producer",
            str(checkout),
            cwd=repo,
        )
        return
    # `--force` discards tracked edits, which is all release_digest.yml counts as dirty. No
    # `git clean`: untracked files cannot reach a render, and `clean -x` would delete the
    # worktree's .venv and cost a full uv sync every hour.
    git("checkout", "--detach", "--force", sha, cwd=checkout)


def read_records(record_dir: Path, targets: list[str]) -> dict[str, dict | None]:
    records: dict[str, dict | None] = {}
    for svc in targets:
        try:
            records[svc] = json.loads((record_dir / f"{svc}.json").read_text())
        except OSError, ValueError:
            records[svc] = None
    return records


def log(status: str, message: str) -> None:
    """Journal line at NOTICE: this host's journald drops INFO (MaxLevelStore=notice)."""
    print(f"status={status} {message}", file=sys.stderr)
    syslog.openlog(ident="render-records", facility=syslog.LOG_DAEMON)
    try:
        level = syslog.LOG_WARNING if status == "down" else syslog.LOG_NOTICE
        syslog.syslog(level, f"status={status} {message[:900]}")
    finally:
        syslog.closelog()


def push(status: str, message: str) -> None:
    """Push to Kuma. A dropped push is printed and ignored; the monitor's interval catches it."""
    token = os.environ.get("PUSH_TOKEN", "")
    kuma_host = os.environ.get("KUMA_HOST", "")
    if not token or not kuma_host:
        print("render-records: no PUSH_TOKEN/KUMA_HOST, not pushing", file=sys.stderr)
        return
    query = urllib.parse.urlencode({"status": status, "msg": message[:900], "ping": ""})
    try:
        urllib.request.urlopen(
            f"https://{kuma_host}/api/push/{token}?{query}", timeout=10
        ).read()
    except Exception as exc:
        print(f"kuma push failed: {exc}", file=sys.stderr)


def host_uptime_s() -> float | None:
    try:
        with open("/proc/uptime", encoding="ascii") as f:
            return float(f.read().split()[0])
    except OSError, ValueError, IndexError:
        return None


def main() -> int:
    settings = Settings.from_env(dict(os.environ))
    uptime = host_uptime_s()
    if (
        settings.boot_grace_s > 0
        and uptime is not None
        and uptime < settings.boot_grace_s
    ):
        print(
            f"boot grace: uptime under {settings.boot_grace_s}s, no verdict",
            file=sys.stderr,
        )
        return 1

    config = load_config(read_config_file(settings.gitops_config))
    # The deployer's own checkout: the one its tick fetches and the reader resolves
    # origin/master in. Read from its config rather than templated twice.
    if not config.repo:
        print(f"{settings.gitops_config} names no REPO_DIR", file=sys.stderr)
        return 1
    repo = Path(config.repo)
    tip = git("rev-parse", f"refs/remotes/origin/{config.branch}", cwd=repo)
    walk_max = max(1, config.ci_ancestor_walk_max)
    rev_list = git(
        "rev-list", "--first-parent", f"--max-count={walk_max}", tip, cwd=repo
    ).split()

    def verdict(sha: str) -> str:
        return fetch_ci_verdict(
            sha,
            require_ci=config.require_ci,
            repo=config.ci_repo,
            contexts=config.ci_contexts,
        )

    sha = choose_green(rev_list, verdict, walk_max, github_authenticated())
    if sha is None:
        print(
            f"no green commit among the {len(rev_list)} newest on origin/{config.branch} "
            f"(tip {tip[:8]}); deferring without a verdict",
            file=sys.stderr,
        )
        return 1

    sync_checkout(repo, settings.checkout, sha)
    listed = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/deploy_tools/render_targets.py",
            settings.host,
        ],
        cwd=settings.checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    targets = [t for t in listed.stdout.strip().split(",") if t]
    if listed.returncode != 0 or not targets:
        message = f"render_targets.py found nothing to render at {sha[:8]}: {listed.stderr.strip()}"
        log("down", message)
        push("down", message)
        return 1

    # Floored to the second, the resolution `rendered_at` is written at.
    started = datetime.now(UTC).strftime(TIMESTAMP)
    began = time.monotonic()
    # --skip-staleness-check: the chosen commit may sit below the tip, and the record names
    # its own commit, which is what the reader compares. The gate protects a live apply from
    # a stale tree; a dry run into a record keyed by commit has nothing for it to protect.
    rc = subprocess.run(
        [
            "./scripts/deploy.sh",
            "--dry-run",
            "--skip-staleness-check",
            "--tags",
            ",".join(targets),
            "-e",
            "manifests_render_record=true",
        ],
        cwd=settings.checkout,
        stdin=subprocess.DEVNULL,
        check=False,
    ).returncode

    problems = record_problems(
        targets, read_records(settings.record_dir, targets), sha, settings.host, started
    )
    status, message = verdict_message(targets, problems, sha, rc)
    message += f" in {int(time.monotonic() - began)}s"
    log(status, message)
    push(status, message)
    return 0 if status == "up" else 1


if __name__ == "__main__":
    sys.exit(main())
