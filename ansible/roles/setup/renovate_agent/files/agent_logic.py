"""Pure decision logic for the unattended Renovate agent (no I/O — unit-tested).

Decides whether a tick should spend a Claude session at all, and turns the session's result
JSON plus the before/after PR census into the Discord digest. The I/O shell
(renovate_agent.py) only fetches, runs the session, persists, and posts.

The digest is keyed on the MEASURED delta between the open-PR sets, never on the session's
own summary. A session that ended cleanly having achieved nothing still reports
`is_error: false` and still writes a confident closing paragraph, so trusting either would
give this the shape of the docs-refresh deadman that stamped `generators: ok` through two
failures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# The session's result JSON marks its final object with this `type`. Anything printed before
# it on stdout is a warning or progress line, which is why the parse scans rather than loads.
_RESULT_TYPE = "result"

# How much of the session's own closing summary reaches Discord. host_lib.discord_post caps
# the whole message at 1900 characters; this leaves room for the delta lines above it.
_SUMMARY_CHARS = 900


@dataclass(frozen=True)
class OpenPR:
    number: int
    title: str
    url: str = ""


@dataclass(frozen=True)
class Gate:
    """Whether this tick runs, and what to say about it.

    `quiet` separates the two kinds of skip. An empty backlog is the steady state and must not
    post — a daily "nothing to do" trains the channel to be ignored. Every other skip names a
    condition an operator has to clear, so it posts.
    """

    run: bool
    reason: str
    quiet: bool = False


@dataclass(frozen=True)
class Outcome:
    """What the `claude -p` process produced. `ok` describes the PROCESS, not the work."""

    ok: bool
    summary: str
    cost_usd: float
    turns: int
    denials: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True)
class Delta:
    """The measured effect of the run: which Renovate PRs left the open set, and which stayed."""

    resolved: tuple[int, ...]
    remaining: tuple[int, ...]
    opened: tuple[int, ...]


def decide(open_prs: list[OpenPR], hold_sha: str, hold_plane: str) -> Gate:
    """Whether to spend a session this tick.

    Skips on a GitOps hold because a session could only reach `CLAUDE.md` → *When to wait* and
    stop: a held host means an earlier SHA failed its health gate, so landing anything on top
    of it is the state the hold exists to prevent.
    """
    if hold_sha.strip():
        held = hold_sha.strip()[:8]
        plane = f" (broad apply: {hold_plane.strip()})" if hold_plane.strip() else ""
        return Gate(
            run=False,
            reason=f"the GitOps deployer is holding at {held}{plane} — clear the hold first",
        )
    if not open_prs:
        return Gate(run=False, reason="no open Renovate PRs", quiet=True)
    return Gate(run=True, reason=f"{len(open_prs)} open Renovate PR(s)")


def parse_run(stdout: str, rc: int, timed_out: bool) -> Outcome:
    """Read the session's result object out of `stdout`, tolerating anything printed before it.

    A timeout, a non-zero exit, or unparseable output all produce `ok=False` with the reason in
    `error`, so the caller posts the failure rather than a silent nothing.
    """
    if timed_out:
        return Outcome(
            False, "", 0.0, 0, (), "the session hit its timeout and was killed"
        )
    obj = _last_result_object(stdout)
    if obj is None:
        tail = (stdout.strip().splitlines() or [""])[-1]
        return Outcome(
            False, "", 0.0, 0, (), f"no result JSON on stdout (exit {rc}): {tail[:200]}"
        )
    denials = tuple(
        str(d.get("tool_name") or d) for d in obj.get("permission_denials") or []
    )
    ok = rc == 0 and not obj.get("is_error")
    error = "" if ok else str(obj.get("terminal_reason") or f"exit {rc}")
    return Outcome(
        ok=ok,
        summary=str(obj.get("result") or ""),
        cost_usd=float(obj.get("total_cost_usd") or 0.0),
        turns=int(obj.get("num_turns") or 0),
        denials=denials,
        error=error,
    )


def _last_result_object(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == _RESULT_TYPE:
            return obj
    return None


def delta(before: list[OpenPR], after: list[OpenPR]) -> Delta:
    """What actually moved. This, not the session's summary, is what the digest reports."""
    b = {p.number for p in before}
    a = {p.number for p in after}
    return Delta(
        resolved=tuple(sorted(b - a)),
        remaining=tuple(sorted(b & a)),
        opened=tuple(sorted(a - b)),
    )


def render_skip(gate: Gate, host: str) -> str:
    return f"renovate-agent: skipped on {host} — {gate.reason}."


def render_digest(outcome: Outcome, moved: Delta, host: str, log_path: str) -> str:
    """The Discord digest, headlined by the measured delta.

    The first line answers "did anything move", because that is the question a reader of a
    daily automation post is actually asking.
    """
    if not outcome.ok:
        head = f"🚨 renovate-agent FAILED on {host} — {outcome.error}"
    elif moved.resolved:
        nums = ", ".join(f"#{n}" for n in moved.resolved)
        head = f"✅ renovate-agent resolved {nums} on {host}"
    else:
        head = f"⚠️ renovate-agent ran on {host} and no Renovate PR changed state"

    lines = [head]
    if moved.remaining:
        lines.append("still open: " + ", ".join(f"#{n}" for n in moved.remaining))
    if moved.opened:
        lines.append(
            "opened during the run: " + ", ".join(f"#{n}" for n in moved.opened)
        )
    if outcome.denials:
        # A denial is the failure this whole design rests on not happening: auto mode refusing
        # the session's writes leaves it reading green while doing nothing.
        lines.append("permission denials: " + ", ".join(sorted(set(outcome.denials))))
    if outcome.summary:
        lines.append(
            "> " + outcome.summary.strip().replace("\n", "\n> ")[:_SUMMARY_CHARS]
        )
    lines.append(
        f"{outcome.turns} turns, ${outcome.cost_usd:.2f} — transcript: {log_path}"
    )
    return "\n".join(lines)
