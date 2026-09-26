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
from collections.abc import Callable

from bridge.config import Config
import bridge.net
import bridge.streaks
from verdicts.host import (
    pi_ports_verdict,
    pi_pressure,
    speedtest_verdict,
)

# The SD card's root and its firmware partition. Anchored by PromQL, so `/var/hdd.log` (the
# root's second mount) and log2ram's tmpfs are out. Both read 0 across 27 days of retention
# measured 2026-09-26, and fstab mounts both `defaults`, so the arm starts green.
PI_READONLY_MOUNTS = "/|/boot/firmware"


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

    # DECIDED: TCP connect is the whole signal. Until 2026-09-18 a dead port was attributed
    # to its container's Docker state through glances' /api/4/containers, fetched only after
    # the connect failed — measured 2026-08-27 that endpoint took 4.43s and then TIMED OUT at
    # the 10s HTTP_TIMEOUT on the very next call, so polling it every cycle would have left
    # the arm failing open most of the time. glances retired (#2004) and nothing the cluster
    # can reach serves that view (docker-proxy publishes no port), so the verdict names the
    # port and both causes and the operator reads `docker ps` on the Pi. The verdict never
    # depended on the attribution, so the arm loses a diagnosis and keeps its page.
    # DECIDED: the message leads with the dead ports when the arm fires, because
    # "pi_pressure DOWN" otherwise pages someone to look at load and memory when the fault is
    # neither. Same shape as with_ha_ban putting the ban first.
    # DECIDED: a down_streak, unlike with_ha_ban's arm. A Pi deploy recreates containers, so
    # their ports are legitimately closed for a few seconds and a single cycle can read dead.
    # A detached container persists until someone recreates it, so it survives the grace.
    # DECIDED: this arm rides inside a PROM_DEPENDENT check since 2026-09-18, so a Prometheus
    # outage or a dead Pi node_exporter skips the port probes with the pressure arms. Both
    # cases already page (Prometheus Reachable / Scrape Targets), and the Kuma HTTP monitor
    # on wg-easy still watches the one Pi port a person uses. A monitor of its own would
    # cost a push token in SOPS, the reason the arm was folded in here to begin with.
    """
    if not cfg.PI_PUBLISHED_PORTS or not cfg.PI_HOST:
        return ok, msg
    dead = [
        (name, port)
        for name, port in cfg.PI_PUBLISHED_PORTS
        if not tcp_open(cfg.PI_HOST, port, cfg.PI_PORT_TIMEOUT)
    ]
    arm_ok, arm_msg = pi_ports_verdict(dead, len(cfg.PI_PUBLISHED_PORTS))
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

    Reads the Pi's node-exporter series off Prometheus, selected by `origin=PI_ORIGIN` (the
    `node-pi` scrape job). Empty PI_ORIGIN -> disabled (stays up), like check_n8n without an
    API key. An unreachable Prometheus raises, which the `prometheus` gate suppresses before
    it reaches here; a series that is absent while Prometheus answers pages, because a Pi
    whose exporter stopped reporting is a Pi nothing is watching.

    Filesystems are keyed by block device rather than mountpoint — the SD card is mounted
    twice (`/` and `/var/hdd.log`) and one full device is one problem. tmpfs is excluded:
    log2ram's 128 MiB `/var/log` fills and flushes by design.

    The read-only arm is keyed by mountpoint instead, over PI_READONLY_MOUNTS, and reads the
    value unfiltered so the healthy message can name the mounts it saw rw.
    """
    if not cfg.PI_ORIGIN:
        return True, "pi monitoring disabled (no PI_ORIGIN)"
    sel = 'origin="%s"' % cfg.PI_ORIGIN
    load5 = bridge.net.prom_scalar(cfg, "node_load5{%s}" % sel)
    cores = bridge.net.prom_scalar(
        cfg, 'count(node_cpu_seconds_total{%s,mode="idle"})' % sel
    )
    avail = bridge.net.prom_scalar(cfg, "node_memory_MemAvailable_bytes{%s}" % sel)
    fs_sel = '%s,fstype!="tmpfs"' % sel
    disk = bridge.net.prom_vector(
        cfg,
        "max by (device) (100 * (1 - node_filesystem_avail_bytes{%s}"
        " / node_filesystem_size_bytes{%s}))" % (fs_sel, fs_sel),
    )
    readonly = bridge.net.prom_vector(
        cfg,
        'node_filesystem_readonly{%s,mountpoint=~"%s"}' % (sel, PI_READONLY_MOUNTS),
    )
    per_core = load5 / cores if load5 is not None and cores else None
    ok, msg = pi_pressure(
        per_core,
        avail,
        {labels.get("device", "?"): pct for labels, pct in disk},
        {labels.get("mountpoint", "?"): ro for labels, ro in readonly},
        cfg.PI_LOAD_MAX,
        cfg.PI_MEM_MIN_MB,
        cfg.PI_DISK_MAX_PCT,
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
    # DECIDED: the check cannot tell a failing Ookla run from a stopped scheduler, and this
    # repo accepts that (#1603). speedtest-tracker writes NO row for a failed run — that is the
    # LinuxServer image's own behaviour, decided between the Ookla CLI exiting non-zero and the
    # results table, and the role has no hook in between. Measured 2026-09-10 against the live
    # app: 25 rows, ids 825-849, every one `status: completed`, so speedtest_verdict's
    # `status != "completed"` branch is reachable by test and has never been taken by data.
    # The two rejected alternatives are a LogQL check for the per-tick log line (Loki does not
    # retain the pod's logs across the restart that is exactly when it is wanted) and replacing
    # the internal scheduler with a k8s CronJob (rejected at the `# DECIDED:` on the
    # SPEEDTEST_SCHEDULE line — it moves the transcript, not the reliability). What mitigates it
    # is the staleness message naming both hypotheses and asserting neither, in
    # verdicts/host.py's speedtest_verdict.
    return speedtest_verdict(
        rows[0] if rows else None,
        cfg.SPEEDTEST_DOWNLOAD_MIN_MBPS,
        cfg.SPEEDTEST_MAX_AGE_H,
    )
