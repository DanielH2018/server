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

import json
from collections.abc import Mapping
from dataclasses import dataclass


# Output before the result object is warning or progress text, so the parse scans rather than loads.
_RESULT_TYPE = "result"

# host_lib.discord_post caps the message at 1900 characters; this leaves room for the delta lines.
_SUMMARY_CHARS = 900
_DIGEST_CHARS = 1850


@dataclass(frozen=True)
class OpenPR:
    number: int
    title: str
    url: str = ""
    # The before/after-census difference is the only signal a comment-only triage leaves on an open
    # PR.
    updated_at: str = ""


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
    session_id: str = ""


@dataclass(frozen=True)
class Delta:
    """The measured effect of the run: which Renovate PRs left the open set, and which stayed.

    Leaving the open set is not landing. The renovate-prs skill finishes a `manual —` bump by
    closing the Renovate PR in favour of a superseding PR that stays open for a person,
    so a departed PR is split by the state GitHub reports for it: `resolved` merged, `closed`
    was closed without merging, and `unread` is one whose state lookup failed.

    `touched` is the subset of `remaining` whose `updatedAt` moved during the run — the only
    census-visible trace of a session that commented on or labelled a PR and left it open.
    A Renovate rebase during the session moves `updatedAt` too, so it is evidence
    something touched the PR, not proof the session did.
    """

    resolved: tuple[int, ...]
    remaining: tuple[int, ...]
    opened: tuple[int, ...]
    closed: tuple[int, ...] = ()
    unread: tuple[int, ...] = ()
    touched: tuple[int, ...] = ()


def decide(open_prs: list[OpenPR]) -> Gate:
    """Whether to spend a session this tick.

    The tick does not read `DEPLOY_HOLD`: the lander's `land.sh` refuses a landing under it.
    """
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
        session_id=str(obj.get("session_id") or ""),
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
        touched=_touched(before, after),
    )


def _touched(before: list[OpenPR], after: list[OpenPR]) -> tuple[int, ...]:
    """The still-open PRs whose `updatedAt` moved between the two censuses.

    Both timestamps must be present and different. An empty one means the census could not
    read it, and a PR whose timestamp is unreadable must read as no evidence rather than as
    evidence of a touch — the whole point of the field is that a comment-only triage is
    otherwise invisible.
    """
    was = {p.number: p.updated_at for p in before}
    return tuple(
        sorted(
            p.number
            for p in after
            if p.number in was
            and was[p.number]
            and p.updated_at
            and was[p.number] != p.updated_at
        )
    )


def run_record(
    ts: int,
    result: str,
    reason: str = "",
    moved: Delta | None = None,
    outcome: Outcome | None = None,
) -> str:
    """One JSON line for `runs.jsonl`, the per-tick record the usage count reads.

    Written on every exit path, skips included, so "how many ticks spent a session" and
    "how many PRs each one merged, closed or left open" are both a count over
    one file. `result` is `skipped`, `blocked`, `ran`, `failed` or `crashed`. The PR fields
    come from the measured delta, never the session's summary. A triage that only comments on
    or labels a PR moves nothing out of the open set, so `touched` — the left-open PRs whose
    `updatedAt` moved during the run — is the only field that records it, and a Renovate
    rebase mid-session moves `updatedAt` as well.
    """
    rec: dict = {"ts": ts, "result": result, "reason": reason}
    if moved is not None:
        rec |= {
            "merged": list(moved.resolved),
            "closed": list(moved.closed),
            "unread": list(moved.unread),
            "left_open": list(moved.remaining),
            "touched": list(moved.touched),
            "opened": list(moved.opened),
        }
    if outcome is not None:
        rec |= {
            "cost_usd": round(outcome.cost_usd, 4),
            "turns": outcome.turns,
            "denials": len(outcome.denials),
        }
        if outcome.session_id:
            rec["session_id"] = outcome.session_id
    return json.dumps(rec, sort_keys=True)


def _nums(numbers: tuple[int, ...]) -> str:
    return ", ".join(f"#{n}" for n in numbers)


def render_skip(gate: Gate, host: str) -> str:
    return f"renovate-agent: skipped on {host} — {gate.reason}."


def render_digest(
    outcome: Outcome,
    moved: Delta,
    host: str,
    log_path: str,
    unlanded: list[str] | None = None,
) -> str:
    """The Discord digest, headlined by the measured delta.

    The first line answers "did anything move", because that is the question a reader of a
    daily automation post is actually asking.
    """
    if not outcome.ok:
        head = f"🚨 renovate-agent FAILED on {host} — {outcome.error}"
    elif moved.resolved:
        head = f"✅ renovate-agent resolved {_nums(moved.resolved)} on {host}"
    elif moved.closed or moved.unread:
        # Nothing merged, so nothing landed.
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
    if moved.remaining:
        lines.append("still open: " + _nums(moved.remaining))
    if moved.touched:
        # A rebase by Renovate moves updatedAt too, so this line says the PR changed during
        # the run, never that the session is what changed it.
        lines.append(
            "left open but updated during the run (a triage comment, or a Renovate rebase): "
            + _nums(moved.touched)
        )
    if moved.opened:
        lines.append("opened during the run: " + _nums(moved.opened))
    if unlanded:
        lines.append("nominated, not landed: " + "; ".join(unlanded))
    if outcome.denials:
        # A denial is the failure the design rests on not happening: refused writes read green while
        # doing nothing.
        lines.append("permission denials: " + ", ".join(sorted(set(outcome.denials))))
    tail = [f"{outcome.turns} turns, ${outcome.cost_usd:.2f} — transcript: {log_path}"]
    if outcome.summary:
        # Every line around the summary keeps its full text; only the summary gives way.
        room = _DIGEST_CHARS - len("\n".join(lines + tail)) - len("\n> ")
        quoted = outcome.summary.strip().replace("\n", "\n> ")
        cut = quoted[: max(0, min(_SUMMARY_CHARS, room))]
        if cut:
            lines.append("> " + cut)
    return "\n".join(lines + tail)
