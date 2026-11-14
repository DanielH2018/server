"""The domain's registration expiry over RDAP, for monitor-bridge (#8519).

A lapsed registration takes down ACME renewal, every public route and the WireGuard endpoint
name at once, and the alert plane's own notifications with them. `CertExpiringSoon` watches the
certificates Traefik serves, not the registration behind them.

The check reads the registry's RDAP record, which needs no credential, and takes the date of
its `expiration` event. A success is cached for DOMAIN_REGISTRATION_PROBE_INTERVAL_S (a day),
and a failure is never cached, the `checks/cloudflare_ips.py` idiom. The row is down only when
the record cannot be read or carries no expiration. The 45- and 14-day thresholds are not here:
`verdict_export.CheckExposition` serves the days left as a gauge, and two rules in
observability's `bridge-exporter.yaml` compare it. `days_remaining` recomputes the days from
the cached date at every call, so the gauge never lags the clock by the cache's day.

The cache is `src.state.domain_registration_probe` (`bridge.streaks.RegistrationCache`). It
keeps the last expiry through a failed read, so an RDAP outage leaves the gauge in place.
"""

import datetime
import time

from bridge.config import Config
from bridge.sources import Sources
from bridge.streaks import RegistrationCache
from bridge.types import JsonValue

DAY_S = 86400.0


def expiry_from_rdap(doc: JsonValue) -> float:
    """The Unix time of an RDAP domain record's `expiration` event.

    Raises ValueError when the record carries no such event or its date does not parse, so the
    caller reports a malformed answer rather than a date.
    """
    events = doc.get("events") if isinstance(doc, dict) else None
    for event in events if isinstance(events, list) else []:
        if isinstance(event, dict) and event.get("eventAction") == "expiration":
            return datetime.datetime.fromisoformat(
                str(event.get("eventDate"))
            ).timestamp()
    raise ValueError("the RDAP record carries no expiration event")


def days_remaining(probe: RegistrationCache, now: float) -> float | None:
    """Days from `now` to the cached expiry, or None when no read has succeeded yet."""
    if probe["expiry"] is None:
        return None
    return (probe["expiry"] - now) / DAY_S


def domain_registration(
    cfg: Config,
    src: Sources,
    now: float | None = None,
    probe: RegistrationCache | None = None,
) -> tuple[bool, str]:
    """Throttled RDAP read of the registration expiry. (ok, msg).

    `src` and `probe` are the seams: a test hands in a fake `src` and a fresh cache rather than
    patching this module. None reads `src.state.domain_registration_probe`.
    """
    probe = src.state.domain_registration_probe if probe is None else probe
    if not cfg.DOMAIN_RDAP_URL:
        return True, "domain registration check disabled (no DOMAIN_RDAP_URL)"
    now = now if now is not None else time.time()
    fresh = (
        probe["ts"] is not None
        and probe["ok"]
        and now - probe["ts"] < cfg.DOMAIN_REGISTRATION_PROBE_INTERVAL_S
    )
    if not fresh:
        probe["ts"] = now
        try:
            probe["expiry"] = expiry_from_rdap(src.get_json(cfg.DOMAIN_RDAP_URL))
        except Exception as e:
            probe["ok"] = False
            return False, (
                "failed to read the domain registration expiry over RDAP (%s): %s"
                % (cfg.DOMAIN_RDAP_URL, e)
            )
        probe["ok"] = True
    return True, "domain registration expires in %.0f days (read %.0fh ago)" % (
        days_remaining(probe, now),
        (now - probe["ts"]) / 3600,
    )


def check_domain_registration(cfg: Config, src: Sources) -> tuple[bool, str]:
    return domain_registration(cfg, src)
