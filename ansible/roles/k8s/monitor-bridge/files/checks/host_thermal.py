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
from bridge.types import as_object, optional_object
from verdicts.host import (
    hwmon_included_series,
    hwmon_name_maps,
    hwmon_temp_limits,
    hwmon_temp_verdict,
)
from verdicts.host_power import (
    thermal_monitor_verdict,
    thermal_throttle_verdict,
    undervoltage_verdict,
    ups_health,
    ups_on_battery_verdict,
)
from verdicts.host_smart import (
    scrutiny_device_wear,
    scrutiny_freshness,
    scrutiny_health,
    scrutiny_wear_verdict,
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
        dev = as_object(
            as_object(entry, "scrutiny summary entry").get("device") or {},
            "scrutiny device",
        )
        if dev.get("archived"):
            continue
        name = str(dev.get("device_name") or wwn)
        model = dev.get("model_name")
        label = "%s (%s)" % (name, model) if model else name
        details = optional_object(
            bridge.net._get_json("%s/api/device/%s/details" % (cfg.SCRUTINY_URL, wwn)),
            "scrutiny device details",
        )
        devices.append((label, scrutiny_device_wear(details)))
    return devices


def check_scrutiny(cfg: Config) -> tuple[bool, str]:
    """Checks Scrutiny's summary for freshness, drive health, and (if configured) wear.

    Fetches /api/summary once; per-device wear details are fetched only when freshness and
    health both pass and cfg.SCRUTINY_WEAR_MAX is set. Returns (ok, msg).
    """
    data = as_object(
        bridge.net._get_json(cfg.SCRUTINY_URL + "/api/summary"), "scrutiny summary"
    )
    summary = optional_object(
        as_object(data.get("data") or {}, "scrutiny data").get("summary"),
        "scrutiny summary",
    )
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
    """UPS battery health from nut-exporter (the UPS_* env block in bridge/config_host.py).

    Four arms, one source: mains loss (NUT's `ups.status{flag="OB"}`), charge %, estimated runtime,
    and the replace-battery self-test verdict. All queries empty -> disabled (stays up), like
    check_pi_pressure without a PI_ORIGIN.

    Home Assistant's re-export of the same UPS was each arm's `max(A) or max(B)` fallback between
    #1548 and #3105; the `# DECIDED:` on the UPS block in bridge/config_host.py records why one
    source replaced two.

    The mains-loss arm is judged first and returns alone when it is red, because charge and runtime
    read the RUNWAY and can hold green through most of an outage. It carries its own streak key.
    It sits in the same `configured`/`missing` census as the other three (issue #1630), so a rename
    or removal of `network_ups_tools_ups_status{flag="OB"}` alone pages as a partial absence rather
    than silently switching mains-loss monitoring off while the runway arms stay green.

    ALL arms absent while the nut scrape is DOWN (or the up-gate is unqueryable) defers: that is a
    source outage Scrape Targets pages for, and it covers a dead upsd as well as a dead exporter,
    because nut-exporter fails the whole scrape when upsd is unreachable. If instead the scrape is
    answering (up-gate == 1) and the replace arm is configured, all-absent means every UPS series
    was renamed or removed at once — Scrape Targets can't see it, so page through the streak rather
    than silently unmonitor.

    A PARTIAL absence (one arm gone while the others report) is a specific series rename or removal
    — it pages (through the streak) rather than silently monitoring the survivor. UPS_CONSECUTIVE
    hysteresis (like check_ha_heartbeat) rides out a single-cycle runtime dip from a load spike or
    an exporter-restart blip; only a sustained problem pages.
    """
    configured = [
        (name, q)
        for name, q in (
            ("charge", cfg.UPS_CHARGE_QUERY),
            ("runtime", cfg.UPS_RUNTIME_QUERY),
            ("replace-battery", cfg.UPS_REPLACE_QUERY),
            ("on-battery", cfg.UPS_ON_BATTERY_QUERY),
        )
        if q
    ]
    if not configured:
        return True, "UPS monitoring disabled (no query)"
    values = {name: bridge.net.prom_scalar(cfg, q) for name, q in configured}
    if all(v is None for v in values.values()):
        # All arms gone. Every arm reads the one nut scrape, so all-absent means that scrape went
        # quiet — Scrape Targets owns that, so defer. But if it is scraping fine while every UPS
        # series was renamed or removed at once, Scrape Targets can't see it and the UPS would go
        # silently unmonitored — so gate on the scrape's own up series and fall through to the
        # partial-absence page below when it is affirmatively up AND the replace arm is
        # configured. An unqueryable or absent gate keeps the safe defer (never page over a
        # source outage another monitor owns).
        source_up = (
            bridge.net.prom_scalar(cfg, cfg.UPS_SOURCE_UP_QUERY)
            if cfg.UPS_SOURCE_UP_QUERY
            else None
        )
        if not (
            source_up is not None and source_up > 0.5 and "replace-battery" in values
        ):
            bridge.streaks._down_streaks["ups"] = 0
            return (
                True,
                "no UPS data in Prometheus (nut scrape down? "
                "Scrape Targets owns source liveness)",
            )
    # Mains loss outranks the runway arms and is judged before them. Charge and runtime read the
    # battery's RUNWAY, which can sit at 100% and 20 minutes for most of an outage and go red
    # only as the runway collapses; this arm is the outage itself. Its own streak key, because a
    # brownout cycle and a low-runway cycle sharing one counter would page at half the intended
    # grace — the rule check_host_temp's arms record.
    on_battery = ups_on_battery_verdict(values.get("on-battery"))
    if on_battery is not None:
        (
            bridge.streaks._down_streaks["ups_on_battery"],
            ok,
            msg,
        ) = bridge.streaks.down_streak(
            bridge.streaks._down_streaks.get("ups_on_battery", 0),
            cfg.UPS_CONSECUTIVE,
            on_battery[1],
            "on-battery grace",
        )
        return ok, msg
    bridge.streaks._down_streaks["ups_on_battery"] = 0
    missing = [name for name, v in values.items() if v is None]
    if missing:
        # Some configured arms present, others absent — NOT the whole-scrape-down case above but a
        # specific series rename/removal. Don't silently monitor the survivor: passing on the present
        # arm(s) would blind the missing one (e.g. keep charge green while the primary aged-battery
        # runtime signal is gone). Flag it through the same down-streak so an exporter-restart blip
        # still gets the UPS_CONSECUTIVE grace, but a sustained partial drop pages.
        #
        # A upsd outage cannot land here: nut-exporter fails the WHOLE /ups_metrics scrape when
        # upsd is unreachable, so every arm goes absent together and the branch above defers. A
        # dedicated defer for "both numeric arms absent while replace reports" existed until
        # #3105 and only ever fired because the HA fallback dropped its numeric sensors while its
        # replace-battery template floored to 0 — one source, one absence shape.
        ok, msg = (
            False,
            "UPS sensor(s) absent: %s (series renamed/removed?)" % ", ".join(missing),
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
