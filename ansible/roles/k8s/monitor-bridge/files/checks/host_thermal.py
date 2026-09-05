"""Thermal and hardware-health checks for monitor-bridge.

Covers SMART wear through Scrutiny, host temperature from node-exporter's hwmon collector, and
the three UPS battery arms.

Split out of `checks/host.py`, which keeps disk, certificate expiry and memory. Reads config as
`cfg.X`, the fetch layer as `bridge.net.X` and the shared streak counter as `bridge.streaks.X`,
so the tests' patches on those modules reach it; the verdicts it from-imports from verdicts.host
are patched on THIS module, where they are bound. The origin-coverage floor stays in
`checks.host` and is read qualified as `checks.host._host_origin_shortfall`, because
`_host_origin_streaks` is a single dict the tests clear on that module. Rule and enforcement:
bridge/config.py's header.
"""

from bridge.config import Config
import bridge.net
import bridge.streaks
import checks.host
from verdicts.host import (
    hwmon_included_series,
    hwmon_name_maps,
    hwmon_temp_limits,
    hwmon_temp_verdict,
    scrutiny_device_wear,
    scrutiny_freshness,
    scrutiny_health,
    scrutiny_wear_verdict,
    ups_health,
)


def scrutiny_wear_devices(
    cfg: Config, summary: dict | None
) -> list[tuple[str, float | None]]:
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
        details = bridge.net._get_json(
            "%s/api/device/%s/details" % (cfg.SCRUTINY_URL, wwn)
        )
        devices.append((label, scrutiny_device_wear(details)))
    return devices


def check_scrutiny(cfg: Config) -> tuple[bool, str]:
    """Checks Scrutiny's summary for freshness, drive health, and (if configured) wear.

    Fetches /api/summary once; per-device wear details are fetched only when freshness and
    health both pass and cfg.SCRUTINY_WEAR_MAX is set. Returns (ok, msg).
    """
    data = bridge.net._get_json(cfg.SCRUTINY_URL + "/api/summary")
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
        scrutiny_wear_devices(cfg, summary), cfg.SCRUTINY_WEAR_MAX
    )
    if not wear_ok:
        return False, wear_msg
    return True, "%s; %s; %s" % (fresh_msg, health_msg, wear_msg)


def check_host_temp(cfg: Config) -> tuple[bool, str]:
    """Board and CPU temperature across the three hosts, from node-exporter's hwmon collector.

    Answers the one thermal question nothing else here asks: is a host cooking? A hot box
    throttles, then corrupts, then dies, and every existing monitor reads green throughout —
    check_cpu_throttle sees CFS throttling (a cgroup limit, not heat), and the Grafana
    "Hardware Temperature Monitor" panel plots these series but nobody watches a panel.

    Drives are NOT read here; see HWMON_TEMP_EXCLUDE_CHIP. Two arms assign every remaining
    sensor a limit — its own declared max or crit where either is plausible (max preferred when
    both are), a flat ceiling where neither is — so coverage is exhaustive rather than whatever
    the metric join happens to yield. The limit selection is pure and lives in verdicts.host,
    which is what lets the red-proof tests drive it without a Prometheus.

    Empty vector pages rather than passing: no sensors means EVERY collector went blind, and a
    "nothing is too hot" verdict from zero readings is the inert-check failure this repo has
    paid for twice. A PARTIAL blindness — one host gone, the others answering — is what
    HWMON_TEMP_ORIGINS_MIN covers, and the empty-vector arm structurally cannot see it.

    Ordering mirrors check_disk and check_mem: a host that IS reporting and IS too hot pages
    ahead of a complaint about the absent one. The two graces stay separate and are never
    compounded — down_streak is the thermal-spike grace and applies only to the hot-sensor path,
    while the coverage shortfall carries its own hysteresis inside checks.host._host_origin_shortfall.
    """
    temps = bridge.net.prom_vector(cfg, "node_hwmon_temp_celsius")
    # node-exporter keeps the readable names in two side metrics rather than on the reading, so
    # naming the hot sensor `daniel-box k10temp/Tctl` instead of
    # `daniel-box/pci0000:00_0000:00:18_3/temp1` costs two more instant queries. Both are tiny
    # (11 and 16 series live on 2026-09-01) and neither can fail the check: an empty answer just
    # falls back to the sysfs path.
    names = hwmon_name_maps(
        bridge.net.prom_vector(cfg, "node_hwmon_chip_names"),
        bridge.net.prom_vector(cfg, "node_hwmon_sensor_label"),
    )
    limits = hwmon_temp_limits(
        temps,
        bridge.net.prom_vector(cfg, "node_hwmon_temp_max_celsius"),
        cfg.HWMON_TEMP_RATIO,
        cfg.HWMON_TEMP_FALLBACK_C,
        cfg.HWMON_TEMP_MIN_PLAUSIBLE_C,
        cfg.HWMON_TEMP_MAX_PLAUSIBLE_C,
        cfg.HWMON_TEMP_EXCLUDE_CHIP,
        names,
        # A third instant query, the same shape as the two name lookups above: a driver that
        # skips temp*_max but still publishes temp*_crit (issue #995 — see hwmon_temp_limits'
        # docstring for why max wins when both exist) would otherwise fall to the flat fallback
        # even though it declared a real limit.
        crits=bridge.net.prom_vector(cfg, "node_hwmon_temp_crit_celsius"),
        # Config, not a query — the published rating for a sensor whose driver declares neither
        # source. Both queries above still win over it wherever they answer.
        rated=cfg.HWMON_TEMP_RATED_MAX_C,
    )
    # Counted over the series that survive exclusion, via the same predicate hwmon_temp_limits
    # uses — a host whose only sensors are excluded is not a host this check covers.
    short = checks.host._host_origin_shortfall(
        cfg,
        "host_temp",
        hwmon_included_series(temps, cfg.HWMON_TEMP_EXCLUDE_CHIP),
        "host temperature",
        min_origins=cfg.HWMON_TEMP_ORIGINS_MIN,
        consecutive=cfg.HWMON_TEMP_ORIGINS_CONSECUTIVE,
    )
    ok, msg = hwmon_temp_verdict(limits)
    if not ok:
        bridge.streaks._down_streaks["host_temp"], ok, msg = bridge.streaks.down_streak(
            bridge.streaks._down_streaks.get("host_temp", 0),
            cfg.HWMON_TEMP_CONSECUTIVE,
            msg,
            "thermal spike grace",
        )
        return ok, msg
    bridge.streaks._down_streaks["host_temp"] = 0
    if short is not None:
        return short
    return True, msg


def check_ups(cfg: Config) -> tuple[bool, str]:
    """UPS battery health from HA's Prometheus-scraped sensors (the UPS_* env block in bridge/config_host.py).

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
    values = {name: bridge.net.prom_scalar(cfg, q) for name, q in configured}
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
            bridge.net.prom_scalar(cfg, cfg.UPS_HA_UP_QUERY)
            if cfg.UPS_HA_UP_QUERY
            else None
        )
        if not (ha_up is not None and ha_up > 0.5 and "replace-battery" in values):
            bridge.streaks._down_streaks["ups"] = 0
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
        bridge.streaks._down_streaks["ups"] = 0
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
        bridge.streaks._down_streaks["ups"] = 0
        return True, msg
    bridge.streaks._down_streaks["ups"], ok, msg = bridge.streaks.down_streak(
        bridge.streaks._down_streaks.get("ups", 0), cfg.UPS_CONSECUTIVE, msg, "grace"
    )
    return ok, msg
