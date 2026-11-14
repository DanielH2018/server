#!/usr/bin/env python3
"""Unattended Renovate agent — runs once per daily systemd-timer tick.

Spends one headless Claude Code session (`claude -p "/renovate-prs"`) on the repo's open
Renovate PRs, then posts a Discord digest keyed on the MEASURED change in the open-PR set.
The unit's ExecStartPost writes the textfile verdict that RenovateAgentNotReporting reads.

The tick is gated before it costs anything: no open Renovate PRs means no session. The decision
lives in agent_logic, which is pure.

The session runs in a dedicated worktree, never in the primary checkout. `run_worktree`
decides whether that tree can be recreated and recreates it; `agent_toolbox` holds the
process boundaries both halves inject.

Config from /etc/renovate-agent/config.env (KEY=VALUE) — see templates/config.env.j2.
Stdlib only.

Usage: renovate_agent.py   (takes no arguments; -h/--help prints this text)

Run by renovate-agent.service from renovate-agent.timer (daily), installed as
/usr/lib/homelab/renovate-agent/renovate_agent.py. To start a tick by hand, run
`sudo systemctl start renovate-agent`. RENOVATE_AGENT_CONFIG overrides the config path.
REPO, REPO_DIR and PROMPT_FILE are required in the config file.
"""

from __future__ import annotations

import json
import os
import pwd
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
    resume_command,
    run_record,
)
from agent_toolbox import TOOLS, AgentTools, log, start_unit
from host_lib import atomic_write, install_excepthook, parse_env_file
from renovate_session import BLOCKED_FILE, OUTPUT_FILE, SESSION_ENV_FILE
from run_requests import Outcome, carry_out, fresh_dir

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
# The unit the session runs in; polkit lets this process's user start it.
SESSION_UNIT = "homelab-renovate-agent-session.service"
# Every other variable the session unit sets. A resume has to set each again, because sudo
# resets the environment (#4603). test_session_resume.py holds this to both unit templates.
SESSION_ENV_KEYS = (
    "PATH",
    "HOME",
    "PYTHONDONTWRITEBYTECODE",
    "RUN_HOOK_PROJECT_DIR",
    "RUN_HOOK_UV",
)


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


def request_line(request: Outcome) -> str:
    """The journal line for one request, in fixed words only.

    The file name and a refusal's detail are session text. They stay in requests.jsonl, a file,
    and never reach the journal, where loki-homelab's rules read them (#967's I3 and I6, #8676).
    """
    done = "done" if request.done else "not done"
    return f"request {request.action or 'refused'} #{request.pr} {done}"


def landings(requests: list[Outcome]) -> list[str]:
    """One digest line for each nomination the runner did not land, with why."""
    return [
        f"#{r.pr} {r.detail}" for r in requests if r.action == "land" and not r.done
    ]


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
    # One deadline for the whole run: the session, then the landings it nominated.
    deadline = time.time() + int(cfg.get("RUN_TIMEOUT_S", "14400"))
    missing = [k for k in ("REPO", "REPO_DIR", "PROMPT_FILE") if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"{config_path} is missing {', '.join(missing)}")
    host = os.uname().nodename
    webhook = cfg.get("DISCORD_WEBHOOK", "")
    state_dir = cfg.get("STATE_DIR", "/var/lib/renovate-agent")
    path = os.path.join(
        cfg["REPO_DIR"], ".claude", "worktrees", cfg.get("WORKTREE", "renovate-auto")
    )
    log_path = os.path.join(state_dir, "last_session.json")
    spool = discord_spool(state_dir)
    # Every tick, before anything can fail or take the quiet skip: a crash report queued during
    # an outage would otherwise wait for a day that has PRs to digest (#3905).
    tools.flush_discord_spool(spool, webhook, USER_AGENT, log=log)

    before = open_prs(cfg["REPO"], tools)
    gate = decide(before)
    if not gate.run:
        log(f"skipping: {gate.reason}")
        record_run(state_dir, run_record(int(time.time()), "skipped", gate.reason))
        if not gate.quiet:
            tools.discord_post(
                webhook, render_skip(gate, host), USER_AGENT, log=log, spool_dir=spool
            )
        return 0

    log(f"{gate.reason}; starting {SESSION_UNIT}")
    requests_dir = fresh_dir(state_dir)
    for name in (OUTPUT_FILE, BLOCKED_FILE):
        if os.path.lexists(os.path.join(state_dir, name)):
            os.unlink(os.path.join(state_dir, name))
    wait = min(
        int(cfg.get("SESSION_TIMEOUT_S", "2700")) + 300, int(deadline - time.time())
    )
    end = start_unit(SESSION_UNIT, tools, max(wait, 1))
    why = tools.read_file(os.path.join(state_dir, BLOCKED_FILE)).strip()
    if why:
        # The previous tick left work in flight, so the session left the tree alone — removing
        # it is how unlanded work is lost. But it IS a unit failure: the tree stays blocked until
        # a person clears it, and this skip repeated for five days behind a green Alive tile
        # before #2014, because exit 0 let ExecStartPost beat. A `down` pushed here would be
        # laundered by that same beat, so the exit code carries it: no beat, and OnFailure pages.
        msg = f"renovate-agent: skipped on {host} — {why}. Clear it, then the next tick runs."
        log(msg)
        tools.discord_post(webhook, msg, USER_AGENT, log=log, spool_dir=spool)
        record_run(state_dir, run_record(int(time.time()), "blocked", why))
        return EXIT_WORKTREE_BLOCKED
    stdout = tools.read_file(os.path.join(state_dir, OUTPUT_FILE))
    atomic_write(log_path, stdout)
    timed_out = end.result in ("timeout", "deadline")
    rc = 0 if end.result == "success" else max(end.status, 1)
    outcome = parse_run(stdout, rc, timed_out)
    census = {p.number for p in before}
    requests = carry_out(
        requests_dir,
        census,
        cfg["REPO"],
        tools,
        state_dir,
        max_lands=int(cfg.get("MAX_PRS", "3")),
        deadline=deadline,
    )
    for request in requests:
        log(request_line(request))

    after = open_prs(cfg["REPO"], tools)
    gone = sorted({p.number for p in before} - {p.number for p in after})
    moved = delta(before, after, pr_states(cfg["REPO"], gone, tools))
    log(
        f"resolved={moved.resolved} closed={moved.closed} unread={moved.unread} touched="
        f"{moved.touched} remaining={moved.remaining} "
        f"ok={outcome.ok}"
    )
    resume = ""
    if outcome.session_id:
        # The unit runs this process as renovate_agent_user, so the effective user is the
        # session's owner.
        owner = pwd.getpwuid(os.geteuid()).pw_name
        resume = resume_command(
            outcome.session_id,
            path,
            owner,
            cfg.get("CLAUDE_BIN", "claude"),
            SESSION_ENV_FILE,
            {k: os.environ[k] for k in SESSION_ENV_KEYS if k in os.environ},
        )
    tools.discord_post(
        webhook,
        render_digest(outcome, moved, host, log_path, resume, landings(requests)),
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
    """Record the crash in the runs file and post the exception text to Discord.

    The unit's textfile write is its `ExecStartPost`, which systemd runs only when the process
    exited 0, so a crash writes no verdict and `RenovateAgentNotReporting` fires 28 hours later
    with no reason attached. An operator reading that sees what a host that is simply off looks
    like: for two days from 2026-09-08 that hid a one-line `prepare_worktree` failure (#1477).
    The runs file and the Discord post carry the reason.

    Best effort throughout, and it never re-raises: a failed report must not replace the
    traceback that says what actually broke.
    """
    text = f"{type(exc).__name__}: {exc}"
    try:
        cfg = parse_env_file(config_path)
    except OSError:
        cfg = {}
    state_dir = cfg.get("STATE_DIR", "/var/lib/renovate-agent")
    record_run(state_dir, run_record(int(time.time()), "crashed", text[:200]))
    tools.discord_post(
        cfg.get("DISCORD_WEBHOOK", ""),
        f"🚨 renovate-agent: CRASHED on {os.uname().nodename} — {text[:400]}",
        USER_AGENT,
        log=log,
        spool_dir=discord_spool(state_dir),
    )


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    # `report_crash` re-raises; the hook files that traceback at err, not the unit's notice.
    install_excepthook()
    try:
        sys.exit(main())
    except Exception as e:
        report_crash(e)
        raise
