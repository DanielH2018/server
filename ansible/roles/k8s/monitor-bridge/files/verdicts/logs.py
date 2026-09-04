"""Log-pipeline verdicts for check.py — Loki ingestion freshness and shipper/server drops.

These decide; `checks/logs.py` fetches. Each takes its inputs as arguments and reads no
module-level config, which is what makes it safe to live here — see bridge/parsing.py's header
for the rule and why breaking it fails silently rather than loudly.

Split out of verdicts/service.py on 2026-09-04. That module had become the catch-all: it held
the n8n, *arr, Prowlarr, GitOps and HA verdicts alongside these two, which only checks/logs.py
consumes, so its name said less about its contents with every addition.
"""


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
