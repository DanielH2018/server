"""The host-facing half of monitor-bridge's configuration.

Disks, certificates, memory, SMART, board temperatures, the UPS, the Pi and the speedtest —
every threshold read by a check in `checks/host.py`, `checks/host_thermal.py` and
`checks/host_edge.py`, plus the origin-coverage floors the
host-metric checks fail closed on.

A field's justification sits beside its DECLARATION; its env var name and default sit beside
its READ in `host_config`. `bridge/config.py` composes this into the single frozen `Config`
that `main()` builds and threads down. This module imports nothing from `bridge.config` — the
parsers arrive as arguments, so the split leaf never reaches back into its facade.
"""

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HostConfig:
    """Disks, certificates, memory, SMART, temperatures, the UPS, the Pi and the speedtest."""

    DISK_MOUNTPOINTS: tuple[str, ...]
    DISK_MAX_PCT: float
    CERT_MIN_DAYS: float
    MEM_MAX_PCT: float
    HOST_METRIC_ORIGIN_EXCLUDE: str
    SCRUTINY_URL: str
    SCRUTINY_MAX_AGE_H: float
    SCRUTINY_TEMP_MAX: float
    SCRUTINY_WEAR_MAX: float
    HWMON_TEMP_RATIO: float
    HWMON_TEMP_FALLBACK_C: float
    # Per-sensor published ratings, ((instance, chip, sensor), rated_max_c) — the part's own
    # Tjmax for a sensor whose driver declares no max and no crit. A tuple rather than a dict so
    # the frozen Config stays hashable.
    HWMON_TEMP_RATED_MAX_C: tuple[tuple[tuple[str, str, str], float], ...]
    HWMON_TEMP_MIN_PLAUSIBLE_C: float
    HWMON_TEMP_MAX_PLAUSIBLE_C: float
    HWMON_TEMP_EXCLUDE_CHIP: str
    HWMON_TEMP_CONSECUTIVE: int
    HWMON_TEMP_ORIGINS_MIN: int
    HWMON_TEMP_ORIGINS_CONSECUTIVE: int
    UPS_CHARGE_QUERY: str
    UPS_RUNTIME_QUERY: str
    UPS_REPLACE_QUERY: str
    UPS_HA_UP_QUERY: str
    UPS_CHARGE_MIN_PCT: float
    UPS_RUNTIME_MIN_S: float
    UPS_CONSECUTIVE: int
    PI_GLANCES_URL: str
    PI_LOAD_MAX: float
    PI_MEM_MIN_MB: float
    PI_DISK_MAX_PCT: float
    PI_PUBLISHED_PORTS: tuple[tuple[str, int], ...]
    PI_PORT_TIMEOUT: float
    PI_PORTS_CONSECUTIVE: int
    SPEEDTEST_URL: str
    SPEEDTEST_TOKEN: str = field(repr=False)
    SPEEDTEST_DOWNLOAD_MIN_MBPS: float
    SPEEDTEST_MAX_AGE_H: float
    SPEEDTEST_CONSECUTIVE: int
    HOST_ORIGINS_MIN: int
    HOST_ORIGINS_CONSECUTIVE: int
    # The cgroups whose `claude_cgroup_*` textfile series MUST be reporting for the arm folded
    # into check_mem to mean anything. Empty disables the whole arm, like PI_GLANCES_URL.
    CLAUDE_CGROUPS: tuple[str, ...]
    CLAUDE_CGROUP_STALL_WINDOW: str
    CLAUDE_CGROUP_STALL_MAX_PCT: float
    CLAUDE_CGROUP_EVENT_WINDOW: str
    CLAUDE_CGROUP_EVENTS: str
    CLAUDE_CGROUP_CONSECUTIVE: int


def host_config(
    _env: Callable[..., str],
    _int: Callable[[str, str], int],
    _num: Callable[[str, str], float],
    _env_file: Callable[..., str],
    problems: list[str],
) -> HostConfig:
    """The host fields, read through the parsers `load_config` built over its environment."""

    def _published_ports(raw: str) -> tuple[tuple[str, int], ...]:
        """`name:port` pairs from a comma-separated list, skipping a malformed port."""
        pairs = []
        for pair in raw.split(","):
            if ":" not in pair:
                continue
            name, _, port = pair.partition(":")
            try:
                pairs.append((name.strip(), int(port)))
            except ValueError:
                problems.append(
                    "PI_PUBLISHED_PORTS entry %r has a non-integer port; it is not watched"
                    % pair
                )
        return tuple(pairs)

    def _rated_max(raw: str) -> tuple[tuple[tuple[str, str, str], float], ...]:
        """`instance/chip/sensor=celsius` entries from a comma-separated list.

        `/` separates the three key components because an hwmon chip label carries colons of its
        own (`pci0000:00_0000:00:18_3`) and carries no slash. A malformed entry is REPORTED and
        dropped rather than guessed at: the sensor then falls to the flat fallback, which is the
        conservative direction — it pages earlier, never later.
        """
        rated = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            key, sep, value = entry.partition("=")
            parts = key.split("/")
            if not sep or len(parts) != 3:
                problems.append(
                    "HWMON_TEMP_RATED_MAX_C entry %r is not instance/chip/sensor=celsius; "
                    "that sensor keeps the flat fallback" % entry
                )
                continue
            try:
                rated.append(
                    (
                        (parts[0].strip(), parts[1].strip(), parts[2].strip()),
                        float(value),
                    )
                )
            except ValueError:
                problems.append(
                    "HWMON_TEMP_RATED_MAX_C entry %r has a non-numeric limit; that sensor "
                    "keeps the flat fallback" % entry
                )
        return tuple(rated)

    return HostConfig(
        DISK_MOUNTPOINTS=tuple(
            m.strip() for m in _env("DISK_MOUNTPOINTS", "/").split(",") if m.strip()
        ),
        DISK_MAX_PCT=_num("DISK_MAX_PCT", "90"),
        CERT_MIN_DAYS=_num("CERT_MIN_DAYS", "14"),
        MEM_MAX_PCT=_num("MEM_MAX_PCT", "90"),
        # Origins that check_disk/check_mem must NOT scan, as a regex alternation. See
        # host_metric_sel: daniel-pi runs node-exporter like the other hosts, but
        # check_pi_pressure owns its disk and memory with thresholds sized for a 456 MB box.
        HOST_METRIC_ORIGIN_EXCLUDE=_env("HOST_METRIC_ORIGIN_EXCLUDE", "daniel-pi"),
        # Scrutiny SMART freshness + health: the collector cron runs daily (00:00) and has no
        # usable container healthcheck (cron is PID 1) — a silently-dead collector only shows as
        # aging collector_date values in the web API. 26h allows one run + slack. On TOP of
        # freshness we assert each device's `device_status` == 0: freshness only proves the
        # collector still reports, so a drive that goes SMART-FAILED / breaches a Scrutiny
        # attribute threshold while STILL reporting fresh data would otherwise page nothing
        # (Scrutiny stores to InfluxDB, not Prometheus, and its own Shoutrrr notifier is
        # unconfigured — this bridge check is the only alert path). SCRUTINY_TEMP_MAX is an
        # optional temperature ceiling (°C); 0 = disabled (default), since Scrutiny already
        # folds the SMART temperature attribute into device_status — the ceiling is just an
        # earlier-warning lever.
        SCRUTINY_URL=_env("SCRUTINY_URL", "http://scrutiny:8080").rstrip("/"),
        SCRUTINY_MAX_AGE_H=_num("SCRUTINY_MAX_AGE_H", "26"),
        SCRUTINY_TEMP_MAX=_num("SCRUTINY_TEMP_MAX", "0"),
        # NVMe endurance ceiling (percentage_used, where 100 means the controller's rated write
        # endurance is spent). Scrutiny ships this attribute with thresh=100, so its own
        # evaluation cannot fold a breach into device_status until the drive is fully consumed —
        # days of warning where the wear curve offers months. Verified against the live API
        # 2026-08-22: daniel-server's SHPP41-500GM reads 7 at 30,959 power-on hours,
        # daniel-box's CT1000E100SSD8 reads 0 at 576. 0 = disabled.
        SCRUTINY_WEAR_MAX=_num("SCRUTINY_WEAR_MAX", "80"),
        # Board and CPU temperature from node-exporter's hwmon collector. Drives are NOT read
        # here — check_scrutiny owns them (its device_status folds the SMART temperature
        # attribute), so HWMON_TEMP_EXCLUDE_CHIP drops the nvme chips and the two checks never
        # page for one condition.
        #
        # Each sensor gets a limit from ONE of two arms, and the arms are exhaustive over the
        # scraped vector — every non-excluded series is covered, which is what
        # test_host_temp_covers_every_sensor pins. Measured live 2026-08-28: 21 temp series
        # (daniel-server 12, daniel-box 7, daniel-pi 2).
        #
        #   1. The sensor's own declared max, when it declares a PLAUSIBLE one: page at
        #      HWMON_TEMP_RATIO of it. Preferred — coretemp declares 100, the daniel-server
        #      NVMe 85.85. Where max is absent/implausible but the sensor declares a plausible
        #      crit (node_hwmon_temp_crit_celsius), crit is used instead — a driver need not
        #      publish max at all (k10temp on daniel-box publishes NEITHER for Tctl, verified
        #      against /sys/class/hwmon/hwmon2/ 2026-09-03; issue #995). max wins over crit when
        #      both are plausible: hwmon's convention has crit as the LATER shutdown point
        #      (daniel-server's NVMe measures max 85.85 / crit 86.85), so ratioing crit would
        #      page closer to failure than the max-based 90% this estate already runs on.
        #   2. HWMON_TEMP_FALLBACK_C, a flat ceiling, for every sensor that declares neither.
        #
        # DECIDED: a declared max or crit is sanity-bounded rather than trusted, because three
        # of the ten max-declaring sensors declare 65261.85 (0xFFFF sentinel, an undeclared max
        # encoded as a number): daniel-server nvme temp2/temp3 and daniel-box nvme temp3.
        # Ratio-of-max against that is unreachable, so those sensors would read green through a
        # fire — the inert-check class in [[an-optimisation-can-land-green-and-be-inert]]. A
        # declared value outside (HWMON_TEMP_MIN_PLAUSIBLE_C, HWMON_TEMP_MAX_PLAUSIBLE_C] is
        # therefore treated as UNDECLARED and falls through (crit, then arm 2). This is also why
        # the fallback arm is not optional: without it, 14 of 21 sensors — including BOTH
        # daniel-pi sensors, on the host with no fan — carry no limit at all.
        HWMON_TEMP_RATIO=_num("HWMON_TEMP_RATIO", "0.90"),
        # 85C is an estate-wide flat default, not any chip's own rating, so it is the arm of last
        # resort rather than a limit anybody chose per part.
        HWMON_TEMP_FALLBACK_C=_num("HWMON_TEMP_FALLBACK_C", "85"),
        # Arm 3, and the one that keeps arm 2 rare: a sensor whose driver declares nothing can
        # still have a PUBLISHED rating, and an operator can supply it. Ratioed exactly like a
        # declared max, so a rated 100 and a declared 100 produce the same 90C limit.
        # Empty by default — a rating belongs beside the host it describes, in
        # templates/env-secret.yaml.j2, where the part number justifying the number is reviewable.
        # daniel-box's k10temp/Tctl is the entry that exists: AMD publishes Tjmax 100C for the
        # Ryzen 7 8845HS. See the `DECIDED: daniel-box's k10temp/Tctl takes its limit` marker in
        # verdicts.host.hwmon_temp_limits for that evidence and for how to fetch amd.com, and the
        # `DECIDED: k10temp subtracts no Tctl offset` marker above it for why Tctl is the
        # junction temperature this compares (#1003, #1152).
        HWMON_TEMP_RATED_MAX_C=_rated_max(_env("HWMON_TEMP_RATED_MAX_C", "")),
        HWMON_TEMP_MIN_PLAUSIBLE_C=_num("HWMON_TEMP_MIN_PLAUSIBLE_C", "20"),
        HWMON_TEMP_MAX_PLAUSIBLE_C=_num("HWMON_TEMP_MAX_PLAUSIBLE_C", "150"),
        HWMON_TEMP_EXCLUDE_CHIP=_env("HWMON_TEMP_EXCLUDE_CHIP", "nvme_"),
        # Hysteresis: a transcode or a compile spikes coretemp for one scrape. 3 cycles at the
        # loop cadence is sustained heat, not a burst.
        HWMON_TEMP_CONSECUTIVE=_int("HWMON_TEMP_CONSECUTIVE", "3"),
        # Host-coverage floor for the thermal check, the peer of HOST_ORIGINS_MIN and
        # deliberately a DIFFERENT number. Until 2026-08-29 hwmon_temp_verdict paged only on a
        # fully empty vector, so any non-empty subset passed: lose one host's hwmon collector
        # and the other two answered "all below limit" for the whole estate, forever. A total
        # node-exporter death is already caught by check_cluster_targets; the gap is the PARTIAL
        # failure — node-exporter up, one collector blind — which check.py's own
        # HOST_ORIGINS_MIN comment names as node-exporter's normal failure mode.
        #
        # 3 rather than the shared 2 because all three hosts declare non-excluded sensors,
        # measured live 2026-08-29 after HWMON_TEMP_EXCLUDE_CHIP: daniel-server 9, daniel-box 5,
        # daniel-pi 2. The shared floor of 2 would be met by any two of them, which is exactly
        # the state this must catch.
        HWMON_TEMP_ORIGINS_MIN=_int("HWMON_TEMP_ORIGINS_MIN", "3"),
        # Its own grace, longer than HOST_ORIGINS_CONSECUTIVE, because the third host is
        # daniel-pi and the Pi drops out for longer than either amd64 node. Measured over the 7d
        # to 2026-08-29 at a 5m step: 1054 samples, coverage below 3 in 6 of them, all daniel-pi
        # (1048/1054 present), and the worst 30m window held 4 consecutive short samples — about
        # 20 minutes. The shared grace of 3 cycles is 15 minutes at INTERVAL=300, so it would
        # have paged once in that week on a healthy estate. 5 cycles is 25 minutes: one cycle of
        # margin over the observed worst case.
        HWMON_TEMP_ORIGINS_CONSECUTIVE=_int("HWMON_TEMP_ORIGINS_CONSECUTIVE", "5"),
        # UPS battery health via Home Assistant's Prometheus scrape (the APC UPS is on
        # NUT/peanut; HA's prometheus integration exposes its sensors as hass_sensor_*). The
        # only pre-existing UPS alert is an HA automation -> mobile push (a separate channel
        # from this Kuma->Discord brain), and nothing trends the battery, so a slowly-degrading
        # battery — full-charge runtime decaying over years — is invisible until an outage
        # collapses it. We page on a low battery RUNWAY: charge below UPS_CHARGE_MIN_PCT (a deep
        # discharge while on battery) OR estimated runtime below UPS_RUNTIME_MIN_S (an aged
        # battery even at full charge, or a discharge nearing shutdown) — a dual-purpose health
        # + imminent-cutoff floor — PLUS the UPS's own replace-battery self-test verdict
        # (UPS_REPLACE_QUERY), the earliest signal, which can trip while charge/runtime still
        # read fine. Queries are env-driven (all empty = disabled, like PI_GLANCES_URL) so a
        # UPS/entity rename or removal needs no code edit. Prom-dependent: an HA-scrape outage
        # leaves ALL series absent -> up (Scrape Targets owns HA-source liveness; the nut pod
        # liveness probe owns NUT-server death), so this never double-pages those; a PARTIAL
        # drop (one arm gone) pages instead of silently monitoring the survivor. UPS_CONSECUTIVE
        # rides out a one-cycle dip from a transient load spike (like HA_CONSECUTIVE), so only a
        # sustained problem pages.
        UPS_CHARGE_QUERY=_env(
            "UPS_CHARGE_QUERY",
            'hass_sensor_battery_percent{entity="sensor.apc_ups_battery_charge"}',
        ),
        UPS_RUNTIME_QUERY=_env(
            "UPS_RUNTIME_QUERY",
            'hass_sensor_duration_s{entity="sensor.apc_ups_battery_runtime"}',
        ),
        # The UPS's own "Replace Battery" self-test verdict (NUT `ups.status` RB flag).
        # Charge/runtime are a lagging runway proxy — a failed periodic self-test can trip RB
        # while both still read fine — so this is the earliest actionable replace-the-battery
        # signal, and it reached NEITHER alert channel before (the HA ups_power_event automation
        # only branches on OB/LB, and check_ups read only charge/runtime). Exposed as a numeric
        # 0/1 series by an HA template binary_sensor (home-assistant templates.yaml), which
        # stays on/off — never unknown — while HA is up, so its absence means the whole HA scrape
        # is down (all arms absent -> defer), not a silent single-arm drop. Empty = arm disabled.
        UPS_REPLACE_QUERY=_env(
            "UPS_REPLACE_QUERY",
            'hass_binary_sensor_state{entity="binary_sensor.apc_ups_replace_battery"}',
        ),
        # HA's own scrape-up series, used only to discriminate the all-arms-absent case: HA's
        # whole Prometheus scrape being down (all hass_sensor_* vanish → Scrape Targets owns it,
        # defer) vs HA scraping fine while every UPS entity was renamed/removed at once (Scrape
        # Targets can't see it → the UPS would go silently unmonitored). Empty disables the gate
        # (always defer, the old behaviour).
        UPS_HA_UP_QUERY=_env("UPS_HA_UP_QUERY", 'up{job="home-assistant"}'),
        UPS_CHARGE_MIN_PCT=_num("UPS_CHARGE_MIN_PCT", "50"),
        UPS_RUNTIME_MIN_S=_num("UPS_RUNTIME_MIN_S", "300"),
        UPS_CONSECUTIVE=_int("UPS_CONSECUTIVE", "2"),
        # Pi pressure: the 512MB Zero 2 W dies by swap-thrash, not by clean failures —
        # 2026-06-11 (fwupd): hourly load5/core >1.7 episodes with healthcheck-timeout storms
        # that no other monitor saw (containers stayed "restarting", never down long enough).
        # Polled from the glances API already running on the Pi (zero added Pi footprint); the
        # separate static Kuma HTTP monitor covers glances itself being down.
        PI_GLANCES_URL=_env("PI_GLANCES_URL", "").rstrip("/"),
        PI_LOAD_MAX=_num("PI_LOAD_MAX", "1.5"),  # load5 per core
        PI_MEM_MIN_MB=_num("PI_MEM_MIN_MB", "50"),
        PI_DISK_MAX_PCT=_num("PI_DISK_MAX_PCT", "90"),
        # `name:port` pairs for the Pi containers that publish a port, rendered from daniel-pi's
        # containers_list (every entry with a `port`) so the set cannot drift from the inventory.
        # Empty = the port arm is disabled, like PI_GLANCES_URL disables the whole check.
        PI_PUBLISHED_PORTS=_published_ports(_env("PI_PUBLISHED_PORTS", "")),
        PI_PORT_TIMEOUT=_num("PI_PORT_TIMEOUT", "3"),
        # A Pi deploy recreates containers, so their ports are genuinely closed for a few
        # seconds. Two cycles of grace, same idiom as HA_CONSECUTIVE; a detached container
        # persists until someone recreates it and still pages.
        PI_PORTS_CONSECUTIVE=_int("PI_PORTS_CONSECUTIVE", "2"),
        # speedtest-tracker's own result rows. Empty URL/token = disabled (stays up), like HA
        # above.
        #
        # WHY THIS CHECK EXISTS: a failed speedtest run wrote nothing anywhere an operator could
        # see — no stdout line (the container logs only its pre-run connectivity ping), no
        # metric, no monitor. The only record was a row in the app's sqlite, which nothing could
        # read: the readonly SA holds no pods/exec, and the unauthenticated API is two endpoints,
        # one of which returns a single row. Five of the 42 runs between 2026-08-14 and
        # 2026-08-24 failed and none of them paged.
        SPEEDTEST_URL=_env("SPEEDTEST_URL", "").rstrip("/"),
        # File-mounted (SPEEDTEST_TOKEN_FILE) for the same reason HA_TOKEN is: envFrom has no
        # per-key filter, so a token in monitor-bridge-env is a token in every process's
        # environment.
        SPEEDTEST_TOKEN=_env_file("SPEEDTEST_TOKEN", ""),
        # Download floor, Mbit/s. 100 is not a target — it is the empty band. Results are bimodal
        # by which Ookla server the run drew: over 2026-08-14..24 the 20 runs on server 41671 had
        # a median of 910 Mbps and a worst of 119, while the 17 runs on six other servers had a
        # median of 12.8 and a best of 42.8. Nothing landed between 42.8 and 119, so any floor in
        # that gap separates the two populations with room on both sides.
        SPEEDTEST_DOWNLOAD_MIN_MBPS=_num("SPEEDTEST_DOWNLOAD_MIN_MBPS", "100"),
        # Staleness ceiling, hours. SPEEDTEST_SCHEDULE runs every 6h, so 8 allows one missed slot
        # plus slack. This arm is what notices the scheduler dying — the failure mode with no
        # other symptom, since a pod that runs no tests still serves its UI and passes both
        # probes.
        SPEEDTEST_MAX_AGE_H=_num("SPEEDTEST_MAX_AGE_H", "8"),
        # Consecutive-cycle hysteresis for the FETCH only, never for the verdict — see
        # check_speedtest.
        SPEEDTEST_CONSECUTIVE=_int("SPEEDTEST_CONSECUTIVE", "2"),
        # Distinct `origin` values the host-metric checks must see. node-exporter is a DaemonSet
        # on both nodes, so a vector grouped by origin returning fewer than this has LOST a host,
        # not measured a healthy estate — and check_disk/check_mem would report the survivor's
        # numbers as the estate's. Live on 2026-08-23: daniel-box's node-exporter was unreachable
        # for 5.4h (a one-directional UFW rule, k3s defaults k3s_join_server_ports) and both
        # checks pushed OK off daniel-server alone, so daniel-box's host memory and /boot went
        # unwatched behind two green tiles for the whole window.
        #
        # Why a floor here and not reliance on the Scrape Targets sentinel: that check keys on
        # `up`, and node-exporter's normal failure mode is PER-COLLECTOR —
        # `node_scrape_collector_success == 0` already returns five collectors on a host whose
        # `up` is 1. A filesystem or meminfo collector failing therefore leaves `up == 1`, leaves
        # Scrape Targets green, and drops the host from these two checks with nothing firing
        # anywhere. Same shape as check_ups's partial-absence arm: never monitor the survivor
        # silently. Verified before setting the floor: /, /boot and /boot/efi each report from
        # both origins over the preceding 7d, so no mountpoint is legitimately single-host.
        HOST_ORIGINS_MIN=_int("HOST_ORIGINS_MIN", "2"),
        # Hysteresis, for the same reason UPS_CONSECUTIVE exists: the weekly Sunday reboot takes
        # a node's node-exporter away for minutes against a 1m scrape and a 5m check loop, and a
        # bare floor would page every week. Measured over the 7d to 2026-08-23,
        # `count(node_memory_MemTotal_bytes) < 2` held for 66 samples and ALL 66 were inside the
        # real outage — so the floor is quiet in steady state and this grace only has to cover
        # reboots.
        HOST_ORIGINS_CONSECUTIVE=_int("HOST_ORIGINS_CONSECUTIVE", "3"),
        # ── The claude-rc / login-slice cgroup arm (issue #1258) ────────────────────────────
        # PR #1251 put `claude_cgroup_*` into Prometheus and nothing read them. The 2026-09-05
        # incident (#1243) began with the claude-rc cgroup stalled in memory reclaim for about
        # ten minutes before anything downstream failed, and this arm is what sees that shape
        # while it is still only a stall.
        #
        # Folded into check_mem rather than given its own Kuma monitor, for the reason recorded
        # at with_pi_ports: a new monitor costs a new push token in SOPS and a monitor created by
        # hand in the Kuma UI. `memory` already owns "this box is running out of memory", and a
        # cgroup taking the box is that — on 2026-09-05 this cgroup held all 8 GiB of swap plus
        # 6.96 GB anon while k3s got 2.9 GB.
        #
        # The cgroups that must be REPORTING for the arm to mean anything — `claude-rc` and
        # `fleet`, not `user-1000-slice`: the login slice's cgroup exists only once a login session
        # has happened since boot, so requiring it would page after any reboot nobody had SSH'd
        # into. Series from any other cgroup are still judged — the queries below filter by metric,
        # not by cgroup — so user-1000-slice is covered when it exists and absent without paging
        # when it does not.
        # Empty disables the arm entirely, like PI_GLANCES_URL and PI_PUBLISHED_PORTS; the live
        # value is set in templates/env-secret.yaml.j2, beside the host it describes.
        CLAUDE_CGROUPS=tuple(
            c.strip() for c in _env("CLAUDE_CGROUPS", "").split(",") if c.strip()
        ),
        # PSI `full` stall rate window. The writer's timer samples cgroupfs every 30s, but the
        # BINDING sample interval is Prometheus's `job: node` scrape_interval of 1m
        # (claude-otel/templates/prometheus.yaml.j2) — the extra timer tick only guarantees the
        # textfile is fresh at every scrape. 5m is therefore 5 samples: enough that one missed
        # scrape cannot empty the window, and short enough that the arm sees a stall inside the
        # ten minutes the 2026-09-05 incident took to reach anything downstream.
        CLAUDE_CGROUP_STALL_WINDOW=_env("CLAUDE_CGROUP_STALL_WINDOW", "5m"),
        # Percent of wall time the cgroup spent with EVERY task stalled on memory (PSI `full`).
        #
        # DERIVATION, and what to re-derive. The floor is measured: over the whole retained
        # history of this metric on 2026-09-06 (388 samples per series, ~6.5h, `resets()` = 0 —
        # the series began when PR #1251 deployed), the highest 5m-window `full` stall rate was
        # 0.00040% for claude-rc and 0.00128% for user-1000-slice, with p99 at 0.00024% and
        # 0.00099%. Steady state is four to five orders of magnitude below any number here.
        #
        # The ceiling comes from what `full` MEANS, not from a measured breach: `full` counts
        # time when no task in the cgroup made progress at all. 10% of a 5m window is 30 seconds
        # of a cgroup doing nothing but reclaim. That is degraded by definition, at any load —
        # which is the argument, because 6.5h of quiet data cannot bound the legitimate peak of a
        # pytest fan-out or a large deploy. It is deliberately NOT derived from the "stalled in
        # reclaim 98.5% of the time" figure in roles/setup/claude_code/defaults/main.yml: that
        # number describes the 2026-09-05 incident, which predates this metric, so it is a
        # different instrument and cannot bound this series.
        #
        # RE-DERIVE once there are seven days of history, which is 2026-09-12:
        #   probe.py metric 'max_over_time((rate(claude_cgroup_memory_pressure_stalled_usec_total{kind="full"}[5m])/10000)[7d:1m])'
        # If the observed max under real load lands anywhere near 10, lower this to sit between
        # the two populations the way SPEEDTEST_DOWNLOAD_MIN_MBPS does. Tracked as issue #1288.
        CLAUDE_CGROUP_STALL_MAX_PCT=_num("CLAUDE_CGROUP_STALL_MAX_PCT", "10"),
        # memory.events increase window. Wider than the stall window because these are rare
        # discrete events rather than a rate: 10m at a 1m scrape keeps an OOM kill visible across
        # two check cycles, so a single missed cycle cannot lose it.
        CLAUDE_CGROUP_EVENT_WINDOW=_env("CLAUDE_CGROUP_EVENT_WINDOW", "10m"),
        # Which memory.events counters page on ANY increase, as a regex alternation.
        #
        # `oom`, `oom_kill` and `oom_group_kill` are unambiguous: the kernel killed something in
        # this cgroup. `max` means memory.max was hit — a hard cap breach wherever one is set,
        # and a no-op on claude-rc, where the unit template sets MemoryMax deliberately to
        # infinity.
        #
        # `high` is EXCLUDED, and that is the one exclusion worth justifying. MemoryHigh throttles
        # by design, so a `high` increase is the cap working rather than a fault, and it is keyed
        # directly to a value another role owns and is actively changing (#1264 moves
        # claude_code_rc_memory_high / _swap_max). Every event kept here means the same thing
        # whatever those caps become. `high` is graphed instead, on the AI/claude-code-host-cgroups
        # board.
        CLAUDE_CGROUP_EVENTS=_env(
            "CLAUDE_CGROUP_EVENTS", "max|oom|oom_kill|oom_group_kill"
        ),
        # Consecutive breaching cycles before the arm pages, doing double duty.
        #
        # It suppresses a burst: 2 cycles at INTERVAL=300 is ten minutes of sustained stall,
        # which is the duration the 2026-09-05 stall ran before anything downstream failed — so
        # this pages while the fault is still only a stall, and one transient window cannot.
        #
        # It also suppresses a counter-reset artifact. claude-rc-restart.timer restarts the unit
        # weekly (claude_code_rc_restart_schedule), which destroys and recreates the cgroup and
        # zeroes every counter here. Prometheus extrapolates across a reset, so a single
        # `increase()` window spanning one can report a non-zero rise from a counter that went
        # 0 -> 0 — a false page on restart week for an arm where any increase pages. No reset is
        # visible in the retained data (`resets(...[7d])` = 0 on 2026-09-06, because the series
        # is younger than one restart interval), so this is guarded rather than measured.
        CLAUDE_CGROUP_CONSECUTIVE=_int("CLAUDE_CGROUP_CONSECUTIVE", "2"),
    )
