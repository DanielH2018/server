"""Log-pipeline verdicts for check.py — Loki ingestion freshness and shipper/server drops.

These decide; `checks/logs.py` fetches. Each takes its inputs as arguments and reads no
module-level config, which is what makes it safe to live here — see bridge/parsing.py's header
for the rule and why breaking it fails silently rather than loudly.

Split out of verdicts/service.py on 2026-09-04. That module had become the catch-all: it held
the n8n, *arr, Prowlarr, GitOps and HA verdicts alongside these two, which only checks/logs.py
consumes, so its name said less about its contents with every addition.
"""

import re


def loki_ingestion_fresh(count: float | None, window: str) -> tuple[bool, str]:
    """Decide log-pipeline freshness from the line count over `window` (None = no series)."""
    if not count:  # None or 0 — nothing shipped: promtail dead, positions corrupt, etc.
        return (
            False,
            "no log lines ingested in %s — promtail/Loki pipeline silent" % window,
        )
    return True, "%d log lines in %s" % (int(count), window)


def shipper_dropped(
    client_count: float | None,
    server_reasons: list[tuple[str, float]] | None,
    window: str,
    threshold: float,
) -> tuple[bool, str]:
    """Pure: did the shipper give up on entries, or did Loki discard them, past `threshold`?

    (ok, msg). Reports whichever side lost MORE over `window`.

    `client_count` = sum(increase(<dropped-entries counters>[window])) over ALL drop reasons
    (ingester_error / rate_limited / stream_limited / line_too_long) and both shippers (Alloy on
    the cluster, Promtail on the Pi), None when no counter has a series (reads as 0). That is
    the CLIENT side of the pipe: a shipper only counts what IT gave up on.

    `server_reasons` = [(reason, count), ...] from Loki's own distributor-side
    `loki_discarded_samples_total`, one entry per `reason` label, [] when no series. This is
    the SERVER side: entries Loki itself rejected, including a burst the shipper never
    attributes to itself. Measured 2026-09-03: Loki discarded 161,573 samples server-side
    (reason=too_far_behind) in a 24h window where the client-side counter recorded only 1,027
    (reason=ingester_error) — the client side alone understated real loss by ~150x, and would
    not have fired at all had the client not separately logged an unrelated ingester_error.

    Whichever total is larger decides the verdict, so a burst attributed to only one side still
    pages. The server side names the reason that fired: `too_far_behind` means entries arrived
    outside Loki's accept window — a clock/backfill problem — where every other reason
    (rate_limited / stream_limited / line_too_long) is a throughput or limit problem, and the
    operator needs to know which they are chasing.
    """
    client_n = client_count or 0.0
    server_reasons = server_reasons or []
    server_n = sum(c for _, c in server_reasons)
    n = max(client_n, server_n)
    if n <= threshold:
        return True, "shipper drops ok (client %.0f, server %.0f in %s)" % (
            client_n,
            server_n,
            window,
        )
    if server_n >= client_n:
        top_reason, top_count = max(server_reasons, key=lambda kv: kv[1])
        return False, (
            "Loki discarded %.0f entries in %s (> %.0f), reason=%s (%.0f) — server-side "
            "loss the shipper's own counter did not attribute to itself"
            % (server_n, window, threshold, top_reason, top_count)
        )
    return False, (
        "log shipper dropped %.0f entries in %s (> %.0f) — partial log loss"
        % (client_n, window, threshold)
    )


# The host crons' push outcomes, as rsyslog ships them: "<iso-ts> <host> <tag>[pid]: <rest>".
# Same prefix shape scripts/diagnostics/probe_lib/alerts.py parses (`_SYSLOG_LINE_RE`), and
# the Pi's health.log is written in it deliberately so the one reader covers both.
_SYSLOG_LINE_RE = re.compile(
    r"^\S+\s+(?P<host>\S+)\s+(?P<tag>[A-Za-z0-9_.-]+?)(?:\[\d+\])?:\s+(?P<rest>.*)$"
)
# A cron logs its verdict BEFORE it pushes — `logger -t <tag> "status=<up|down> <msg>"` — so
# this is the record that a run happened. kuma-push-lib.sh then logs only when the push is
# lost: `push failed (http=<code> rc=<rc>) (status=<up|down>: <msg>)`, after its retries, or
# `push failed (status=...)` with no code on the Pi and in the pre-retry library. The
# transient-retry line reads `push failed transiently (...)` and matches neither.
_RUN_RE = re.compile(r"^status=(?P<status>up|down)\b")
_SWALLOWED_RE = re.compile(
    r"^push failed \((?:(?P<detail>http=\S+ rc=\S+)\) \()?status=(?P<status>up|down):"
    r"\s*(?P<msg>.*?)\)?$"
)


def parse_push_line(line: str) -> tuple[str, str, str, str, str] | None:
    """(tag, host, kind, status, detail) for a push-outcome syslog line, else None.

    `kind` is "run" for a cron's own `status=` line and "swallowed" for the library's final
    `push failed` line. `detail` carries the http/rc pair on a swallowed line and the message
    on a run line, so a page can name the failure class without a journal round trip.
    """
    m = _SYSLOG_LINE_RE.match(line)
    if not m:
        return None
    rest = m["rest"]
    run = _RUN_RE.match(rest)
    if run:
        return (
            m["tag"],
            m["host"],
            "run",
            run["status"],
            rest[len(run.group(0)) :].strip(),
        )
    lost = _SWALLOWED_RE.match(rest)
    if lost:
        return (
            m["tag"],
            m["host"],
            "swallowed",
            lost["status"],
            lost["detail"] or "no http code",
        )
    return None


# The crons that read KUMA_PUSH_OK and route a healthchecks.io `/fail` on a lost push, which
# alerts at once. Their lost DOWN already pages, so counting it here is one root cause twice;
# their `status=` lines still count as landed siblings. Pinned against the tree by
# tests/test_check_swallowed_verdicts.py: a fourth reader has to be added here too.
HC_ROUTED_TAGS = frozenset(
    {"longhorn-backup-health", "pi-sd-health", "pi-recovery-health"}
)


def swallowed_verdicts(
    lines: list[tuple[int, str]], window: str, truncated: bool
) -> tuple[bool, str]:
    """Pure: did a cron's most recent DOWN verdict fail to reach Kuma while its siblings' landed?

    `lines` is the ordered [(ts, line), ...] a range query returned for the push-outcome
    lines over `window`; `truncated` says the fetch hit its cap. Per tag, the LATEST line
    decides: a tag whose newest push-outcome line is a swallowed `status=down` is carrying a
    verdict Kuma never saw, and its tile reads stale-green until the heartbeat deadline —
    up to 25h for a daily producer (#1869). A tag whose newest line is a `status=` run with no
    failure after it landed its push.

    Pages ONLY when some other tag landed a push in the same window. When nothing lands, the
    cause is fleet-wide — Kuma unreachable, the edge answering 404 for every route, the host
    that runs Kuma down — and another tile already pages for it; a second page for one root
    cause is the rule gates.py applies everywhere else, and it is the population
    uptime-kuma/CLAUDE.md measured when it rejected a plain count of push failures. The one
    case this exists for — one tag's DOWN lost while the rest of the fleet pushes fine — is
    the one that count could not separate. Measured: release-staleness-check's http=500 on
    2026-09-10 13:01 and setup-drift-check's on 2026-08-29 16:47, each alone in its window.

    A swallowed `up` is not counted. Its tile goes red at the deadline for a cron that ran,
    which is a wrong diagnosis but not a hidden finding, and the library's retry (#1010) is the
    mechanism sized for that case.

    A tag in HC_ROUTED_TAGS is not counted either: its own script already pages a lost push
    through healthchecks.io.
    """
    latest: dict[str, tuple[int, str, str, str, str]] = {}
    for ts, line in lines:
        parsed = parse_push_line(line)
        if parsed is None:
            continue
        tag, host, kind, status, detail = parsed
        prior = latest.get(tag)
        if prior is None or ts >= prior[0]:
            latest[tag] = (ts, host, kind, status, detail)
    swallowed = {
        t: v
        for t, v in latest.items()
        if v[2] == "swallowed" and v[3] == "down" and t not in HC_ROUTED_TAGS
    }
    landed = sorted(t for t, v in latest.items() if v[2] == "run")
    if not swallowed:
        if truncated:
            return True, (
                "no swallowed DOWN verdicts in the newest part of %s — fetch hit its line cap, "
                "older lines unread" % window
            )
        return True, "no swallowed DOWN verdicts in %s (%d tag(s) pushed)" % (
            window,
            len(latest),
        )
    if not landed:
        return True, (
            "%d tag(s) lost a DOWN verdict in %s and none landed a push — fleet-wide, "
            "owned by the edge/host tiles: %s"
            % (len(swallowed), window, ", ".join(sorted(swallowed)))
        )
    named = "; ".join(
        "%s on %s (%s)" % (tag, v[1], v[4]) for tag, v in sorted(swallowed.items())
    )
    return False, (
        "DOWN verdict swallowed for %s while %d sibling tag(s) landed in %s — the tile "
        "reads stale until its deadline; `journalctl -t <tag>` has the verdict"
        % (named, len(landed), window)
    )
