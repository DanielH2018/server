"""Host and hardware checks for monitor-bridge.

Covers disk, cert expiry and memory. `checks/host_thermal.py` covers SMART, host temperature
and the UPS; `checks/host_edge.py` covers Pi pressure with its published ports, and the
speedtest.

`check_mem` also carries the Claude Code cgroup arm (`with_claude_cgroups`, issue #1258).

Slice 5 of the check.py split. Reads config as `cfg.X`, every query through the `src` argument
(`bridge.sources.Sources`) and the shared streak counter as `bridge.streaks.X`, so a test hands
in a fake `src` and clears the streak dict; `claude_cgroup_verdict` is from-imported from verdicts.host_cgroups and is therefore patched on
THIS module, where it is bound. `_host_origin_streaks` lives here beside
`_host_origin_shortfall`, the only code that mutates it — `checks.host_thermal` reads the floor
qualified, off this module, rather than from-importing it. Rule and enforcement:
bridge/config.py's header.
"""

from bridge.config import Config
from bridge.sources import Sources
import bridge.net
import bridge.streaks
from verdicts.host_cgroups import claude_cgroup_verdict


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


def check_disk(cfg: Config, src: Sources) -> tuple[bool, str]:
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
        vec = src.prom_vector(
            "max by (origin) (100 * (1 - node_filesystem_avail_bytes%s"
            " / node_filesystem_size_bytes%s))" % (sel, sel),
        )
        if not vec:
            return False, "metric unavailable for %s" % mp
        # DECIDED: the per-host coverage floor runs AFTER the breach scan, so a lone reporting
        # host's disk-full page is never replaced by a coverage complaint.
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


def check_cert(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Checks whether any certificate Traefik serves expires within cfg.CERT_MIN_DAYS.

    Per-series, not `min(...)`: the aggregation discarded the labels, so a DOWN said a
    certificate was expiring and could not say which (#3101). The series carry `cn`, `sans`
    and `serial` rather than the `origin` the host checks group by, so the message names the
    `cn` — and every breaching one, the way check_disk names every full mountpoint.
    """
    vec = src.prom_vector("(traefik_tls_certs_not_after - time()) / 86400")
    if not vec:
        return False, "cert metric unavailable"
    breaching = sorted(
        (days, labels.get("cn") or "unnamed cert")
        for labels, days in vec
        if days < cfg.CERT_MIN_DAYS
    )
    if breaching:
        return False, "cert expires within %.0fd: %s" % (
            cfg.CERT_MIN_DAYS,
            ", ".join("%s in %.1fd" % (cn, days) for days, cn in breaching),
        )
    soonest, cn = min(
        (days, labels.get("cn") or "unnamed cert") for labels, days in vec
    )
    return True, "cert valid %.0fd (soonest: %s)" % (soonest, cn)


def check_mem(cfg: Config, src: Sources) -> tuple[bool, str]:
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
    vec = src.prom_vector(
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
        return with_claude_cgroups(
            cfg,
            src,
            False,
            "mem over %.0f%%: %s" % (cfg.MEM_MAX_PCT, ", ".join(breaching)),
        )
    if short is not None:
        return with_claude_cgroups(cfg, src, *short)
    worst = max(pct for _, pct in vec)
    return with_claude_cgroups(cfg, src, True, "mem %.0f%%" % worst)


def with_claude_cgroups(
    cfg: Config, src: Sources, ok: bool, msg: str
) -> tuple[bool, str]:
    """Fold the Claude Code cgroup arm into the host memory verdict, a cgroup fault winning.

    Issue #1258: PR #1251 put `claude_cgroup_*` into Prometheus for `claude-rc.service` and
    `user.slice/user-1000.slice`, and nothing read them. What they expose is the opening move of
    the 2026-09-05 incident (#1243) — the claude-rc cgroup stalled in memory reclaim for about
    ten minutes, holding all 8 GiB of this box's swap plus 6.96 GB anon, before anything
    downstream failed.

    # DECIDED: folded into `memory` rather than given its own Kuma monitor, for the reason
    # recorded at with_pi_ports — a new monitor costs a new push token in SOPS and a monitor
    # created by hand in the Kuma UI. This monitor already owns "this box is running out of
    # memory", and a cgroup taking the box is exactly that.
    # DECIDED: the message leads with the cgroup when the arm fires, like with_pi_ports, because
    # "memory DOWN" otherwise sends someone to look at node_memory_* when the fault is one
    # cgroup's reclaim.
    # DECIDED: the queries filter by METRIC, not by cgroup. `CLAUDE_CGROUPS` is the set whose
    # absence is a fault, not the set that is judged — so a cgroup that appears later
    # (user-1000-slice exists only once someone has logged in since boot) is covered the moment
    # it reports, without its absence paging.
    # DECIDED: a down_streak, and it does double duty — see CLAUDE_CGROUP_CONSECUTIVE in
    # bridge/config_host.py for both jobs (burst suppression and the weekly cgroup-recreate
    # counter reset).
    # DECIDED: the fetch arrives as the `src` argument, threaded from check_mem, rather than
    # being reached through `bridge.net`. A test hands in a fake that answers each of the two
    # queries separately; patching `bridge.net.prom_vector` would answer both with one value,
    # which is how a fixture ends up proving the opposite of what it claims.
    # DECIDED: neither query is a subquery. The distribution behind
    # CLAUDE_CGROUP_STALL_MAX_PCT was derived with a `[6h:1m]` subquery, which is far more
    # expensive than what runs here; measured against the live Prometheus three times each on
    # 2026-09-06, both queries below returned in 0.125-0.139s end to end, including process
    # start and the Traefik hop this check does not pay. That is the PR #482 trap — an
    # exploration query measured in place of the production one.
    # DECIDED: Grouped by origin as well since 2026-09: daniel-server reports the same
    # cgroup names.
    """
    if not cfg.CLAUDE_CGROUPS:
        return ok, msg
    stalls = src.prom_vector(
        'max by (origin, cgroup) (rate(claude_cgroup_memory_pressure_stalled_usec_total{kind="full"}[%s]) / 10000)'
        % cfg.CLAUDE_CGROUP_STALL_WINDOW,
    )
    events = src.prom_vector(
        'sum by (origin, cgroup, event) (increase(claude_cgroup_memory_events_total{event=~"%s"}[%s]))'
        % (cfg.CLAUDE_CGROUP_EVENTS, cfg.CLAUDE_CGROUP_EVENT_WINDOW),
    )
    arm_ok, arm_msg = claude_cgroup_verdict(
        stalls,
        events,
        cfg.CLAUDE_CGROUPS,
        cfg.CLAUDE_CGROUP_STALL_MAX_PCT,
        cfg.CLAUDE_CGROUP_STALL_WINDOW,
        cfg.CLAUDE_CGROUP_EVENT_WINDOW,
    )
    if arm_ok:
        bridge.streaks._down_streaks["claude_cgroups"] = 0
        return ok, "%s, %s" % (msg, arm_msg)
    bridge.streaks._down_streaks["claude_cgroups"], arm_ok, arm_msg = (
        bridge.streaks.down_streak(
            bridge.streaks._down_streaks.get("claude_cgroups", 0),
            cfg.CLAUDE_CGROUP_CONSECUTIVE,
            arm_msg,
            "burst/cgroup-recreate grace",
        )
    )
    if arm_ok:
        return ok, "%s, %s" % (msg, arm_msg)
    return False, "%s | %s" % (arm_msg, msg)
