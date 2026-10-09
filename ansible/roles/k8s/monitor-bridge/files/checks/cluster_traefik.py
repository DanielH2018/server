"""Routers serving 421 continuously, the Traefik failure no per-service check can see.

Its own module for the reason `cluster_etcd.py` is one: `checks/cluster.py` sits at the
600-line cap the module-length ratchet enforces, beside the three Traefik checks this one
complements. Config as `cfg.X`, the query through the `src` argument (`bridge.sources.Sources`), the
streak in `src.state` (`bridge.streaks.State`), the same layering as its neighbours.
"""

from bridge.config import Config
from bridge.sources import Sources
import bridge.streaks


def check_traefik_421(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Routers serving 421 continuously: a client wedged on a mis-pinned connection.

    SNICheck records the TLS-options name once per connection, at the handshake, and answers
    421 to every later request on it whose router declares a different name. A long-lived
    client that connects while Traefik is still reconciling routes after a reboot therefore
    gets 421 for the life of the connection (traefik's CLAUDE.md has the mechanism). The
    request never reaches a backend, so check_traefik_5xx and check_traefik_latency have no
    `traefik_service_*` series to read, and check_traefik_404_flood counts a different code.
    Three wedges went unseen in one week: authelia for 14.5h from 2026-09-20 23:10, the Pi's
    Alloy for 4h15m (#2747) and the apiserver's OIDC fetches for 5h30m (#2749).

    Per ROUTER, and an absolute rate rather than a share behind TRAEFIK_MIN_RPS. 421 has no
    legitimate sustained source here, and the wedged clients are pollers at 0.10-0.18 rps
    that a share would dilute on a busy router. TRAEFIK_421_RPS carries the measured
    populations. The streak is the hysteresis a low threshold needs: a one-shot handshake
    mismatch stays inside a [5m] rate for at most two evaluations, so it cannot reach
    TRAEFIK_421_CONSECUTIVE.

    An empty vector is a genuine zero, not an unknown: Traefik emits no 421 series until it
    serves one. An unscraped Traefik also reads empty here, which is Scrape Targets' page.
    """
    key = "traefik_421"
    rates = sorted(
        (
            (m.get("router", "?"), v)
            for m, v in src.prom_vector(
                'sum by (router)(rate(traefik_router_requests_total{code="421"}[5m]))',
            )
            if v > cfg.TRAEFIK_421_RPS
        ),
        key=lambda rv: -rv[1],
    )
    if not rates:
        src.state.down_streaks[key] = 0
        return True, "421 ok: no router above %.2f rps" % cfg.TRAEFIK_421_RPS
    msg = (
        "%d router(s) serving 421 above %.2f rps: %s — a client is wedged on a connection "
        "SNICheck pinned wrong; restart that client so it redials (see #2757)"
        % (
            len(rates),
            cfg.TRAEFIK_421_RPS,
            ", ".join("%s %.2f rps" % rv for rv in rates),
        )
    )
    src.state.down_streaks[key], ok, msg = bridge.streaks.down_streak(
        src.state.down_streaks.get(key, 0),
        cfg.TRAEFIK_421_CONSECUTIVE,
        msg,
        "a one-shot handshake mismatch clears within two cycles",
    )
    return ok, msg
