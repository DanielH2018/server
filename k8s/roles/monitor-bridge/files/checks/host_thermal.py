"""The SMART check for monitor-bridge: drive freshness, health and NVMe wear through Scrutiny.

The host temperature and UPS checks that shared this module left in M6 batch A1: the `hosts`
rules in observability read the same series. Reads config as `cfg.X`. Every HTTP query goes
through the `src` argument (`bridge.sources.Sources`), which a test replaces with a
`FakeSources`. The verdicts it from-imports from verdicts.host_smart are patched on THIS module,
where they are bound. Rule and enforcement: bridge/config.py's header.
"""

from bridge.config import Config
from bridge.sources import Sources
from bridge.types import CheckResult, as_object, optional_object
from verdicts.host_smart import (
    device_label,
    scrutiny_device_wear,
    scrutiny_freshness,
    scrutiny_health,
    scrutiny_wear_verdict,
)


def scrutiny_wear_devices(
    cfg: Config, src: Sources, summary: dict | None
) -> list[tuple[str, float | None]]:
    """One /api/device/<wwn>/details fetch per non-archived device.

    The wear attributes are not in /api/summary, which is what makes this N calls per cycle rather
    than none — the same shape as any check that reads several sources per cycle. Each payload is ~19 KB
    and only smart_results[0] is read. A failing fetch raises out of `src.get_json` and the runner
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
        label = device_label(wwn, dev)
        details = optional_object(
            src.get_json("%s/api/device/%s/details" % (cfg.SCRUTINY_URL, wwn)),
            "scrutiny device details",
        )
        devices.append((label, scrutiny_device_wear(details)))
    return devices


def check_scrutiny(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Checks Scrutiny's summary for freshness, drive health, and (if configured) wear.

    Fetches /api/summary once; per-device wear details are fetched only when freshness and
    health both pass and cfg.SCRUTINY_WEAR_MAX is set. Returns (ok, msg).
    """
    data = as_object(
        src.get_json(cfg.SCRUTINY_URL + "/api/summary"), "scrutiny summary"
    )
    summary = optional_object(
        as_object(data.get("data") or {}, "scrutiny data").get("summary"),
        "scrutiny summary",
    )
    fresh = CheckResult.of(scrutiny_freshness(summary, cfg.SCRUTINY_MAX_AGE_H))
    if not fresh.ok:
        return fresh
    health = CheckResult.of(scrutiny_health(summary, cfg.SCRUTINY_TEMP_MAX))
    if not health.ok:
        return health
    fresh_msg, health_msg = fresh.msg, health.msg
    # Folded into this check because wear answers the same question as device_status, months
    # earlier; fetched only once freshness passes.
    if not cfg.SCRUTINY_WEAR_MAX:
        return True, "%s; %s" % (fresh_msg, health_msg)
    wear = CheckResult.of(
        scrutiny_wear_verdict(
            scrutiny_wear_devices(cfg, src, summary), cfg.SCRUTINY_WEAR_MAX
        )
    )
    if not wear.ok:
        return wear
    return True, "%s; %s; %s" % (fresh_msg, health_msg, wear.msg)
