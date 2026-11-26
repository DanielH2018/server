"""The host-facing half of monitor-bridge's configuration.

SMART and the speedtest — every threshold read by a check in
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
    """SMART and the speedtest."""

    SCRUTINY_URL: str
    SCRUTINY_MAX_AGE_H: float
    SCRUTINY_TEMP_MAX: float
    SCRUTINY_WEAR_MAX: float
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
) -> HostConfig:
    """The host fields, read through the parsers `load_config` built over its environment."""
    return HostConfig(
        # The collector cron has no container healthcheck, so freshness (26h) shows a dead
        # collector; `device_status` == 0 is asserted too because a SMART-failed drive can still
        # report fresh.
        # This check is the only alert path. SCRUTINY_TEMP_MAX is an optional ceiling in C, 0 =
        # disabled.
        SCRUTINY_URL=_env("SCRUTINY_URL").rstrip("/"),
        SCRUTINY_MAX_AGE_H=_num("SCRUTINY_MAX_AGE_H"),
        SCRUTINY_TEMP_MAX=_num("SCRUTINY_TEMP_MAX"),
        # NVMe percentage_used ceiling: Scrutiny's own thresh is 100, so it flags only a fully
        # consumed drive. 0 = disabled.
        SCRUTINY_WEAR_MAX=_num("SCRUTINY_WEAR_MAX"),
        # A failed speedtest run leaves no stdout line, metric or monitor, and its only record is an
        # unreadable sqlite row, so this check reads the API. Empty URL/token = disabled.
        SPEEDTEST_URL=_env("SPEEDTEST_URL").rstrip("/"),
        # File-mounted: `envFrom` has no per-key filter, so a token in monitor-bridge-env is in
        # every process's environment.
        SPEEDTEST_TOKEN=_env_file("SPEEDTEST_TOKEN", ""),
        # Download floor, Mbit/s: 100 is a percentile cut near p5 of a ~870 Mbps link, so one result
        # beneath it is a slow run, not a slow link;
        # the floor arm needs SPEEDTEST_FLOOR_CONSECUTIVE results.
        SPEEDTEST_DOWNLOAD_MIN_MBPS=_num("SPEEDTEST_DOWNLOAD_MIN_MBPS"),
        # Staleness ceiling, hours: SPEEDTEST_SCHEDULE runs every 6h, so 8 allows one missed slot.
        # It is what notices a dead scheduler, which still serves its UI.
        SPEEDTEST_MAX_AGE_H=_num("SPEEDTEST_MAX_AGE_H"),
        # Hysteresis for the FETCH only, never the verdict: this counts cycles, while
        # SPEEDTEST_FLOOR_CONSECUTIVE counts RESULTS.
        SPEEDTEST_CONSECUTIVE=_int("SPEEDTEST_CONSECUTIVE"),
        # Consecutive sub-floor RESULTS the floor arm needs before it pages. A test runs
        # every 6h, so one slow result would hold the tile red for ~6h. Read from the API's own history (the fetch asks for this
        # many rows), never from bridge cycles: 72 cycles re-reading one row prove nothing, while
        # two results are two measurements. They are usually 6h apart and NOT necessarily so — a
        # manual run writes into the same results table (the series carries `scheduled="false"`
        # rows against unpinned servers), so one of the two slots can hold an operator's own
        # test minutes after the scheduled one. 2 is the smallest value that rejects a
        # single bad draw, and it caps the page's delay at one test interval. 1 pages on
        # sight; the status and age arms are untouched by this knob and
        # still page on the newest row alone.
        SPEEDTEST_FLOOR_CONSECUTIVE=_int("SPEEDTEST_FLOOR_CONSECUTIVE"),
    )
