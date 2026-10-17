"""The Claude Code cgroup verdict — `claude-rc.service` and `user.slice/user-1000.slice`.

Its own module rather than a section of `verdicts/host.py` because that file was 597 lines against
the 600-line cap in `ansible/tests/repo/test_module_length_ratchet.py`. Same split idiom as
`checks/host_edge.py` and `checks/host_thermal.py`.

Decides; does not fetch. Takes its inputs as arguments and reads no module-level config — see
`bridge/parsing.py`'s header for the rule and why breaking it fails silently rather than loudly.
"""

from collections.abc import Sequence


def _where(m: dict) -> str:
    """`origin/cgroup`, so a fault on the second session host names the host."""
    return "%s/%s" % (m.get("origin", "?"), m.get("cgroup", "?"))


def claude_cgroup_verdict(
    stalls: Sequence[tuple[dict, float]],
    events: Sequence[tuple[dict, float]],
    expected: Sequence[str],
    stall_max_pct: float,
    stall_window: str,
    event_window: str,
    uid_procs: Sequence[tuple[dict, float]] = (),
    *,
    expected_by_origin: Sequence[tuple[str, Sequence[str]]] = (),
) -> tuple[bool, str]:
    """Pure: judge the `claude_cgroup_*` textfile series for the Claude Code cgroups.

    `stalls` is the PSI `full` stall rate as a PERCENT of wall time, per cgroup; `events` is the
    increase in each watched memory.events counter, per (cgroup, event). `expected` names the
    cgroups whose absence is itself a fault. `expected_by_origin` pairs each host with the
    cgroups THAT host must report, and replaces `expected` for arm 4 when it is given.

    Three arms, in the order an operator needs them:

    1. A watched event increased. The kernel killed something in this cgroup, or a hard cap was
       hit. Reported first because it outranks a stall — a stall is the warning, a kill is the
       thing the warning was about.
    2. The stall rate is over `stall_max_pct`. PSI `full` counts time when NO task in the cgroup
       made progress, so this is the 2026-09-05 shape (#1243) while it is still only a stall.
    3. An expected cgroup is not reporting at all. Fails rather than passing, because an empty
       vector is exactly how a dead writer reads green forever — the failure the kopia
       `b2_billable_bytes` board took two weeks to notice, and the reason check_disk and
       check_mem carry _host_origin_shortfall. With `expected_by_origin`, the missing set is
       per (origin, cgroup) (#4870). Both session hosts report `fleet`, so a union across hosts
       read daniel-server's dead writer as green while daniel-box still reported. The lists
       differ by host because only a host running claude-rc.service has its cgroup. An empty
       `expected_by_origin` judges `expected` across all hosts, for a caller that names none.

    Arm 3 is deliberately LAST, matching check_disk and check_mem: a cgroup that IS reporting and
    IS in trouble pages ahead of a complaint about the absent one.

    A watched uid running Claude Code is a NOTE on the passing message, not an arm. `uid_procs`
    is `claude_uid_processes` per (origin, uid); a value above 0 is a session started as the
    wrong account (slice 6 of docs/claude-agent-user.md).

    DECIDED: note, never page (operator, 2026-10-17, #5125). As an arm it held the Memory tile
    DOWN near-continuously from 2026-10-09, because background jobs and fan-out still start as
    the operator's uid. That masked any real memory page behind it, and an alert-triggered
    triage run would start on it every poll. The note keeps it visible on the tile.
    """
    wrong_uid = sorted(
        "%s/uid %s: %.0f" % (m.get("origin", "?"), m.get("uid", "?"), n)
        for m, n in uid_procs
        if n > 0
    )
    note = (
        " · note: claude running as a retired uid (start sessions with claude-agents): %s"
        % ", ".join(wrong_uid)
        if wrong_uid
        else ""
    )
    firing = [
        ("%s %s +%.0f" % (_where(m), m.get("event", "?"), v), v)
        for m, v in events
        if v > 0
    ]
    if firing:
        firing.sort(key=lambda dv: -dv[1])
        return False, "claude cgroup memory events in %s: %s" % (
            event_window,
            ", ".join(d for d, _ in firing[:5]),
        )
    stalled = [
        ("%s %.1f%%" % (_where(m), pct), pct)
        for m, pct in stalls
        if pct > stall_max_pct
    ]
    if stalled:
        stalled.sort(key=lambda dv: -dv[1])
        return False, "claude cgroup memory-stalled over %g%% of %s: %s" % (
            stall_max_pct,
            stall_window,
            ", ".join(d for d, _ in stalled[:5]),
        )
    if expected_by_origin:
        reporting = {(m.get("origin"), m.get("cgroup")) for m, _ in stalls}
        missing = [
            "%s/%s" % (o, c)
            for o, cgroups in sorted(expected_by_origin)
            for c in cgroups
            if (o, c) not in reporting
        ]
    else:
        names = {m.get("cgroup") for m, _ in stalls}
        missing = [c for c in expected if c not in names]
    if missing:
        return (
            False,
            "claude cgroup metrics UNKNOWN: %s not reporting — that cgroup is NOT being checked"
            % ", ".join(missing),
        )
    worst = max((pct for _, pct in stalls), default=0.0)
    return True, "claude cgroups stalled %.2f%% of %s%s" % (worst, stall_window, note)
