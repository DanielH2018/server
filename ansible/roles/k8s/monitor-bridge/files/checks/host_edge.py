"""Edge checks for monitor-bridge: daniel-pi and the internet link.

Covers the Pi's resource pressure with the published-port arm folded into it, and
speedtest-tracker's newest result row.

Split out of `checks/host.py`, which keeps disk, certificate expiry and memory. Reads config as
`cfg.X`, the fetch layer as `bridge.net.X` and the shared streak counter as `bridge.streaks.X`,
so the tests' patches on those modules reach it; the verdicts it from-imports from verdicts.host
are patched on THIS module, where they are bound. The TCP prober is an ARGUMENT rather than a
module global a test patches — `check_pi_pressure` and `with_pi_ports` both take `tcp_open`,
defaulting to `_tcp_open`, so a test injects a fake port map by calling them. Rule and
enforcement: bridge/config.py's header.
"""

import socket
import urllib.parse
from collections.abc import Callable

from bridge.config import Config
import bridge.net
import bridge.streaks
from verdicts.host import (
    pi_ports_verdict,
    pi_pressure,
    speedtest_verdict,
)


def _tcp_open(host: str, port: int, timeout: float) -> bool:
    """True when something accepts a TCP connection on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def with_pi_ports(
    cfg: Config,
    ok: bool,
    msg: str,
    tcp_open: Callable[[str, int, float], bool] = _tcp_open,
) -> tuple[bool, str]:
    """Fold the published-port arm into the Pi verdict, a dead port winning the message.

    Folded into this monitor rather than given its own for the reason recorded at with_ha_ban:
    a new Kuma monitor costs a new push token in SOPS. This monitor already owns "the Pi is
    unhealthy", and a service that stopped listening is that.

    # DECIDED: TCP connect is the primary signal, glances only the attribution. Measured
    # 2026-08-27 against the live Pi: /api/4/load, /mem and /fs answer in 0.03-0.06s each,
    # while /api/4/containers took 4.43s and then TIMED OUT at the 10s HTTP_TIMEOUT on the
    # very next call. Polling it every cycle would have left the arm failing open most of the
    # time — inert behind a green monitor, which is the failure mode this arm exists to
    # catch in the first place. It is also a heavy query to run every cycle against a 456 MB
    # Zero 2 W whose pressure this same check reports.
    # DECIDED: the message leads with the container names when the arm fires, because
    # "pi_pressure DOWN" otherwise pages someone to look at load and memory when the fault is
    # neither. Same shape as with_ha_ban putting the ban first.
    # DECIDED: a down_streak, unlike with_ha_ban's arm. A Pi deploy recreates containers, so
    # their ports are legitimately closed for a few seconds and a single cycle can read dead.
    # A detached container persists until someone recreates it, so it survives the grace.
    # DECIDED: an attribution fetch that fails downgrades the DIAGNOSIS, never the verdict —
    # pi_ports_verdict renders "cause unknown" and the port is still reported dead. Failing
    # open there would reintroduce exactly the inertness the first DECIDED avoids.
    """
    if not cfg.PI_PUBLISHED_PORTS:
        return ok, msg
    host = urllib.parse.urlsplit(cfg.PI_GLANCES_URL).hostname
    if not host:
        return ok, msg
    dead = [
        (name, port)
        for name, port in cfg.PI_PUBLISHED_PORTS
        if not tcp_open(host, port, cfg.PI_PORT_TIMEOUT)
    ]
    containers = None
    if dead:
        try:
            containers = bridge.net._get_json(cfg.PI_GLANCES_URL + "/api/4/containers")
        except Exception:
            containers = None
    arm_ok, arm_msg = pi_ports_verdict(dead, len(cfg.PI_PUBLISHED_PORTS), containers)
    if arm_ok:
        bridge.streaks._down_streaks["pi_ports"] = 0
        return ok, "%s, %s" % (msg, arm_msg)
    bridge.streaks._down_streaks["pi_ports"], arm_ok, arm_msg = (
        bridge.streaks.down_streak(
            bridge.streaks._down_streaks.get("pi_ports", 0),
            cfg.PI_PORTS_CONSECUTIVE,
            arm_msg,
            "deploy grace",
        )
    )
    if arm_ok:
        return ok, "%s, %s" % (msg, arm_msg)
    return False, "%s | %s" % (arm_msg, msg)


def check_pi_pressure(
    cfg: Config, tcp_open: Callable[[str, int, float], bool] = _tcp_open
) -> tuple[bool, str]:
    """Swap-thrash / overload early warning for the memory-constrained Pi.

    Empty PI_GLANCES_URL -> disabled (stays up), like check_n8n without an API key.
    An unreachable glances raises -> the loop renders it down with the error.
    """
    if not cfg.PI_GLANCES_URL:
        return True, "pi monitoring disabled (no glances URL)"
    load = bridge.net._get_json(cfg.PI_GLANCES_URL + "/api/4/load")
    mem = bridge.net._get_json(cfg.PI_GLANCES_URL + "/api/4/mem")
    fs = bridge.net._get_json(cfg.PI_GLANCES_URL + "/api/4/fs")
    ok, msg = pi_pressure(
        load, mem, fs, cfg.PI_LOAD_MAX, cfg.PI_MEM_MIN_MB, cfg.PI_DISK_MAX_PCT
    )
    return with_pi_ports(cfg, ok, msg, tcp_open)


def check_speedtest(cfg: Config) -> tuple[bool, str]:
    """Judge speedtest-tracker's newest result row (the SPEEDTEST_* env block in bridge/config_host.py).

    Empty URL/token -> disabled (stays up), like check_ha_heartbeat.

    NO HYSTERESIS ON THE VERDICT, deliberately. The app runs every 6h and this loop every 5
    min, so a consecutive-cycle streak would re-read the IDENTICAL row up to 72 times: it would
    delay the page by N*INTERVAL and prove nothing new about the run. The FETCH failure does
    ride the streak, because the app restarting under a deploy is a genuine transient — the
    same split check_ha_heartbeat draws, for the same reason. `speedtest` is also in
    STARTUP_GRACE, which covers the post-reboot cycle where the app has not finished booting.
    """
    if not cfg.SPEEDTEST_URL or not cfg.SPEEDTEST_TOKEN:
        return True, "speedtest monitoring disabled (no URL/token)"
    try:
        # sort=-created_at, because the default order is ASCENDING and would hand back the
        # OLDEST row in the 30-day window — a stale-forever reading that looks like a verdict.
        payload = bridge.net._get_json(
            cfg.SPEEDTEST_URL + "/api/v1/results?sort=-created_at&page%5Bsize%5D=1",
            headers={
                "Authorization": "Bearer " + cfg.SPEEDTEST_TOKEN,
                "Accept": "application/json",
            },
        )
    except Exception as e:
        bridge.streaks._down_streaks["speedtest"], ok, msg = bridge.streaks.down_streak(
            bridge.streaks._down_streaks.get("speedtest", 0),
            cfg.SPEEDTEST_CONSECUTIVE,
            "speedtest API unreachable: %s" % e,
            "deploy/restart grace",
        )
        return ok, msg
    bridge.streaks._down_streaks["speedtest"] = 0
    rows = payload.get("data") or []
    return speedtest_verdict(
        rows[0] if rows else None,
        cfg.SPEEDTEST_DOWNLOAD_MIN_MBPS,
        cfg.SPEEDTEST_MAX_AGE_H,
    )
