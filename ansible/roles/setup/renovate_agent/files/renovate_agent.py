#!/usr/bin/env python3
"""Unattended Renovate agent — runs once per daily systemd-timer tick.

Spends one headless Claude Code session (`claude -p "/renovate-prs"`) on the repo's open
Renovate PRs, then posts a Discord digest keyed on the MEASURED change in the open-PR set.
Writes a last_run timestamp for the "Renovate Agent — Alive" Kuma monitor.

The tick is gated before it costs anything: no open Renovate PRs means no session, and a
GitOps hold means no session either. Both decisions live in agent_logic, which is pure.

The session runs in a dedicated worktree, never in the primary checkout. `run_worktree`
decides whether that tree can be recreated and recreates it; `agent_toolbox` holds the
process boundaries both halves inject.

Config from /etc/renovate-agent/config.env (KEY=VALUE) — see templates/config.env.j2.
Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_logic import (
    OpenPR,
    decide,
    delta,
    parse_run,
    render_digest,
    render_skip,
    run_record,
)
from agent_toolbox import TOOLS, AgentTools, log, read_file
from host_lib import atomic_write, parse_env_file
from run_worktree import prepare_worktree, worktree_is_reusable

# The env override exists so the I/O shell can be exercised end-to-end against a throwaway
# config before the timer is ever armed. Without it the only way to run this code is to deploy
# it, and the first armed tick would be its first execution.
CONFIG = os.environ.get("RENOVATE_AGENT_CONFIG", "/etc/renovate-agent/config.env")
USER_AGENT = "renovate-agent"
# Distinct from the 1 a failed session returns, so the OnFailure page reads which it was.
EXIT_WORKTREE_BLOCKED = 2
# The login gh reports for Renovate's PRs. The unit's LAND_REQUIRE_AUTHOR must match it, and
# test_renovate_agent_unit.py reads it from here.
RENOVATE_AUTHOR = "app/renovate"
# The census lists every open PR and filters locally, so the cap covers all authors.
OPEN_PR_LIMIT = 200
# One JSON line per tick under STATE_DIR, from agent_logic.run_record.
RUNS_FILE = "runs.jsonl"


def discord_spool(state_dir: str) -> str:
    """Where a post the host could not deliver waits for the next tick's post (#3905).

    Every post here is fire-and-forget, so without the spool a digest sent while the host
    could not reach Discord was lost: 2026-10-08 11:04 UTC, inside the outage of #3882.
    """
    return os.path.join(state_dir, "discord-spool")


def _open_pr_listing(repo: str, tools: AgentTools) -> tuple[int, str]:
    """Every open PR in `repo`, with its author and `updatedAt`.

    Never `gh pr list --author`: with that flag gh runs a GraphQL `search(` query, and the
    search index is eventually consistent. The after-census runs seconds after the session
    exits, so the index can still omit a PR the session just opened, or list one it just
    merged (#2772). Without `--author`, gh reads `repository.pullRequests`, which is current.
    """
    return tools.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(OPEN_PR_LIMIT),
            "--json",
            # `updatedAt` is the one field a comment-only triage moves (#3032).
            "number,title,url,updatedAt,author",
        ]
    )


def _authored_by(listing: str, login: str) -> list[OpenPR]:
    return [
        OpenPR(
            number=int(p["number"]),
            title=p.get("title", ""),
            url=p.get("url", ""),
            updated_at=p.get("updatedAt") or "",
        )
        for p in json.loads(listing)
        if (p.get("author") or {}).get("login") == login
    ]


def open_prs(repo: str, tools: AgentTools | None = None) -> list[OpenPR]:
    """The open Renovate PRs, newest first. An empty list on any gh failure is NOT a census.

    Raising rather than returning [] matters: a failed `gh` call that read as "no open PRs"
    would make the tick skip quietly, which is indistinguishable from the healthy steady state.
    """
    rc, out = _open_pr_listing(repo, tools or TOOLS)
    if rc != 0:
        raise RuntimeError(f"gh pr list failed (exit {rc}): {out.strip()[:300]}")
    return _authored_by(out, RENOVATE_AUTHOR)


def pr_states(
    repo: str, numbers: list[int], tools: AgentTools | None = None
) -> dict[int, str]:
    """GitHub's state (`MERGED`, `CLOSED`) for each PR that left the open set during the run.

    A failed lookup leaves its PR out rather than raising: the session has already run, and a
    crash here would lose the digest that reports it. `delta` reads a missing PR as `unread`,
    never as merged.
    """
    states: dict[int, str] = {}
    for n in numbers:
        rc, out = (tools or TOOLS).run(
            ["gh", "pr", "view", str(n), "--repo", repo, "--json", "state"]
        )
        if rc != 0:
            log(f"gh pr view #{n} failed (exit {rc}): {out.strip()[:200]}")
            continue
        try:
            states[n] = str(json.loads(out).get("state") or "")
        except ValueError, AttributeError:
            log(f"gh pr view #{n} returned unparseable output: {out.strip()[:200]}")
    return states


def run_session(cfg: dict[str, str], cwd: str, log_path: str) -> tuple[str, int, bool]:
    """Run the headless Claude session in `cwd`, teeing its stdout to `log_path`.

    stdin is /dev/null: without it Claude Code waits 3s for piped input on every start, and
    under systemd there is no stdin to wait for.
    """
    prompt = read_file(cfg["PROMPT_FILE"])
    if not prompt.strip():
        raise RuntimeError(f"prompt file {cfg['PROMPT_FILE']} is empty or missing")
    argv = [
        cfg.get("CLAUDE_BIN", "claude"),
        "-p",
        prompt,
        "--permission-mode",
        cfg.get("PERMISSION_MODE", "auto"),
        "--model",
        cfg.get("MODEL", "opus"),
        "--output-format",
        "json",
        "--max-budget-usd",
        cfg.get("BUDGET_USD", "25"),
    ]
    timeout = int(cfg.get("RUN_TIMEOUT_S", "5400"))
    log(f"starting session (timeout {timeout}s, budget ${cfg.get('BUDGET_USD', '25')})")
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        atomic_write(log_path, (e.stdout or "") if isinstance(e.stdout, str) else "")
        return "", 124, True
    atomic_write(log_path, p.stdout or "")
    return p.stdout or "", p.returncode, False


def record_run(state_dir: str, line: str) -> None:
    """Append one tick's record to `runs.jsonl`; a failed write must not cost the digest."""
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, RUNS_FILE), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        log(f"could not record the run: {exc}")


def main(tools: AgentTools = TOOLS, config_path: str = CONFIG) -> int:
    cfg = parse_env_file(config_path)
    missing = [k for k in ("REPO", "REPO_DIR", "PROMPT_FILE") if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"{config_path} is missing {', '.join(missing)}")
    host = os.uname().nodename
    webhook = cfg.get("DISCORD_WEBHOOK", "")
    state_dir = cfg.get("STATE_DIR", "/var/lib/renovate-agent")
    repo_dir = cfg["REPO_DIR"]
    branch = cfg.get("BRANCH", "worktree-renovate-auto")
    path = os.path.join(
        repo_dir, ".claude", "worktrees", cfg.get("WORKTREE", "renovate-auto")
    )
    log_path = os.path.join(state_dir, "last_session.json")
    spool = discord_spool(state_dir)
    # Every tick, before anything can fail or take the quiet skip: a crash report queued during
    # an outage would otherwise wait for a day that has PRs to digest (#3905).
    tools.flush_discord_spool(spool, webhook, USER_AGENT, log=log)

    before = open_prs(cfg["REPO"], tools)
    try:
        snap = tools.deployer_state()
    except (OSError, UnicodeDecodeError) as exc:
        log(f"cannot read the GitOps deployer's state: {exc}")
        gate = decide(before, None, "")
    else:
        gate = decide(before, snap.hold or "", "; ".join(snap.held_planes))
    if not gate.run:
        log(f"skipping: {gate.reason}")
        record_run(state_dir, run_record(int(time.time()), "skipped", gate.reason))
        if not gate.quiet:
            tools.discord_post(
                webhook, render_skip(gate, host), USER_AGENT, log=log, spool_dir=spool
            )
        return 0

    reusable, why = worktree_is_reusable(repo_dir, path, branch, tools, cfg["REPO"])
    if not reusable:
        # The previous tick left work in flight, so leave the tree alone — removing it is how
        # unlanded work is lost. But it IS a unit failure: the tree stays blocked until a person
        # clears it, and this skip repeated for five days behind a green Alive tile before
        # #2014, because exit 0 let ExecStartPost beat. A `down` pushed here would be laundered
        # by that same beat, so the exit code carries it: no beat, and OnFailure pages.
        msg = f"renovate-agent: skipped on {host} — {why}. Clear it, then the next tick runs."
        log(msg)
        tools.discord_post(webhook, msg, USER_AGENT, log=log, spool_dir=spool)
        record_run(state_dir, run_record(int(time.time()), "blocked", why))
        return EXIT_WORKTREE_BLOCKED

    log(f"{gate.reason}; preparing {path}")
    prepare_worktree(repo_dir, path, branch, tools)
    stdout, rc, timed_out = run_session(cfg, path, log_path)
    outcome = parse_run(stdout, rc, timed_out)

    after = open_prs(cfg["REPO"], tools)
    gone = sorted({p.number for p in before} - {p.number for p in after})
    moved = delta(before, after, pr_states(cfg["REPO"], gone, tools))
    log(
        f"resolved={moved.resolved} closed={moved.closed} unread={moved.unread} touched="
        f"{moved.touched} remaining={moved.remaining} "
        f"ok={outcome.ok}"
    )
    tools.discord_post(
        webhook,
        render_digest(outcome, moved, host, log_path),
        USER_AGENT,
        log=log,
        spool_dir=spool,
    )

    now = int(time.time())
    result = "ran" if outcome.ok else "failed"
    record_run(state_dir, run_record(now, result, outcome.error, moved, outcome))
    atomic_write(os.path.join(state_dir, "last_run"), str(now))
    # A session that failed is a unit failure, so OnFailure pages as well as the digest.
    return 0 if outcome.ok else 1


def report_crash(
    exc: BaseException, tools: AgentTools = TOOLS, config_path: str = CONFIG
) -> None:
    """Push a `down` carrying the exception text, and post the same to Discord.

    The alive beat is the unit's `ExecStartPost`, which systemd runs only when the process
    exited 0 — so a crash pushed NOTHING and the monitor went down 28 hours later by deadman
    expiry, carrying no reason. An operator then reads "no heartbeat", which is what a host that
    is simply off looks like: for two days from 2026-09-08 that hid a one-line
    `prepare_worktree` failure (#1477). The deadman stays as the backstop for a host that is
    genuinely off; it must not be the only signal for a run that started and threw.

    Best effort throughout, and it never re-raises: a failed report must not replace the
    traceback that says what actually broke. The push URL carries a rotation-tracked token, so
    it is read from config.env (0600) and never logged.
    """
    text = f"{type(exc).__name__}: {exc}"
    try:
        cfg = parse_env_file(config_path)
    except OSError:
        cfg = {}
    state_dir = cfg.get("STATE_DIR", "/var/lib/renovate-agent")
    record_run(state_dir, run_record(int(time.time()), "crashed", text[:200]))
    push_url = cfg.get("KUMA_PUSH_URL", "")
    if push_url:
        sep = "&" if "?" in push_url else "?"
        # The URL reaches curl as a config file on stdin (`-K -`), the form the unit's
        # ExecStartPost uses, because a curl argv is readable by any local user while it runs.
        url = f"{push_url}{sep}status=down".replace("\\", "\\\\").replace('"', '\\"')
        tools.run(
            [
                "curl",
                "-fsS",
                "-m",
                "15",
                "-o",
                "/dev/null",
                "-K",
                "-",
                "--get",
                "--data-urlencode",
                f"msg=crashed: {text[:200]}",
            ],
            timeout=30,
            stdin_text=f'url = "{url}"\n',
        )
    tools.discord_post(
        cfg.get("DISCORD_WEBHOOK", ""),
        f"🚨 renovate-agent: CRASHED on {os.uname().nodename} — {text[:400]}",
        USER_AGENT,
        log=log,
        spool_dir=discord_spool(state_dir),
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        report_crash(e)
        raise
