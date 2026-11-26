#!/usr/bin/env python3
"""Unattended Renovate agent — runs once per daily systemd-timer tick.

Spends one headless Claude Code session (`claude -p "/renovate-prs"`) on the repo's open
Renovate PRs, then posts a Discord digest keyed on the MEASURED change in the open-PR set.
The unit's ExecStartPost writes the textfile verdict that RenovateAgentNotReporting reads.

The tick is gated before it costs anything: no open Renovate PRs means no session. The decision
lives in agent_logic, which is pure.

The session runs in its own unit, as its own user, in the controller's clone, which it reads and
cannot write; this runner only starts it and reads what it left in the exchange directory. `agent_toolbox` holds the process boundaries both inject.

Config from /etc/renovate-agent/config.env (KEY=VALUE) — see templates/config.env.j2.
Stdlib only.

Usage: renovate_agent.py   (takes no arguments; -h/--help prints this text)

Run by renovate-agent.service from renovate-agent.timer (daily), installed as
/usr/lib/homelab/renovate-agent/renovate_agent.py. To start a tick by hand, run
`sudo systemctl start renovate-agent`. RENOVATE_AGENT_CONFIG overrides the config path.
REPO, REPO_DIR, EXCHANGE_DIR and SESSION_USER are required in the config file.
"""

import json
import os
import shutil
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
from agent_toolbox import TOOLS, AgentTools, log
from host_lib import (
    atomic_write,
    fresh_dir,
    install_excepthook,
    keep_transcript,
    parse_env_file,
    read_untrusted,
    start_unit,
)
from renovate_identity import RenovatePR
from renovate_session import OUT_DIR, OUTPUT_FILE
from run_requests import REQUESTS_DIR, Outcome, carry_out

# The env override exists so the I/O shell can be exercised end-to-end against a throwaway
# config before the timer is ever armed. Without it the only way to run this code is to deploy
# it, and the first armed tick would be its first execution.
CONFIG = os.environ.get("RENOVATE_AGENT_CONFIG", "/etc/renovate-agent/config.env")
USER_AGENT = "renovate-agent"
SESSION_OUTPUT_MAX = 1 << 20
# The census lists every open PR and filters locally, so the cap covers all authors.
OPEN_PR_LIMIT = 200
# One JSON line per tick under STATE_DIR, from agent_logic.run_record.
RUNS_FILE = "runs.jsonl"
TRANSCRIPTS_DIR = "transcripts"
TRANSCRIPTS_KEPT = 30
# The unit the session runs in; polkit lets this process's user start it.
SESSION_UNIT = "homelab-renovate-agent-session.service"


def discord_spool(state_dir: str) -> str:
    """Where a post the host could not deliver waits for the next tick's post.

    Every post here is fire-and-forget, so without the spool a digest sent while the host
    could not reach Discord would be lost.
    """
    return os.path.join(state_dir, "discord-spool")


def _open_pr_listing(repo: str, tools: AgentTools) -> tuple[int, str]:
    """Every open PR in `repo`, with its author and `updatedAt`.

    Never `gh pr list --author`: with that flag gh runs a GraphQL `search(` query, and the
    search index is eventually consistent. The after-census runs seconds after the session
    exits, so the index can still omit a PR the session just opened, or list one it just
    merged. Without `--author`, gh reads `repository.pullRequests`, which is current.
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
            # `updatedAt` is the one field a comment-only triage moves.
            "number,title,url,updatedAt,author,headRefName",
        ]
    )


def _renovate_prs(listing: str) -> list[OpenPR]:
    prs = []
    for p in json.loads(listing):
        renovate = RenovatePR.from_gh(p)
        if renovate.is_renovate:
            prs.append(
                OpenPR(
                    number=renovate.number,
                    title=p.get("title", ""),
                    url=p.get("url", ""),
                    updated_at=p.get("updatedAt") or "",
                )
            )
    return prs


def open_prs(repo: str, tools: AgentTools | None = None) -> list[OpenPR]:
    """The open Renovate PRs, newest first. An empty list on any gh failure is NOT a census.

    Raising rather than returning [] matters: a failed `gh` call that read as "no open PRs"
    would make the tick skip quietly, which is indistinguishable from the healthy steady state.
    """
    rc, out = _open_pr_listing(repo, tools or TOOLS)
    if rc != 0:
        raise RuntimeError(f"gh pr list failed (exit {rc}): {out.strip()[:300]}")
    return _renovate_prs(out)


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


def session_output(out: str) -> str:
    """What the session printed, from the exchange's `out/` directory, read as untrusted.

    The session wrote the file, so a symlink there (to this controller's token file, say) is
    refused rather than followed into the digest. A refused file reads as "".
    """
    try:
        return read_untrusted(out, OUTPUT_FILE, SESSION_OUTPUT_MAX)
    except (OSError, ValueError) as exc:
        log(f"refused the session's {OUTPUT_FILE}: {type(exc).__name__}")
        return ""


def request_line(request: Outcome) -> str:
    """The journal line for one request, in fixed words only.

    The file name and a refusal's detail are session text. They stay in requests.jsonl, a file,
    and never reach the journal, where loki-homelab's rules read them.
    """
    done = "done" if request.done else "not done"
    return f"request {request.action or 'refused'} #{request.pr} {done}"


def landings(requests: list[Outcome]) -> list[str]:
    """One digest line for each nomination the runner did not land, with why."""
    return [
        f"#{r.pr} {r.detail}" for r in requests if r.action == "land" and not r.done
    ]


def keep_run_transcript(out: str, state_dir: str, ts: int) -> tuple[str, int | None]:
    """Keep the session's transcript as `<STATE_DIR>/transcripts/<ts>.jsonl`; (path, size).

    The path is "" when the session left none. The oldest transcripts past TRANSCRIPTS_KEPT go.
    """
    directory = os.path.join(state_dir, TRANSCRIPTS_DIR)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{ts}.jsonl")
    size = keep_transcript(out, path, "renovate-agent")
    kept = sorted(
        (n for n in os.listdir(directory) if n.removesuffix(".jsonl").isdigit()),
        key=lambda n: int(n.removesuffix(".jsonl")),
    )
    for name in kept[:-TRANSCRIPTS_KEPT]:
        os.unlink(os.path.join(directory, name))
    return (path if size is not None else ""), size


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
    missing = [
        k
        for k in ("REPO", "REPO_DIR", "EXCHANGE_DIR", "SESSION_USER")
        if not cfg.get(k)
    ]
    if missing:
        raise RuntimeError(f"{config_path} is missing {', '.join(missing)}")
    host = os.uname().nodename
    webhook = cfg.get("DISCORD_WEBHOOK", "")
    state_dir = cfg.get("STATE_DIR", "/var/lib/renovate-agent-ctl")
    exchange = cfg["EXCHANGE_DIR"]
    log_path = os.path.join(state_dir, "last_session.json")
    spool = discord_spool(state_dir)
    # Every tick, before anything can fail or take the quiet skip, so a queued crash report is not
    # held back.
    tools.flush_discord_spool(spool, webhook, USER_AGENT, log=log)
    # Before the skip too: a killed run leaves session files that a no-PR tick would otherwise keep.
    requests_dir = fresh_dir(exchange, REQUESTS_DIR)
    out = fresh_dir(exchange, OUT_DIR)

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
    wait = min(
        int(cfg.get("SESSION_TIMEOUT_S", "2700")) + 300, int(deadline - time.time())
    )
    end = start_unit(SESSION_UNIT, tools.run, max(wait, 1))
    stdout = session_output(out)
    atomic_write(log_path, stdout)
    transcript, _ = keep_run_transcript(out, state_dir, int(time.time()))
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
    # Nothing the session wrote outlives the run.
    shutil.rmtree(out, ignore_errors=True)
    shutil.rmtree(requests_dir, ignore_errors=True)

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
        render_digest(outcome, moved, host, transcript or log_path, landings(requests)),
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
    like, and can hide a one-line `prepare_worktree` failure for days.
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
