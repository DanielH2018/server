"""Host and hardware checks for monitor-bridge — disk, memory, cert expiry, host temperature,
SMART, the UPS, Pi pressure and published ports, the speedtest.

Slice 5 of the check.py split. Reads config as `cfg.X`, the fetch layer as `bridge_io.X` and
the shared streak counter as `bridge_streaks.X`, so the tests' patches on those modules reach
it; the verdicts it from-imports from verdicts_host are patched on THIS module, where they are
bound. `_host_origin_streaks` lives here beside `_host_origin_shortfall`, the only code that
mutates it. Rule and enforcement: bridge_config.py's header.
"""

import socket
import urllib.parse
from datetime import datetime, timezone

import bridge_config as cfg
import bridge_io
import bridge_streaks
from verdicts_host import (
    hwmon_included_series,
    hwmon_name_maps,
    hwmon_temp_limits,
    hwmon_temp_verdict,
    pi_ports_verdict,
    pi_pressure,
    scrutiny_device_wear,
    scrutiny_freshness,
    scrutiny_health,
    scrutiny_wear_verdict,
    ups_health,
)


_host_origin_streaks: dict[str, int] = {}


def _host_origin_shortfall(key, vec, what, min_origins=None, consecutive=None):
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
    origins = {bridge_io._origin_name(labels) for labels, _ in vec}
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


def check_disk():
    # Percentage computed per series, then grouped by origin, so avail and size always come from
    # the SAME host and device. The previous form took max(avail) and max(size) as two separate
    # queries: fine while one estate reported, but once daniel-server and daniel-box both landed
    # in this Prometheus it paired one host's avail with the other's size, and a filling disk on
    # the smaller host produced an arbitrarily wrong percentage rather than a high one.
    breaching = []
    shortfalls = []
    for mp in cfg.DISK_MOUNTPOINTS:
        sel = bridge_io.host_metric_sel('mountpoint="%s"' % mp)
        vec = bridge_io.prom_vector(
            "max by (origin) (100 * (1 - node_filesystem_avail_bytes%s"
            " / node_filesystem_size_bytes%s))" % (sel, sel)
        )
        if not vec:
            return False, "metric unavailable for %s" % mp
        # Collected, not returned, so a host that IS reporting and IS full still pages ahead of
        # the coverage complaint — a real breach on the survivor outranks the absent host.
        short = _host_origin_shortfall("disk:%s" % mp, vec, "disk %s" % mp)
        if short is not None:
            shortfalls.append(short)
        for labels, used_pct in vec:
            if used_pct > cfg.DISK_MAX_PCT:
                breaching.append(
                    "%s %s %.0f%%" % (bridge_io._origin_name(labels), mp, used_pct)
                )
    if breaching:
        return False, "disk over %.0f%%: %s" % (cfg.DISK_MAX_PCT, ", ".join(breaching))
    failed = [s for s in shortfalls if not s[0]]
    if failed:
        return False, "; ".join(msg for _, msg in failed)
    if shortfalls:
        return True, "; ".join(msg for _, msg in shortfalls)
    return True, "all mounts under %.0f%%" % cfg.DISK_MAX_PCT


def check_cert():
    days = bridge_io.prom_scalar("(min(traefik_tls_certs_not_after) - time()) / 86400")
    if days is None:
        return False, "cert metric unavailable"
    if days < cfg.CERT_MIN_DAYS:
        return False, "cert expires in %.1fd (< %.0fd)" % (days, cfg.CERT_MIN_DAYS)
    return True, "cert valid %.0fd" % days


def check_mem():
    # Host memory pressure only. Per-container OOM kills are reported (with the
    # offending container named) by check_oom — single source of truth.
    #
    # Per-origin for the same reason as check_disk: the bare prom_scalar form took result[0],
    # so which host it reported was an ordering artifact of Prometheus's response once both
    # estates emitted node_memory_*. The division pairs each host's avail with its own total.
    sel = bridge_io.host_metric_sel()
    vec = bridge_io.prom_vector(
        "100 * (1 - node_memory_MemAvailable_bytes%s / node_memory_MemTotal_bytes%s)"
        % (sel, sel)
    )
    if not vec:
        return False, "memory metric unavailable"
    # Computed here, but REPORTED only after the breach scan below — the `if short is not None`
    # return sits under it. Same ordering as check_disk and for the same reason: a reporting host
    # that is actually out of memory outranks a complaint about the absent one. The comment used
    # to say "evaluated after", describing a line position this call has never had (2026-08-23b
    # review L9); what is deferred is the return, not the evaluation.
    short = _host_origin_shortfall("mem", vec, "memory")
    breaching = [
        "%s %.0f%%" % (bridge_io._origin_name(labels), pct)
        for labels, pct in vec
        if pct > cfg.MEM_MAX_PCT
    ]
    if breaching:
        return False, "mem over %.0f%%: %s" % (cfg.MEM_MAX_PCT, ", ".join(breaching))
    if short is not None:
        return short
    worst = max(pct for _, pct in vec)
    return True, "mem %.0f%%" % worst


def scrutiny_wear_devices(summary):
    """One /api/device/<wwn>/details fetch per non-archived device.

    The wear attributes are not in /api/summary, which is what makes this N calls per cycle rather
    than none — same shape as check_k8s_workloads' six Prometheus queries. Each payload is ~19 KB
    and only smart_results[0] is read. A failing fetch raises out of _get_json and the runner
    reports DOWN; that is deliberate and must not be caught here.
    """
    devices = []
    for wwn, entry in (summary or {}).items():
        dev = entry.get("device") or {}
        if dev.get("archived"):
            continue
        name = dev.get("device_name") or wwn
        model = dev.get("model_name")
        label = "%s (%s)" % (name, model) if model else name
        details = bridge_io._get_json(
            "%s/api/device/%s/details" % (cfg.SCRUTINY_URL, wwn)
        )
        devices.append((label, scrutiny_device_wear(details)))
    return devices


def check_scrutiny():
    data = bridge_io._get_json(cfg.SCRUTINY_URL + "/api/summary")
    summary = (data.get("data") or {}).get("summary")
    fresh_ok, fresh_msg = scrutiny_freshness(summary, cfg.SCRUTINY_MAX_AGE_H)
    if not fresh_ok:
        return False, fresh_msg
    health_ok, health_msg = scrutiny_health(summary, cfg.SCRUTINY_TEMP_MAX)
    if not health_ok:
        return False, health_msg
    # Folded into this monitor rather than given its own: a new Kuma monitor needs a new push
    # token in SOPS, and wear answers the same question device_status does — is the drive still
    # fit to hold the data on it — just months earlier. Fetched only once freshness passes, so a
    # dead collector costs no per-device calls.
    if not cfg.SCRUTINY_WEAR_MAX:
        return True, "%s; %s" % (fresh_msg, health_msg)
    wear_ok, wear_msg = scrutiny_wear_verdict(
        scrutiny_wear_devices(summary), cfg.SCRUTINY_WEAR_MAX
    )
    if not wear_ok:
        return False, wear_msg
    return True, "%s; %s; %s" % (fresh_msg, health_msg, wear_msg)


def check_host_temp():
    """Board and CPU temperature across the three hosts, from node-exporter's hwmon collector.

    Answers the one thermal question nothing else here asks: is a host cooking? A hot box
    throttles, then corrupts, then dies, and every existing monitor reads green throughout —
    check_cpu_throttle sees CFS throttling (a cgroup limit, not heat), and the Grafana
    "Hardware Temperature Monitor" panel plots these series but nobody watches a panel.

    Drives are NOT read here; see HWMON_TEMP_EXCLUDE_CHIP. Two arms assign every remaining
    sensor a limit — its own declared max where that max is plausible, a flat ceiling where it
    is not — so coverage is exhaustive rather than whatever the metric join happens to yield.
    The limit selection is pure and lives in verdicts_host, which is what lets the red-proof
    tests drive it without a Prometheus.

    Empty vector pages rather than passing: no sensors means EVERY collector went blind, and a
    "nothing is too hot" verdict from zero readings is the inert-check failure this repo has
    paid for twice. A PARTIAL blindness — one host gone, the others answering — is what
    HWMON_TEMP_ORIGINS_MIN covers, and the empty-vector arm structurally cannot see it.

    Ordering mirrors check_disk and check_mem: a host that IS reporting and IS too hot pages
    ahead of a complaint about the absent one. The two graces stay separate and are never
    compounded — down_streak is the thermal-spike grace and applies only to the hot-sensor path,
    while the coverage shortfall carries its own hysteresis inside _host_origin_shortfall.
    """
    temps = bridge_io.prom_vector("node_hwmon_temp_celsius")
    # node-exporter keeps the readable names in two side metrics rather than on the reading, so
    # naming the hot sensor `daniel-box k10temp/Tctl` instead of
    # `daniel-box/pci0000:00_0000:00:18_3/temp1` costs two more instant queries. Both are tiny
    # (11 and 16 series live on 2026-09-01) and neither can fail the check: an empty answer just
    # falls back to the sysfs path.
    names = hwmon_name_maps(
        bridge_io.prom_vector("node_hwmon_chip_names"),
        bridge_io.prom_vector("node_hwmon_sensor_label"),
    )
    limits = hwmon_temp_limits(
        temps,
        bridge_io.prom_vector("node_hwmon_temp_max_celsius"),
        cfg.HWMON_TEMP_RATIO,
        cfg.HWMON_TEMP_FALLBACK_C,
        cfg.HWMON_TEMP_MIN_PLAUSIBLE_C,
        cfg.HWMON_TEMP_MAX_PLAUSIBLE_C,
        cfg.HWMON_TEMP_EXCLUDE_CHIP,
        names,
    )
    # Counted over the series that survive exclusion, via the same predicate hwmon_temp_limits
    # uses — a host whose only sensors are excluded is not a host this check covers.
    short = _host_origin_shortfall(
        "host_temp",
        hwmon_included_series(temps, cfg.HWMON_TEMP_EXCLUDE_CHIP),
        "host temperature",
        min_origins=cfg.HWMON_TEMP_ORIGINS_MIN,
        consecutive=cfg.HWMON_TEMP_ORIGINS_CONSECUTIVE,
    )
    ok, msg = hwmon_temp_verdict(limits)
    if not ok:
        bridge_streaks._down_streaks["host_temp"], ok, msg = bridge_streaks.down_streak(
            bridge_streaks._down_streaks.get("host_temp", 0),
            cfg.HWMON_TEMP_CONSECUTIVE,
            msg,
            "thermal spike grace",
        )
        return ok, msg
    bridge_streaks._down_streaks["host_temp"] = 0
    if short is not None:
        return short
    return True, msg


def check_ups():
    """UPS battery health from HA's Prometheus-scraped sensors (see the UPS_* env block above).

    Three arms: charge %, estimated runtime, and the replace-battery self-test verdict. All queries
    empty -> disabled (stays up), like check_pi_pressure without a glances URL. Two defer paths keep
    this from double-paging a source outage another monitor already owns:
      - ALL arms absent while HA's scrape is DOWN (or the up-gate is unqueryable) -> HA's whole
        Prometheus scrape is down (Scrape Targets' page). If instead HA is scraping fine (up-gate == 1)
        and the replace arm is configured, all-absent means every UPS entity was renamed/removed at
        once — Scrape Targets can't see it, so page through the streak rather than silently unmonitor.
      - both NUT NUMERIC arms (charge, runtime) absent while the replace-battery arm is still present
        -> the NUT server/integration dropped: HA drops the unavailable numeric sensors, but the
        replace-battery template FLOORS to 0 (stays present) in that same outage (templates.yaml), so
        a NUT outage can't reach the all-absent branch above. The nut pod liveness probe owns
        NUT-server death, so defer rather than double-paging it with a misdirecting "entity renamed?".
    A PARTIAL absence that is NEITHER of those (a single numeric arm gone, or the replace arm gone
    while the numerics report) is a specific entity rename/removal — it pages (through the streak)
    rather than silently monitoring the survivor. UPS_CONSECUTIVE hysteresis (like check_ha_heartbeat)
    rides out a single-cycle runtime dip from a load spike or an HA-restart blip; only a sustained
    problem pages.
    """
    configured = [
        (name, q)
        for name, q in (
            ("charge", cfg.UPS_CHARGE_QUERY),
            ("runtime", cfg.UPS_RUNTIME_QUERY),
            ("replace-battery", cfg.UPS_REPLACE_QUERY),
        )
        if q
    ]
    if not configured:
        return True, "UPS monitoring disabled (no query)"
    values = {name: bridge_io.prom_scalar(q) for name, q in configured}
    if all(v is None for v in values.values()):
        # All arms gone. Usually HA's whole Prometheus scrape is down (the numeric AND the template
        # sensors vanish together) — Scrape Targets owns that, so defer. But if HA is scraping fine and
        # every UPS entity was renamed/removed at once, Scrape Targets can't see it and the UPS would go
        # silently unmonitored — so gate on HA's own up series and fall through to the partial-absence
        # page below when HA is affirmatively up AND the replace arm is configured (its 0-floor in a NUT
        # outage means a real NUT-server outage is never all-absent, so this can't misfire on one).
        # An unqueryable/absent gate keeps the safe defer (never page over a source outage another
        # monitor owns).
        ha_up = (
            bridge_io.prom_scalar(cfg.UPS_HA_UP_QUERY) if cfg.UPS_HA_UP_QUERY else None
        )
        if not (ha_up is not None and ha_up > 0.5 and "replace-battery" in values):
            bridge_streaks._down_streaks["ups"] = 0
            return (
                True,
                "no UPS data in Prometheus (HA scrape down? Scrape Targets owns source liveness)",
            )
    missing = [name for name, v in values.items() if v is None]
    if (
        "charge" in values
        and "runtime" in values
        and values["charge"] is None
        and values["runtime"] is None
        and values.get("replace-battery") is not None
    ):
        # NUT server/integration down, NOT an entity rename: charge+runtime are direct NUT numeric
        # sensors HA drops from Prometheus when the source goes unavailable, while the replace-battery
        # arm is an HA template binary_sensor that FLOORS to 0 (stays present) in that same outage
        # (templates.yaml) — so a NUT outage reads as both numeric arms absent + replace present, past
        # the all-absent branch above. The nut pod liveness probe owns NUT-server death, so defer
        # rather than double-paging it through the partial-absence path below with a misdirecting
        # "entity renamed?" msg. A single numeric arm gone (charge XOR runtime) is still a real rename.
        bridge_streaks._down_streaks["ups"] = 0
        return (
            True,
            "NUT numeric arms (charge, runtime) absent — NUT server/integration down; "
            "nut healthcheck owns it",
        )
    if missing:
        # Some configured arms present, others absent — NOT the whole-scrape-down case above but a
        # specific entity rename/removal. Don't silently monitor the survivor: passing on the present
        # arm(s) would blind the missing one (e.g. keep charge green while the primary aged-battery
        # runtime signal is gone). Flag it through the same down-streak so an HA-restart blip still
        # gets the UPS_CONSECUTIVE grace, but a sustained partial drop pages.
        ok, msg = (
            False,
            "UPS sensor(s) absent: %s (entity renamed/removed?)" % ", ".join(missing),
        )
    else:
        ok, msg = ups_health(
            values.get("charge"),
            values.get("runtime"),
            values.get("replace-battery"),
            cfg.UPS_CHARGE_MIN_PCT,
            cfg.UPS_RUNTIME_MIN_S,
        )
    if ok:
        bridge_streaks._down_streaks["ups"] = 0
        return True, msg
    bridge_streaks._down_streaks["ups"], ok, msg = bridge_streaks.down_streak(
        bridge_streaks._down_streaks.get("ups", 0), cfg.UPS_CONSECUTIVE, msg, "grace"
    )
    return ok, msg


def check_pi_pressure():
    """Swap-thrash / overload early warning for the memory-constrained Pi.

    Empty PI_GLANCES_URL -> disabled (stays up), like check_n8n without an API key.
    An unreachable glances raises -> the loop renders it down with the error.
    """
    if not cfg.PI_GLANCES_URL:
        return True, "pi monitoring disabled (no glances URL)"
    load = bridge_io._get_json(cfg.PI_GLANCES_URL + "/api/4/load")
    mem = bridge_io._get_json(cfg.PI_GLANCES_URL + "/api/4/mem")
    fs = bridge_io._get_json(cfg.PI_GLANCES_URL + "/api/4/fs")
    ok, msg = pi_pressure(
        load, mem, fs, cfg.PI_LOAD_MAX, cfg.PI_MEM_MIN_MB, cfg.PI_DISK_MAX_PCT
    )
    return with_pi_ports(ok, msg)


def _tcp_open(host, port, timeout):
    """True when something accepts a TCP connection on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def with_pi_ports(ok, msg):
    """Fold the published-port arm into the Pi verdict, a dead port winning the message.

    Folded into this monitor rather than given its own for the reason recorded at with_ha_ban:
    a new Kuma monitor costs a new push token in SOPS. This monitor already owns "the Pi is
    unhealthy", and a service that stopped listening is that.

    # DECIDED: TCP connect is the primary signal, glances only the attribution. Measured
    # 2026-08-27 against the live Pi: /api/4/load, /mem and /fs answer in 0.03-0.06s each,
    # while /api/4/containers took 4.43s and then TIMED OUT at the 10s HTTP_TIMEOUT on the
    # very next call. Polling it every cycle would have left the arm failing open most of the
    # time — inert behind a green monitor, which is the failure mode this arm exists to
    # catch in the first place. It is also a heavy query to run every cycle against a 456 MB
    # Zero 2 W whose pressure this same check reports.
    # DECIDED: the message leads with the container names when the arm fires, because
    # "pi_pressure DOWN" otherwise pages someone to look at load and memory when the fault is
    # neither. Same shape as with_ha_ban putting the ban first.
    # DECIDED: a down_streak, unlike with_ha_ban's arm. A Pi deploy recreates containers, so
    # their ports are legitimately closed for a few seconds and a single cycle can read dead.
    # A detached container persists until someone recreates it, so it survives the grace.
    # DECIDED: an attribution fetch that fails downgrades the DIAGNOSIS, never the verdict —
    # pi_ports_verdict renders "cause unknown" and the port is still reported dead. Failing
    # open there would reintroduce exactly the inertness the first DECIDED avoids.
    """
    if not cfg.PI_PUBLISHED_PORTS:
        return ok, msg
    host = urllib.parse.urlsplit(cfg.PI_GLANCES_URL).hostname
    if not host:
        return ok, msg
    dead = [
        (name, port)
        for name, port in cfg.PI_PUBLISHED_PORTS
        if not _tcp_open(host, port, cfg.PI_PORT_TIMEOUT)
    ]
    containers = None
    if dead:
        try:
            containers = bridge_io._get_json(cfg.PI_GLANCES_URL + "/api/4/containers")
        except Exception:
            containers = None
    arm_ok, arm_msg = pi_ports_verdict(dead, len(cfg.PI_PUBLISHED_PORTS), containers)
    if arm_ok:
        bridge_streaks._down_streaks["pi_ports"] = 0
        return ok, "%s, %s" % (msg, arm_msg)
    bridge_streaks._down_streaks["pi_ports"], arm_ok, arm_msg = (
        bridge_streaks.down_streak(
            bridge_streaks._down_streaks.get("pi_ports", 0),
            cfg.PI_PORTS_CONSECUTIVE,
            arm_msg,
            "deploy grace",
        )
    )
    if arm_ok:
        return ok, "%s, %s" % (msg, arm_msg)
    return False, "%s | %s" % (arm_msg, msg)


def speedtest_verdict(row, min_mbps, max_age_h, now=None):
    """Pure: judge the newest speedtest-tracker result row. (ok, msg).

    `row` is one element of /api/v1/results' `data`, or None when the app returned no rows at
    all.

    THE TIMESTAMP IS UTC DESPITE CARRYING NO OFFSET. /api/v1/results serializes `created_at` as
    a bare "2026-08-24 11:00:00", while /api/speedtest/latest serializes the SAME row as
    "2026-08-24T06:00:00.000000-05:00" — verified against row id 780 on 2026-08-24. The bare
    form is therefore UTC, not the DISPLAY_TIMEZONE local time it resembles, and
    datetime.fromisoformat returns it naive. Attaching UTC explicitly is what keeps the age
    arm from reading five hours off; a naive value compared against an aware `now` raises
    instead, which is the safer of the two failures but still not a verdict.

    Arms run status, then age, then floor, in that order and for that reason: `download_bits`
    is null on a failed row, so a floor comparison ahead of the status arm compares None.
    """
    now = now or datetime.now(timezone.utc)
    if not row:
        return (
            False,
            "speedtest has no results at all — the scheduler has never completed a run",
        )

    status = row.get("status")
    created = row.get("created_at")

    if status != "completed":
        detail = ((row.get("data") or {}).get("message") or "").strip()
        return False, "last run (%s) %s%s" % (
            created or "unknown time",
            status or "has no status",
            " — " + detail if detail else "",
        )

    if not created:
        return False, "last run has no created_at — cannot judge freshness"
    stamp = datetime.fromisoformat(created.strip().replace(" ", "T"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age_h = (now - stamp).total_seconds() / 3600
    if age_h > max_age_h:
        return (
            False,
            "last run was %.1fh ago (> %gh) — the 6-hourly schedule has stopped"
            % (
                age_h,
                max_age_h,
            ),
        )

    bits = row.get("download_bits")
    if bits is None:
        return False, "last run completed but recorded no download figure"
    mbps = float(bits) / 1e6
    server = ((row.get("data") or {}).get("server") or {}).get(
        "name"
    ) or "unknown server"
    if mbps < min_mbps:
        return False, "download %.1f Mbps (< %g) via %s — %.1fh ago" % (
            mbps,
            min_mbps,
            server,
            age_h,
        )
    return True, "download %.1f Mbps via %s, %.1fh ago" % (mbps, server, age_h)


def check_speedtest():
    """Judge speedtest-tracker's newest result row (see the SPEEDTEST_* env block above).

    Empty URL/token -> disabled (stays up), like check_ha_heartbeat.

    NO HYSTERESIS ON THE VERDICT, deliberately. The app runs every 6h and this loop every 5
    min, so a consecutive-cycle streak would re-read the IDENTICAL row up to 72 times: it would
    delay the page by N*INTERVAL and prove nothing new about the run. The FETCH failure does
    ride the streak, because the app restarting under a deploy is a genuine transient — the
    same split check_ha_heartbeat draws, for the same reason. `speedtest` is also in
    STARTUP_GRACE, which covers the post-reboot cycle where the app has not finished booting.
    """
    if not cfg.SPEEDTEST_URL or not cfg.SPEEDTEST_TOKEN:
        return True, "speedtest monitoring disabled (no URL/token)"
    try:
        # sort=-created_at, because the default order is ASCENDING and would hand back the
        # OLDEST row in the 30-day window — a stale-forever reading that looks like a verdict.
        payload = bridge_io._get_json(
            cfg.SPEEDTEST_URL + "/api/v1/results?sort=-created_at&page%5Bsize%5D=1",
            headers={
                "Authorization": "Bearer " + cfg.SPEEDTEST_TOKEN,
                "Accept": "application/json",
            },
        )
    except Exception as e:
        bridge_streaks._down_streaks["speedtest"], ok, msg = bridge_streaks.down_streak(
            bridge_streaks._down_streaks.get("speedtest", 0),
            cfg.SPEEDTEST_CONSECUTIVE,
            "speedtest API unreachable: %s" % e,
            "deploy/restart grace",
        )
        return ok, msg
    bridge_streaks._down_streaks["speedtest"] = 0
    rows = payload.get("data") or []
    return speedtest_verdict(
        rows[0] if rows else None,
        cfg.SPEEDTEST_DOWNLOAD_MIN_MBPS,
        cfg.SPEEDTEST_MAX_AGE_H,
    )
