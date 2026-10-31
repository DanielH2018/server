"""The SMART check for monitor-bridge: drive freshness, health and NVMe wear through Scrutiny.

The host temperature and UPS checks that shared this module left in M6 batch A1: the `hosts`
rules in observability read the same series. Reads config as `cfg.X`. Every HTTP query goes
through the `src` argument (`bridge.sources.Sources`), which a test replaces with a
`FakeSources`. The verdicts it from-imports from verdicts.host_smart are patched on THIS module,
where they are bound. Rule and enforcement: bridge/config.py's header.
"""

from bridge.config import Config
from bridge.sources import Sources
from bridge.types import as_object, optional_object
from verdicts.host_smart import (
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
        name = str(dev.get("device_name") or wwn)
        model = dev.get("model_name")
        label = "%s (%s)" % (name, model) if model else name
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
        scrutiny_wear_devices(cfg, src, summary), cfg.SCRUTINY_WEAR_MAX
    )
    if not wear_ok:
        return False, wear_msg
    return True, "%s; %s; %s" % (fresh_msg, health_msg, wear_msg)
