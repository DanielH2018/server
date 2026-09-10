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
from verdicts.host_power import (
    thermal_monitor_verdict,
    thermal_throttle_verdict,
    undervoltage_verdict,
)


def _source_is_up(cfg: Config, up_query: str) -> bool:
    """Whether a scrape a thermal arm depends on is AFFIRMATIVELY up.

    Read through prom_vector rather than prom_scalar, so an arm needs exactly one fetch function
    and therefore one patch point in its tests — `up{job=...}` is a vector anyway, so nothing is
    lost. An empty answer, an unconfigured query and a 0 all mean "not affirmatively up", which
    is the safe direction: never page over a source outage another monitor already owns.
    """
    if not up_query:
        return False
    return any(value > 0.5 for _labels, value in bridge.net.prom_vector(cfg, up_query))


def _undervoltage_arm(
    cfg: Config, names, alarms: list[tuple[dict, float]], source_up: bool
) -> tuple[bool, str] | None:
    """The Pi's firmware undervoltage alarm. (ok, msg), or None when there is nothing to say.

    Takes its fetched vector and its source-gate answer rather than fetching them, for the reason
    `_thermal_throttle_arm` records.

    Folded into check_host_temp's monitor rather than given its own, for the reason
    check_scrutiny's wear arm records: a new Kuma monitor needs a new push token in SOPS, and
    this answers the same question the thermal arm does — is the hardware being damaged right
    now — from the power side instead of the heat side.

    None means "nothing to say", which covers three cases:
      - no query configured, so the arm is switched off;
      - the vector is empty AND daniel-pi's own scrape is not affirmatively up. The sensor lives
        on one host, so an empty vector is that host going quiet, and check_cluster_targets owns
        a dead scrape. Deferring stops this arm double-paging a fault another monitor already
        reports. An unqueryable gate defers too — never page over a source outage on a guess;
      - no alarm asserted. A clean arm stays SILENT rather than appending a note to the up
        message, so the monitor's tile on an ordinary cycle reads exactly as it did before this
        arm existed. A grace that is COUNTING does return its note — a monitor that is up while
        a fault accumulates has to say so, or it is indistinguishable from a clean cycle.
    An empty vector while the Pi IS scraping fine is a real fault: the sensor was renamed or the
    hwmon collector went blind, and undervoltage_verdict pages for it.

    Its streak key is its own. The check's arms must not compound: a throttle blip and a
    thermal blip sharing one counter would page together at half the intended threshold.
    """
    if not cfg.UNDERVOLTAGE_QUERY:
        return None
    if not alarms and not source_up:
        bridge.streaks._down_streaks["host_undervoltage"] = 0
        return None
    ok, msg = undervoltage_verdict(alarms, names)
    if ok:
        bridge.streaks._down_streaks["host_undervoltage"] = 0
        return None
    (
        bridge.streaks._down_streaks["host_undervoltage"],
        ok,
        msg,
    ) = bridge.streaks.down_streak(
        bridge.streaks._down_streaks.get("host_undervoltage", 0),
        cfg.UNDERVOLTAGE_CONSECUTIVE,
        msg,
        "undervoltage grace",
    )
    return ok, msg


def _thermal_throttle_arm(
    cfg: Config, states: list[tuple[dict, float]], source_up: bool
) -> tuple[bool, str] | None:
    """Kernel CPU thermal throttling. (ok, msg), or None when there is nothing to say.

    Takes its fetched vector and its source-gate answer rather than fetching them: the arm then
    holds streak state and decisions only, so its tests hand it inputs directly instead of
    patching `bridge.net`. `check_host_temp` does the two fetches.

    Distinct from check_cpu_throttle, which reads CFS throttling — a cgroup quota being hit, not
    heat. Distinct from the temperature arm too: the firmware can enforce a limit lower than the
    one the driver declares, so a CPU can sit under its declared max and still be throttled.

    None means no query configured, nothing throttling, or a fully empty vector while the amd64
    node-exporters are not affirmatively up — the same three-case shape _undervoltage_arm has,
    and for the same reasons. The empty-vector gate matters here too: no Processor cooling
    device anywhere means both nodes stopped publishing, which check_cluster_targets and this
    check's own EXPORTER_DEPENDENT entry already own. An empty vector while `node` IS scraping
    is a real fault — a driver or kernel change took the sensors away — and pages. The PARTIAL
    loss, one node gone, is what THERMAL_THROTTLE_ORIGINS_MIN catches, and that floor is what
    stops this arm being inert.

    Its own streak key and its own threshold, for the reason _undervoltage_arm's docstring gives.
    A momentary throttle during a burst is ordinary, so this grace is longer than the
    undervoltage one and shorter than the thermal-spike grace.
    """
    if not cfg.THERMAL_THROTTLE_QUERY:
        return None
    if not states and not source_up:
        bridge.streaks._down_streaks["host_thermal_throttle"] = 0
        return None
    ok, msg = thermal_throttle_verdict(states, cfg.THERMAL_THROTTLE_ORIGINS_MIN)
    if ok:
        bridge.streaks._down_streaks["host_thermal_throttle"] = 0
        return None
    (
        bridge.streaks._down_streaks["host_thermal_throttle"],
        ok,
        msg,
    ) = bridge.streaks.down_streak(
        bridge.streaks._down_streaks.get("host_thermal_throttle", 0),
        cfg.THERMAL_THROTTLE_CONSECUTIVE,
        msg,
        "throttle grace",
    )
    return ok, msg


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
    """Board and CPU temperature across the three hosts, plus undervoltage and CPU throttling.

    Three arms on one monitor, each with its OWN streak key so a blip in two of them cannot
    compound into a page at half the intended threshold:

      1. hwmon temperature, described in full below — the original arm.
      2. the Pi's firmware undervoltage alarm (`_undervoltage_arm`), evaluated FIRST and
         returning ahead of everything else when asserted.
      3. kernel CPU thermal throttling (`_thermal_throttle_arm`), evaluated after a clean
         temperature verdict.

    Arms 2 and 3 landed for issue #1471. Both signals were already plotted on
    Infrastructure/hardware-thermal.json and alerted on by nothing.

    This function FETCHES and holds the streak state; `verdicts.host_power.thermal_monitor_verdict`
    decides which arm reaches the monitor. That split is issue #1547: the arms had accept/reject
    pairs of their own, but a deleted `return` at a call site left every test green, because the
    structural test reads `co_names` and the clean-path integration tests drive only the cycle
    where all three arms defer. Read the composer for the ordering; read here for what each path
    costs in Prometheus queries — the throttle arm's two are spent only on a cycle whose
    temperature verdict came back clean.

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
    # The undervoltage alarm is evaluated FIRST and returns ahead of everything else when it is
    # asserted. It is the one signal here with no threshold to argue about — the firmware has
    # already decided — and the damage it does (a corrupted SD card) is not recoverable by
    # cooling down. A deferred or healthy arm falls through and changes nothing about the
    # temperature path below.
    alarms = (
        bridge.net.prom_vector(cfg, cfg.UNDERVOLTAGE_QUERY)
        if cfg.UNDERVOLTAGE_QUERY
        else []
    )
    # `bool(alarms) or ...` short-circuits, so the gate query is spent only on the cycle where
    # the reading came back empty — which is the only cycle the arm reads it on.
    under = _undervoltage_arm(
        cfg,
        names,
        alarms,
        bool(alarms) or _source_is_up(cfg, cfg.UNDERVOLTAGE_UP_QUERY),
    )
    ok, msg = hwmon_temp_verdict(limits)
    temperature: tuple[bool, str] | None = None
    throttle: tuple[bool, str] | None = None
    # An asserted undervoltage alarm ends the cycle, so nothing below it runs — not the
    # temperature streak, not the throttle fetch. The ORDER and the propagation live in
    # thermal_monitor_verdict, which is pure and directly tested; what stays here is the
    # fetching and the streak state, which is what the arms cannot be given.
    if under is None or under[0]:
        if not ok:
            bridge.streaks._down_streaks["host_temp"], ok, msg = (
                bridge.streaks.down_streak(
                    bridge.streaks._down_streaks.get("host_temp", 0),
                    cfg.HWMON_TEMP_CONSECUTIVE,
                    msg,
                    "thermal spike grace",
                )
            )
            temperature = (ok, msg)
        else:
            bridge.streaks._down_streaks["host_temp"] = 0
            states = (
                bridge.net.prom_vector(cfg, cfg.THERMAL_THROTTLE_QUERY)
                if cfg.THERMAL_THROTTLE_QUERY
                else []
            )
            throttle = _thermal_throttle_arm(
                cfg,
                states,
                bool(states) or _source_is_up(cfg, cfg.THERMAL_THROTTLE_UP_QUERY),
            )
    return thermal_monitor_verdict(under, temperature, throttle, short, msg)


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
