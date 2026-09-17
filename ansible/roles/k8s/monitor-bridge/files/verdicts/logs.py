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
# `push failed (status=...)` with no code on the Pi and in the pre-retry library. Since #1803
# the http/rc pair carries ` by=kuma` when the answer was Kuma's own JSON rather than the
# edge's page, so the pair is `http=… rc=…` followed by any further `k=v` words. The
# transient-retry line reads `push failed transiently (...)` and matches neither.
_RUN_RE = re.compile(r"^status=(?P<status>up|down)\b")
_SWALLOWED_RE = re.compile(
    r"^push failed \((?:(?P<detail>http=\S+ rc=\S+(?: [a-z]+=\S+)*)\) \()?"
    r"status=(?P<status>up|down):\s*(?P<msg>.*?)\)?$"
)
# The word kuma-push-lib.sh appends when the response was `application/json`: Kuma's push
# route answered, and what it said was `Monitor not found or not active.` — no live monitor
# holds the token the cron pushed. Traefik's no-router 404 is `text/plain` and carries nothing.
KUMA_REJECTED = "by=kuma"


def parse_push_line(line: str) -> tuple[str, str, str, str, str] | None:
    """(tag, host, kind, status, detail) for a push-outcome syslog line, else None.

    `kind` is "run" for a cron's own `status=` line, "swallowed" for the library's final
    `push failed` line, and "rejected" for that line when Kuma itself answered it
    (KUMA_REJECTED in the http/rc pair). `detail` carries the http/rc pair on a swallowed or
    rejected line and the message on a run line, so a page can name the failure class without
    a journal round trip.
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
        detail = lost["detail"] or "no http code"
        return (
            m["tag"],
            m["host"],
            "rejected" if KUMA_REJECTED in detail.split() else "swallowed",
            lost["status"],
            detail,
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

    A REJECTED push — Kuma itself answered `Monitor not found or not active.`, `by=kuma` on
    the line — is counted whatever its status and whether or not a sibling landed (#1803).
    Kuma answering means the edge and Kuma are both up, so no other tile pages; and a token no
    live monitor holds has no tile to go red at its deadline, so the deadline backstop above
    does not exist for it. The cause is the cron and the static monitors carrying different
    tokens — a static-monitors re-mint deployed ahead of the cron's re-render, or a tile
    paused by hand — and it stays until one side is redeployed. A fleet-wide rejection
    (every token gone — Kuma restored from an old backup) takes this tile's own token with
    it; the three HC_ROUTED_TAGS crons page that case through healthchecks.io within their
    own cycle, which is why they stay excluded here too. A rejection wins the message over
    a swallowed DOWN on another tag in the same window: the tile is red either way, and the
    swallowed one is named on the next cycle once the rejection is cleared.

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
    rejected = {
        t: v
        for t, v in latest.items()
        if v[2] == "rejected" and t not in HC_ROUTED_TAGS
    }
    if rejected:
        named = "; ".join(
            "%s on %s (%s, status=%s)" % (tag, v[1], v[4], v[3])
            for tag, v in sorted(rejected.items())
        )
        return False, (
            "Kuma rejected the push for %s in %s — no live monitor holds the token the cron "
            "pushes, so its verdicts reach nobody and no tile goes red; redeploy whichever of "
            "the cron and uptime-kuma's static monitors is behind" % (named, window)
        )
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


# Kuma's `server/model/monitor.js` logs a failed send as TWO error-level lines and moves on:
# `Cannot send notification to <name>`, then the error itself (`log.error("monitor", e)`), which
# for an HTTP provider is the axios message `throwGeneralAxiosError` builds — `Error: Request
# failed with status code 429 (code=ERR_BAD_REQUEST) (HTTP 429 Too Many Requests) {...body}`.
# A failed send is not retried, so the alert it carried reached nobody (#1891). Both lines are
# printed through Kuma's colour logger, so ANSI escapes surround the level tag and can follow
# the name; the name runs to the first escape or the end of the line. #1895 filed the reason
# as debug-only and absent from Loki; measured 2026-09-17 over 14 days it is there at ERROR
# level, one reason line per drop (74 drops, 69 x HTTP 429 + 5 x HTTP 400), so the tile can
# say why without raising Kuma's log level — which would write the webhook URL to Loki
# (uptime-kuma/CLAUDE.md, the AutoKuma notification-rewrite trap).
_NOTIFY_FAILURE_RE = re.compile(
    r"Cannot send notification to (?P<name>[^\x1b]+?)\s*(?:\x1b|$)"
)
# The reason line: `ERROR:` (optionally colour-reset), then `Error: <message>`. Kuma's own
# check-failure lines are WARN and read `Pending: Request failed with status code 500`, so
# anchoring on the ERROR tag is what keeps a flapping monitor's probes out of the reason set.
_NOTIFY_REASON_RE = re.compile(r"ERROR:(?:\x1b\[[0-9;]*m)?\s*Error: (?P<msg>.+)$")
# The status the provider recorded, in the form Kuma writes it: `(HTTP 429 Too Many Requests)`.
_HTTP_STATUS_RE = re.compile(r"\(HTTP (?P<status>\d{3}[^)]*)\)")
_URL_RE = re.compile(r"https?://\S+")
_REASON_MAX = 60


def parse_notify_failure_line(line: str) -> str | None:
    """The notification name Kuma failed to send to, else None."""
    m = _NOTIFY_FAILURE_RE.search(line)
    return m["name"] if m else None


def parse_notify_reason_line(line: str) -> str | None:
    """The reason Kuma recorded for a dropped send, else None for any other line.

    An HTTP provider's reason is reduced to its status — `HTTP 429 Too Many Requests` — rather
    than the whole axios message: the response body Kuma appends is the provider's JSON, and the
    message can carry the request URL, which for Discord IS the webhook secret. A non-HTTP
    reason (an SMTP login failure) keeps the message's first %d characters with any URL
    replaced, for the same secrecy reason. The tile's message reaches Discord and email.
    """ % _REASON_MAX
    m = _NOTIFY_REASON_RE.search(line)
    if not m:
        return None
    status = _HTTP_STATUS_RE.search(m["msg"])
    if status:
        return "HTTP " + status["status"].strip()
    return _URL_RE.sub("<url>", m["msg"])[:_REASON_MAX].strip()


def kuma_notify_failures(
    lines: list[tuple[int, str]], window: str, truncated: bool
) -> tuple[bool, str]:
    """Pure: did Kuma drop a notification send inside `window`, and why?

    `lines` is [(ts, line), ...] for the failure AND reason lines a range query returned over
    `window`; `truncated` says the fetch hit its cap. Every failure counts — a drop is a drop
    whether the tile in question was transitioning or resending, and Kuma's log does not say
    which. The verdict names each notification with its drop count and the reasons Kuma
    recorded with theirs, so a Discord rate-limit (HTTP 429) and a webhook Discord rejected
    (HTTP 400) read differently on the tile, and the tile itself notifies BOTH channels
    (uptime-kuma's static-monitors template) — a page for a dropped Discord POST sent only
    over the same Discord webhook is the failure it reports.

    Reasons are counted as a SET beside the drops, not joined to them one to one: four drops
    landed inside two seconds on 2026-09-09 20:20, and a nearest-timestamp join would put a
    429 on a 400's drop and read as fact. A drop whose reason line is outside the window, or
    that Kuma logged without one, is reported as `reason not logged` rather than assumed.

    The window is the whole hysteresis: a drop pages for `window` and then clears, and the
    Discord tile's transition message plus this page together say "an alert went missing
    around <time>; check the tiles' current state".
    """
    counts: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for _ts, line in lines:
        name = parse_notify_failure_line(line)
        if name is not None:
            counts[name] = counts.get(name, 0) + 1
            continue
        reason = parse_notify_reason_line(line)
        if reason is not None:
            reasons[reason] = reasons.get(reason, 0) + 1
    if not counts:
        if truncated:
            return True, (
                "no dropped Kuma notifications in the newest part of %s — fetch hit its "
                "line cap, older lines unread" % window
            )
        return True, "no dropped Kuma notifications in %s" % window
    dropped = sum(counts.values())
    unexplained = dropped - sum(reasons.values())
    if unexplained > 0:
        reasons["reason not logged"] = unexplained
    named = ", ".join("%s x%d" % (n, c) for n, c in sorted(counts.items()))
    why = ", ".join(
        "%s x%d" % (r, c) for r, c in sorted(reasons.items(), key=lambda rc: -rc[1])
    )
    return False, (
        "Kuma dropped %d notification send(s) in %s (%s) — reasons: %s — Kuma does not "
        "retry a failed send, so an alert reached nobody; check the DOWN tiles' current state"
        % (dropped, window, named, why)
    )
