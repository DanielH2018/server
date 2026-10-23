"""Log-pipeline verdicts for check.py — shipper drops, swallowed verdicts, OTel and Tempo errors.

These decide; `checks/logs.py` fetches. Each takes its inputs as arguments and reads no
module-level config, which is what makes it safe to live here — see bridge/parsing.py's header
for the rule and why breaking it fails silently rather than loudly.

Split out of verdicts/service.py on 2026-09-04. That module had become the catch-all: it held
the n8n, *arr, Prowlarr, GitOps and HA verdicts alongside these two, which only checks/logs.py
consumes, so its name said less about its contents with every addition.
"""

import re


# Loki's discard reason for an entry that arrived outside its accept window. Distinct from every
# other reason in kind, not just in name: rate_limited / stream_limited / line_too_long are
# throughput or limit faults that recur until someone changes something, while this one is a
# backfill arriving late — which is what a shipper does for hours after the node it runs on
# rebooted. `backlog_grace_active` drops it for that window and nothing else.
TOO_FAR_BEHIND = "too_far_behind"


def shipper_dropped(
    client_count: float | None,
    server_reasons: list[tuple[str, float]] | None,
    window: str,
    threshold: float,
    backlog_grace_active: bool = False,
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

    `backlog_grace_active` drops the `too_far_behind` reason from the server side and says so in
    the message. The weekly reboot makes Alloy ship a backlog for hours, and Loki discards the
    part that arrives outside its accept window under exactly that reason — 4.4h of red on
    2026-09-27 for a fault nobody acts on (#2783). Every other reason and the whole client side
    stay live inside the window, so a real throughput fault on a reboot morning still pages.
    """
    client_n = client_count or 0.0
    server_reasons = server_reasons or []
    grace_note = ""
    if backlog_grace_active:
        held = sum(c for r, c in server_reasons if r == TOO_FAR_BEHIND)
        if held:
            server_reasons = [r for r in server_reasons if r[0] != TOO_FAR_BEHIND]
            grace_note = (
                ", holding %.0f %s server-side discards: the node rebooted inside "
                "SHIPPER_BACKLOG_GRACE_S and the shipper is still catching up"
                % (held, TOO_FAR_BEHIND)
            )
    server_n = sum(c for _, c in server_reasons)
    n = max(client_n, server_n)
    if n <= threshold:
        return True, "shipper drops ok (client %.0f, server %.0f in %s)%s" % (
            client_n,
            server_n,
            window,
            grace_note,
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


def otelcol_export_failures(
    failed_count: float | None,
    window: str,
    threshold: float,
) -> tuple[bool, str]:
    """Pure: did the OTel collector give up on exporting telemetry past `threshold`?

    (ok, msg). `failed_count` = sum(increase(<send-failed counters>[window])) across every
    signal the collector exports, None when the family has no series — which is the normal
    state, since the collector mints a send-failed counter only once an export has failed.

    `increase()` over a window rather than the raw counter, because the counters are cumulative:
    a single failure during a collector restart would otherwise hold this red forever.

    The threshold is 0 by default rather than a churn allowance: unlike a dropped log line,
    there is no rate of failed exports that is ordinary. Reported truncated to whole items,
    the way the host cron this replaced reported it, because a fractional `increase()`
    extrapolation is an artefact of the window rather than a count of anything.
    """
    n = failed_count or 0.0
    if n <= threshold:
        return True, "no collector export failures in %s" % window
    return False, (
        "collector dropped %d telemetry item(s) in %s (> %.0f) — exports failing, so the data "
        "reaches neither Loki nor Tempo and no other counter records the loss"
        % (int(n), window, threshold)
    )


def tempo_poll_errors(
    error_count: float | None,
    window: str,
    threshold: float,
) -> tuple[bool, str]:
    """Pure: did Tempo's blocklist poll fail past `threshold` in `window`?

    (ok, msg). `error_count` = increase(tempodb_blocklist_tenant_index_errors_total[window]),
    None when the counter has no series, which is its state until the first failure.

    A failed poll is not a partial result: Tempo keeps the previous blocklist, which is empty
    after a restart, so stored traces become unsearchable and retention stops (#4626). The
    message says where to look, because it lands on a tile named for the log shipper.
    """
    n = error_count or 0.0
    if n <= threshold:
        return True, "no Tempo blocklist poll errors in %s" % window
    return False, (
        "Tempo blocklist poll failed %d time(s) in %s (> %.0f) — stored traces are unsearchable "
        "and retention is stopped. Find the block under tempo-data's blocks/single-tenant/ whose "
        "meta.json is empty or invalid and move it off the volume (#4626)"
        % (int(n), window, threshold)
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
# edge's page, so the pair is `http=… rc=…` followed by any further `k=v` words. The Python
# twin, setup/common's kuma_push.py, logs `http=<code>[ by=kuma]` with no rc, or
# `error=<exception class>` for a transport failure, so `rc=` is one of the optional words. The
# transient-retry line reads `push failed transiently (...)` and matches neither.
_RUN_RE = re.compile(r"^status=(?P<status>up|down)\b")
_SWALLOWED_RE = re.compile(
    r"^push failed \((?:(?P<detail>(?:http|error)=\S+(?: [a-z]+=\S+)*)\) \()?"
    r"status=(?P<status>up|down):\s*(?P<msg>.*?)\)?$"
)
# The word kuma-push-lib.sh appends when the response was `application/json`: Kuma's push
# route answered. Traefik's no-router 404 is `text/plain` and carries nothing. Kuma answers 404
# for ANY exception in its push handler, so the word does not say why: `Monitor not found or
# not active.` (a token no live monitor holds) and a DB error read the same here (#4481). The
# library logs Kuma's own `msg` on the line before, prefixed KUMA_REASON_PREFIX.
KUMA_REJECTED = "by=kuma"
KUMA_REASON_PREFIX = "push rejected by kuma"


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

# The producers whose Kuma tile M6 is deleting (docs/target-architecture/monitoring/m6-retire-kuma.md,
# "The cutover order"). From a batch's Cutover-1a until its Cutover-2, the producer still pushes
# into a token whose tile is gone, and kuma_push logs that refused push as a `rejected` line. A
# tag listed here is left out of the `rejected` set ONLY: a lost `status=down` push still counts,
# and its `status=` lines still count as landed siblings. Each batch's shadow PR adds its tags, a
# week before its Cutover-1a, so the exclusion is deployed before any tile goes. A tag is the one
# `parse_push_line` reads: a shell script's `logger -t` name, or a Python producer's syslog
# identifier. The set goes with `swallowed_verdicts` in C-7. One tag per line, so a batch's
# additions merge without touching another's.
KUMA_RETIRING_TAGS = frozenset(
    {
        "claude-otel-health",
        # B2: initial_setup. secret_rotation.py's own push logs under a `warning:` prefix that
        # parse_push_line does not match, so only the wrapper's tag is listed.
        "docs-refresh",
        "etcd-snapshot-offbox",
        "eval-run",
        "live-drift-check",
        "loki-read-route-health",
        "longhorn-backup-health",
        "manifest-prune-check",
        "release-staleness-check",
        "secret-rotation-audit",
        "setup-drift-check",
        "sync-artifacts",
    }
)


# Producers whose Kuma tile M6 is retiring. From a batch's Cutover-1a the tile is gone, so the
# producer's push is refused until its Cutover-2 drops it, and a refused push is not a fault.
# `swallowed_verdicts` leaves these tags out of the `rejected` set only: a lost `status=down`
# push still counts, and a `status=` line still counts as a landed sibling. Each B1 to B7 shadow
# PR adds its producers' tags, as `parse_push_line` reads them, and C-7 deletes the set. Pinned
# against the tree by tests/test_check_swallowed_verdicts.py: a tag must be one a rendered
# producer logs under. See docs/target-architecture/monitoring/m6-retire-kuma.md, *The cutover
# order*.
KUMA_RETIRING_TAGS = frozenset(
    {
        "claude-otel-health",
        "configarr-health",
        "crowdsec-appsec-verify",
        "crowdsec-home-allowlist",
        "crowdsec-remote-allowlist",
        "janitorr-health",
        "sync-artifacts",
    }
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

    A REJECTED push — Kuma itself answered, `by=kuma` on the line — is counted whatever its
    status and whether or not a sibling landed (#1803). Kuma answering means the edge and Kuma
    are both up, so no other tile pages. Kuma answers 404 for any exception, so the cause is
    one of two, and only Kuma's `msg` on the library's reason line tells them apart (#4481).
    A token no live monitor holds (a static-monitors re-mint deployed ahead of the cron's
    re-render, or a tile paused by hand) has no tile to go red at its deadline, and it stays
    until one side is redeployed. A Kuma-side error, such as the 2026-10-11
    `SQLITE_CONSTRAINT` on `stat_hourly`, matched the token and survived a redeploy. A fleet-wide rejection
    (every token gone — Kuma restored from an old backup) takes this tile's own token with
    it; the three HC_ROUTED_TAGS crons page that case through healthchecks.io within their
    own cycle, which is why they stay excluded here too. A rejection wins the message over
    a swallowed DOWN on another tag in the same window: the tile is red either way, and the
    swallowed one is named on the next cycle once the rejection is cleared.

    A tag in HC_ROUTED_TAGS is not counted either: its own script already pages a lost push
    through healthchecks.io.

    A REJECTED push from a tag in KUMA_RETIRING_TAGS is not counted either: its tile is being
    deleted, so the token matches no tile by design. Its lost DOWN pushes still count.
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
        if v[2] == "rejected"
        and t not in HC_ROUTED_TAGS
        and t not in KUMA_RETIRING_TAGS
    }
    if rejected:
        named = "; ".join(
            "%s on %s (%s, status=%s)" % (tag, v[1], v[4], v[3])
            for tag, v in sorted(rejected.items())
        )
        return False, (
            "Kuma rejected the push for %s in %s, so its verdicts reach nobody. Kuma answers "
            "404 for any error, so read its reason on the '%s' line before the failure "
            "(`journalctl -t <tag>` on a host, the pod log for a pod pusher): "
            "`Monitor not found or not active.` means the cron and uptime-kuma's static "
            "monitors carry different tokens (redeploy whichever is behind); anything else "
            "is Kuma failing on its own side" % (named, window, KUMA_REASON_PREFIX)
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
