"""The host-facing half of monitor-bridge's configuration.

SMART, the Pi and the speedtest — every threshold read by a check in
`checks/host_thermal.py` (Scrutiny) and `checks/host_edge.py`.

A field's justification sits beside its DECLARATION; its env var name sits beside its READ in
`host_config`, and its value in `templates/config/env.yaml.j2`. `bridge/config.py` composes this into the single frozen `Config`
that `main()` builds and threads down. This module imports nothing from `bridge.config` — the
parsers arrive as arguments, so the split leaf never reaches back into its facade.
"""

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HostConfig:
    """SMART, the Pi and the speedtest."""

    SCRUTINY_URL: str
    SCRUTINY_MAX_AGE_H: float
    SCRUTINY_TEMP_MAX: float
    SCRUTINY_WEAR_MAX: float
    PI_ORIGIN: str
    PI_HOST: str
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
    SPEEDTEST_FLOOR_CONSECUTIVE: int


def host_config(
    _env: Callable[..., str],
    _int: Callable[..., int],
    _num: Callable[..., float],
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

    return HostConfig(
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
        SCRUTINY_URL=_env("SCRUTINY_URL").rstrip("/"),
        SCRUTINY_MAX_AGE_H=_num("SCRUTINY_MAX_AGE_H"),
        SCRUTINY_TEMP_MAX=_num("SCRUTINY_TEMP_MAX"),
        # NVMe endurance ceiling (percentage_used, where 100 means the controller's rated write
        # endurance is spent). Scrutiny ships this attribute with thresh=100, so its own
        # evaluation cannot fold a breach into device_status until the drive is fully consumed —
        # days of warning where the wear curve offers months. Verified against the live API
        # 2026-08-22: daniel-server's SHPP41-500GM reads 7 at 30,959 power-on hours,
        # daniel-box's CT1000E100SSD8 reads 0 at 576. 0 = disabled.
        SCRUTINY_WEAR_MAX=_num("SCRUTINY_WEAR_MAX"),
        # Pi pressure: the 512MB Zero 2 W dies by swap-thrash, not by clean failures —
        # 2026-06-11 (fwupd): hourly load5/core >1.7 episodes with healthcheck-timeout storms
        # that no other monitor saw (containers stayed "restarting", never down long enough).
        # Read from the Pi's node-exporter series on Prometheus, selected by origin (the
        # `node-pi` scrape job); glances served it until 2026-09-18 (#2004). Empty = disabled.
        PI_ORIGIN=_env("PI_ORIGIN"),
        # The Pi's LAN address, for the published-port arm's TCP connects. Empty = the arm is
        # skipped. Separate from PI_ORIGIN because an origin label is not an address.
        PI_HOST=_env("PI_HOST"),
        PI_LOAD_MAX=_num("PI_LOAD_MAX"),  # load5 per core
        PI_MEM_MIN_MB=_num("PI_MEM_MIN_MB"),
        PI_DISK_MAX_PCT=_num("PI_DISK_MAX_PCT"),
        # `name:port` pairs for the Pi containers that publish a port, rendered from daniel-pi's
        # containers_list (every entry with a `port`) so the set cannot drift from the inventory.
        # Empty = the port arm is disabled, like PI_ORIGIN disables the whole check.
        PI_PUBLISHED_PORTS=_published_ports(_env("PI_PUBLISHED_PORTS")),
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
        SPEEDTEST_URL=_env("SPEEDTEST_URL").rstrip("/"),
        # File-mounted (SPEEDTEST_TOKEN_FILE) for the same reason HA_TOKEN is: envFrom has no
        # per-key filter, so a token in monitor-bridge-env is a token in every process's
        # environment.
        SPEEDTEST_TOKEN=_env_file("SPEEDTEST_TOKEN", ""),
        # Download floor, Mbit/s. 100 WAS the empty band between two populations and is no
        # longer: re-measured 2026-09-27 (#2785). Every scheduled run now draws the pinned
        # server 41671, so the second population the band sat above is gone from the series
        # entirely, and 41671's own low tail is continuous — over the 95 six-hourly samples
        # Prometheus retained (28.4d of the 30d asked for): min 61.4, p2 75.0, p5 95.1, p10
        # 146.0, median 868.8 Mbps, with 5 samples under 100. So 100 is now a PERCENTILE CUT at
        # about p5, not a gap, and one result beneath it is a slow run rather than a slow link.
        # That is why the floor arm needs SPEEDTEST_FLOOR_CONSECUTIVE results, not one. The
        # value itself stays: it still marks the fifth percentile of a ~870 Mbps link, which is
        # the "the WAN is degraded" reading this check exists to catch.
        SPEEDTEST_DOWNLOAD_MIN_MBPS=_num("SPEEDTEST_DOWNLOAD_MIN_MBPS"),
        # Staleness ceiling, hours. SPEEDTEST_SCHEDULE runs every 6h, so 8 allows one missed slot
        # plus slack. This arm is what notices the scheduler dying — the failure mode with no
        # other symptom, since a pod that runs no tests still serves its UI and passes both
        # probes.
        SPEEDTEST_MAX_AGE_H=_num("SPEEDTEST_MAX_AGE_H"),
        # Consecutive-cycle hysteresis for the FETCH only, never for the verdict — see
        # check_speedtest. The floor arm's hysteresis is a different knob measured in a
        # different unit, SPEEDTEST_FLOOR_CONSECUTIVE below: cycles for the fetch, RESULTS for
        # the floor.
        SPEEDTEST_CONSECUTIVE=_int("SPEEDTEST_CONSECUTIVE"),
        # Consecutive sub-floor RESULTS the floor arm needs before it pages (#2785). A test runs
        # every 6h, so one slow result held the tile red for ~6h — three times in the 14 days to
        # 2026-09-27, 11.6h in total. Read from the API's own history (the fetch asks for this
        # many rows), never from bridge cycles: 72 cycles re-reading one row prove nothing, while
        # two results are two measurements. They are usually 6h apart and NOT necessarily so — a
        # manual run writes into the same results table (the series carries `scheduled="false"`
        # rows against unpinned servers), so one of the two slots can hold an operator's own
        # test minutes after the scheduled one. 2 is the smallest value that rejects a
        # single bad draw, and it caps the page's delay at one test interval. 1 restores the
        # old page-on-sight behaviour; the status and age arms are untouched by this knob and
        # still page on the newest row alone.
        SPEEDTEST_FLOOR_CONSECUTIVE=_int("SPEEDTEST_FLOOR_CONSECUTIVE"),
    )
