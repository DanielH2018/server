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
from collections.abc import Mapping
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
    branch: str = ""


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
    """The measured effect of the run: which Renovate PRs left the open set, and which stayed.

    Leaving the open set is not landing. The renovate-prs skill finishes a `manual —` bump by
    closing the Renovate PR in favour of a superseding PR that stays open for a person (#2746),
    so a departed PR is split by the state GitHub reports for it: `resolved` merged, `closed`
    was closed without merging, and `unread` is one whose state lookup failed (#2755).
    `handed_off` is the superseding PRs the run opened, from `handed_off()`; None means that
    census failed, which must not read as "none opened" (#2769).
    """

    resolved: tuple[int, ...]
    remaining: tuple[int, ...]
    opened: tuple[int, ...]
    closed: tuple[int, ...] = ()
    unread: tuple[int, ...] = ()
    handed_off: tuple[int, ...] | None = ()


def decide(open_prs: list[OpenPR], hold_sha: str, hold_plane: str) -> Gate:
    """Whether to spend a session this tick.

    Skips on a GitOps hold because a session could only reach `CLAUDE.md` → *When to wait* and
    stop: a held host means an earlier SHA failed its health gate, so landing anything on top
    of it is the state the hold exists to prevent.
    """
    # DECIDED: `k8s_deferred` is NOT a second gate here (#2522). The marker names ONE service
    # whose merged image bump is still unapplied, and this function returns one run/skip for
    # the whole session — there is no per-PR verdict for it to reach. A bump deferred on
    # sonarr cannot withhold a session that is about to review radarr and traefik, and
    # withholding the whole session on it would park the backlog on an unrelated service.
    # Nothing downstream of `decide` is per-service either: `render_digest` reports the
    # measured PR delta. So the signal has no decision surface, and the surfaces that do read
    # it — the SessionStart banner, monitor-bridge's GitOps Status, the deploy UI's state
    # panel — already name it to the operator who can act. `gitops_markers` is a verbatim copy
    # of the deployer's table, so this file carries the parser whether it reads it or not.
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


def delta(
    before: list[OpenPR],
    after: list[OpenPR],
    states: Mapping[int, str],
    handed: tuple[int, ...] | None = (),
) -> Delta:
    """What actually moved. This, not the session's summary, is what the digest reports.

    `states` maps a PR that left the open set to its GitHub state (`MERGED` or `CLOSED`). A PR
    missing from it is `unread` rather than assumed merged, so a failed lookup cannot read as
    a landing.
    """
    b = {p.number for p in before}
    a = {p.number for p in after}
    gone = sorted(b - a)
    return Delta(
        resolved=tuple(n for n in gone if states.get(n) == "MERGED"),
        remaining=tuple(sorted(b & a)),
        opened=tuple(sorted(a - b)),
        closed=tuple(n for n in gone if states.get(n) == "CLOSED"),
        unread=tuple(n for n in gone if states.get(n) not in ("MERGED", "CLOSED")),
        handed_off=handed,
    )


def handed_off(
    before: list[OpenPR] | None, after: list[OpenPR] | None, branch: str
) -> tuple[int, ...] | None:
    """The superseding PRs this run opened and left for a person, from the own-account census.

    The session's account is also every interactive session's account, and those name their
    branches `worktree-renovate-<slug>` too, so author alone would list their PRs. The prompt
    pins a hand-off to `<branch>-<renovate pr>`, and only that name or the run branch itself
    counts. A PR already open before the run is an earlier hand-off, not this run's. Either
    census missing returns None, never an empty tuple.
    """
    if before is None or after is None:
        return None
    known = {p.number for p in before}
    return tuple(
        sorted(
            p.number
            for p in after
            if p.number not in known
            and (p.branch == branch or p.branch.startswith(branch + "-"))
        )
    )


def _nums(numbers: tuple[int, ...]) -> str:
    return ", ".join(f"#{n}" for n in numbers)


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
        head = f"✅ renovate-agent resolved {_nums(moved.resolved)} on {host}"
    elif moved.closed or moved.unread:
        # Nothing merged, so nothing landed. A superseding PR the session opened is listed
        # below as handed off, from the own-account census rather than the session's summary.
        head = f"⚠️ renovate-agent ran on {host} and merged no Renovate PR"
    else:
        head = f"⚠️ renovate-agent ran on {host} and no Renovate PR changed state"

    lines = [head]
    if moved.closed:
        lines.append(
            f"closed without merging (superseded or dropped): {_nums(moved.closed)}"
        )
    if moved.unread:
        lines.append(f"left the open set, state unreadable: {_nums(moved.unread)}")
    if moved.handed_off is None:
        lines.append(
            "hand-off census unreadable: a superseding PR this run opened may be open "
            "and unlisted"
        )
    elif moved.handed_off:
        lines.append(
            "handed off, open for a person to land: " + _nums(moved.handed_off)
        )
    if moved.remaining:
        lines.append("still open: " + _nums(moved.remaining))
    if moved.opened:
        lines.append("opened during the run: " + _nums(moved.opened))
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
