"""Host and hardware checks for monitor-bridge.

Covers disk, cert expiry and memory. `checks/host_thermal.py` covers SMART, host temperature
and the UPS; `checks/host_edge.py` covers Pi pressure with its published ports, and the
speedtest.

Slice 5 of the check.py split. Reads config as `cfg.X` and the fetch layer as `bridge.net.X`,
so the tests' patches on those modules reach it. `_host_origin_streaks` lives here beside
`_host_origin_shortfall`, the only code that mutates it — `checks.host_thermal` reads the floor
qualified, off this module, rather than from-importing it. Rule and enforcement:
bridge/config.py's header.
"""

from bridge.config import Config
import bridge.net


_host_origin_streaks: dict[str, int] = {}


def _host_origin_shortfall(
    cfg: Config,
    key: str,
    vec: list[tuple[dict, float]],
    what: str,
    min_origins: float | None = None,
    consecutive: float | None = None,
) -> tuple[bool, str] | None:
    """(ok, msg) when `vec` covers fewer than `min_origins` hosts, else None.

    Passes (green, but says so) while the shortfall is younger than `consecutive` cycles, so a
    reboot doesn't page; fails once it persists. Any full-coverage cycle resets. `key` separates
    the streaks so disk, memory and host temperature age independently.

    Both thresholds are PARAMETERS defaulting to the shared globals, not reads of the globals.
    check_host_temp needs a different floor from disk and memory — every host declares hwmon
    sensors, where a mountpoint need not exist everywhere — and the 2026-08-29 review M-9
    proposal added an env key that nothing read, leaving hwmon on the shared floor of 2 while
    reading as new coverage. A caller that wants a different floor passes one here; nothing
    reaches for a global whose name it happens to know.
    """
    floor = cfg.HOST_ORIGINS_MIN if min_origins is None else min_origins
    grace = cfg.HOST_ORIGINS_CONSECUTIVE if consecutive is None else consecutive
    origins = {bridge.net._origin_name(labels) for labels, _ in vec}
    if len(origins) >= floor:
        _host_origin_streaks[key] = 0
        return None
    streak = _host_origin_streaks.get(key, 0) + 1
    _host_origin_streaks[key] = streak
    seen = ", ".join(sorted(origins)) or "none"
    if streak < grace:
        return (
            True,
            "%s: only %d of %d hosts reporting (%s), cycle %d/%d — node rebooting?"
            % (
                what,
                len(origins),
                floor,
                seen,
                streak,
                grace,
            ),
        )
    return (
        False,
        "%s UNKNOWN: only %d of %d hosts reporting (%s) — the absent host is NOT being checked"
        % (
            what,
            len(origins),
            floor,
            seen,
        ),
    )


def check_disk(cfg: Config) -> tuple[bool, str]:
    """Checks whether any monitored disk mountpoint is over cfg.DISK_MAX_PCT full.

    Computes each mountpoint's used percentage per-origin (host), pairing avail and size
    from the same series so a multi-host estate can't cross-pair one host's avail with
    another's size. Returns (ok, msg).
    """
    # Percentage computed per series, then grouped by origin, so avail and size always come from
    # the SAME host and device. The previous form took max(avail) and max(size) as two separate
    # queries: fine while one estate reported, but once daniel-server and daniel-box both landed
    # in this Prometheus it paired one host's avail with the other's size, and a filling disk on
    # the smaller host produced an arbitrarily wrong percentage rather than a high one.
    breaching = []
    shortfalls = []
    for mp in cfg.DISK_MOUNTPOINTS:
        sel = bridge.net.host_metric_sel(cfg, 'mountpoint="%s"' % mp)
        vec = bridge.net.prom_vector(
            cfg,
            "max by (origin) (100 * (1 - node_filesystem_avail_bytes%s"
            " / node_filesystem_size_bytes%s))" % (sel, sel),
        )
        if not vec:
            return False, "metric unavailable for %s" % mp
        # Collected, not returned, so a host that IS reporting and IS full still pages ahead of
        # the coverage complaint — a real breach on the survivor outranks the absent host.
        short = _host_origin_shortfall(cfg, "disk:%s" % mp, vec, "disk %s" % mp)
        if short is not None:
            shortfalls.append(short)
        for labels, used_pct in vec:
            if used_pct > cfg.DISK_MAX_PCT:
                breaching.append(
                    "%s %s %.0f%%" % (bridge.net._origin_name(labels), mp, used_pct)
                )
    if breaching:
        return False, "disk over %.0f%%: %s" % (cfg.DISK_MAX_PCT, ", ".join(breaching))
    failed = [s for s in shortfalls if not s[0]]
    if failed:
        return False, "; ".join(msg for _, msg in failed)
    if shortfalls:
        return True, "; ".join(msg for _, msg in shortfalls)
    return True, "all mounts under %.0f%%" % cfg.DISK_MAX_PCT


def check_cert(cfg: Config) -> tuple[bool, str]:
    days = bridge.net.prom_scalar(
        cfg, "(min(traefik_tls_certs_not_after) - time()) / 86400"
    )
    if days is None:
        return False, "cert metric unavailable"
    if days < cfg.CERT_MIN_DAYS:
        return False, "cert expires in %.1fd (< %.0fd)" % (days, cfg.CERT_MIN_DAYS)
    return True, "cert valid %.0fd" % days


def check_mem(cfg: Config) -> tuple[bool, str]:
    """Checks whether any host's memory usage is over cfg.MEM_MAX_PCT.

    Host-level pressure only; per-container OOM kills are check_oom's job. Computed
    per-origin so a two-host estate can't pair one host's avail with another's total.
    Returns (ok, msg).
    """
    # Host memory pressure only. Per-container OOM kills are reported (with the
    # offending container named) by check_oom — single source of truth.
    #
    # Per-origin for the same reason as check_disk: the bare prom_scalar form took result[0],
    # so which host it reported was an ordering artifact of Prometheus's response once both
    # estates emitted node_memory_*. The division pairs each host's avail with its own total.
    sel = bridge.net.host_metric_sel(cfg)
    vec = bridge.net.prom_vector(
        cfg,
        "100 * (1 - node_memory_MemAvailable_bytes%s / node_memory_MemTotal_bytes%s)"
        % (sel, sel),
    )
    if not vec:
        return False, "memory metric unavailable"
    # Computed here, but REPORTED only after the breach scan below — the `if short is not None`
    # return sits under it. Same ordering as check_disk and for the same reason: a reporting host
    # that is actually out of memory outranks a complaint about the absent one. The comment used
    # to say "evaluated after", describing a line position this call has never had (2026-08-23b
    # review L9); what is deferred is the return, not the evaluation.
    short = _host_origin_shortfall(cfg, "mem", vec, "memory")
    breaching = [
        "%s %.0f%%" % (bridge.net._origin_name(labels), pct)
        for labels, pct in vec
        if pct > cfg.MEM_MAX_PCT
    ]
    if breaching:
        return False, "mem over %.0f%%: %s" % (cfg.MEM_MAX_PCT, ", ".join(breaching))
    if short is not None:
        return short
    worst = max(pct for _, pct in vec)
    return True, "mem %.0f%%" % worst
