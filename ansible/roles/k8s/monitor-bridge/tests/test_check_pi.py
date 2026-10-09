"""daniel-pi: resource pressure, and the ports arm that catches a detached container.

`pi_pressure` reads load, memory and disk headroom off the Pi's own node-exporter series in
Prometheus, plus whether the SD card's root or firmware
partition has remounted read-only, the one Pi failure every other arm reads green through. The
ports arm is separate: a Pi reboot leaves
containers `Up (healthy)` with an empty network, so the published port is dead while every
container-level signal reads fine. It leads the message when it fires.
"""

from dataclasses import replace

import pytest

import checks.host_edge
from _fake_sources import FakeSources

MB = 1048576


DISK_OK = {"/dev/mmcblk0p2": 3.3, "/dev/mmcblk0p1": 12.0}
RW = {"/": 0.0, "/boot/firmware": 0.0}


def test_pi_pressure_ok():
    ok, msg = checks.host_edge.pi_pressure(0.2, 150 * MB, DISK_OK, RW, 1.5, 50, 90)
    assert ok
    assert "0.20/core" in msg and "150MB" in msg and "disk /dev/mmcblk0p1 12%" in msg


def test_pi_pressure_high_load_alerts():
    ok, msg = checks.host_edge.pi_pressure(1.8, 150 * MB, DISK_OK, RW, 1.5, 50, 90)
    assert not ok
    assert "load5 1.80/core" in msg


def test_pi_pressure_low_mem_alerts():
    ok, msg = checks.host_edge.pi_pressure(0.2, 30 * MB, DISK_OK, RW, 1.5, 50, 90)
    assert not ok
    assert "mem available 30MB" in msg


def test_pi_pressure_full_disk_alerts_naming_device():
    disk = {"/dev/mmcblk0p2": 95.0, "/dev/mmcblk0p1": 12.0}
    ok, msg = checks.host_edge.pi_pressure(0.2, 150 * MB, disk, RW, 1.5, 50, 90)
    assert not ok
    assert "disk /dev/mmcblk0p2 95%" in msg
    assert "mmcblk0p1" not in msg


def test_pi_pressure_both_breaches_named():
    ok, msg = checks.host_edge.pi_pressure(1.8, 30 * MB, DISK_OK, RW, 1.5, 50, 90)
    assert not ok
    assert "load5" in msg and "mem available" in msg


def test_pi_pressure_at_threshold_is_ok():
    ok, _ = checks.host_edge.pi_pressure(
        1.5, 50 * MB, {"/dev/mmcblk0p2": 90.0}, RW, 1.5, 50, 90
    )
    assert ok


def test_pi_pressure_rw_mounts_is_clean():
    ok, msg = checks.host_edge.pi_pressure(0.2, 150 * MB, DISK_OK, RW, 1.5, 50, 90)
    assert ok
    # The Kuma message is the evidence the arm ran, not only that it did not fire.
    assert "rw /, /boot/firmware" in msg


def test_pi_pressure_readonly_root_is_flagged():
    readonly = {"/": 1.0, "/boot/firmware": 0.0}
    ok, msg = checks.host_edge.pi_pressure(
        0.2, 150 * MB, DISK_OK, readonly, 1.5, 50, 90
    )
    assert not ok
    # Leads the message: the fill % freezes at a healthy value, so it must not come first.
    assert msg.startswith("READ-ONLY / (")
    assert "/boot/firmware" not in msg


@pytest.mark.parametrize(
    ("load", "avail", "disk", "readonly"),
    [
        pytest.param(None, 150 * MB, DISK_OK, RW, id="no_load_series"),
        pytest.param(0.2, None, DISK_OK, RW, id="no_mem_series"),
        # A blind filesystem collector must surface, not silently pass — the same principle
        # as the other checks' unreachable-source handling.
        pytest.param(0.2, 150 * MB, {}, RW, id="no_fs_series"),
        # A renamed node_filesystem_readonly would otherwise read as "nothing read-only".
        pytest.param(0.2, 150 * MB, DISK_OK, {}, id="no_readonly_series"),
    ],
)
def test_pi_pressure_missing_series_alerts(load, avail, disk, readonly):
    ok, msg = checks.host_edge.pi_pressure(load, avail, disk, readonly, 1.5, 50, 90)
    assert not ok
    assert "missing" in msg


# ── check_pi_pressure: the four Prometheus reads, keyed by origin ──


LOAD5 = "node_load5"
CORES = "count(node_cpu_seconds_total"
AVAIL = "node_memory_MemAvailable_bytes"


def _prom(load5=0.8, cores=4.0, avail=150 * MB, disk=None, readonly=None):
    """A `FakeSources` answering prom_scalar/prom_vector by query text.

    `src.calls` records every query issued; `_queries` lists their text in order.
    """
    scalars = {LOAD5: load5, CORES: cores, AVAIL: avail}

    def prom_scalar(q):
        for key, value in scalars.items():
            if q.startswith(key):
                return value
        raise AssertionError("unexpected scalar query %r" % q)

    def prom_vector(q):
        if q.startswith("node_filesystem_readonly"):
            rows = RW if readonly is None else readonly
            return [({"mountpoint": mp}, ro) for mp, ro in rows.items()]
        assert "node_filesystem_avail_bytes" in q
        rows = DISK_OK if disk is None else disk
        return [({"device": dev}, pct) for dev, pct in rows.items()]

    return FakeSources(prom_scalar=prom_scalar, prom_vector=prom_vector)


def _queries(src):
    """The text of every query a check sent through `src`, scalar and vector, in order."""
    return [args[0] for _method, args in src.calls]


def _pressure_cfg(cfg):
    """`cfg` watching the Pi, with the port arm off so a pressure test opens no socket.

    The rendered env carries the Pi's real published ports, which the arm would TCP-probe on
    the LAN. The port arm's own tests below state their ports and stub `tcp_open`.
    """
    return replace(cfg, PI_ORIGIN="daniel-pi", PI_PUBLISHED_PORTS=())


def test_pi_check_disabled_without_origin(cfg):
    # An empty PI_ORIGIN disables monitoring: never a false page, and no query is issued.
    src = _prom()
    ok, msg = checks.host_edge.check_pi_pressure(replace(cfg, PI_ORIGIN=""), src)
    assert ok
    assert "disabled" in msg.lower()
    assert _queries(src) == []


def test_pi_check_selects_every_series_by_the_pi_origin(cfg):
    # The QUERY is what keeps this a Pi check: an unpinned node_load5 returns the first host
    # Prometheus happens to list, and a verdict test passes either way.
    cfg = _pressure_cfg(cfg)
    src = _prom()
    checks.host_edge.check_pi_pressure(cfg, src)
    seen = _queries(src)
    assert len(seen) == 5
    assert all('origin="daniel-pi"' in q for q in seen), seen
    assert any('fstype!="tmpfs"' in q for q in seen), "log2ram's tmpfs fills by design"
    # Anchored, so the root's second mount /var/hdd.log and log2ram's tmpfs stay out.
    assert any('mountpoint=~"/|/boot/firmware"' in q for q in seen), seen


def test_pi_check_down_on_pressure(cfg):
    cfg = _pressure_cfg(cfg)
    src = _prom(load5=7.2)
    ok, msg = checks.host_edge.check_pi_pressure(cfg, src)
    assert not ok
    assert "load5 1.80/core" in msg


def test_pi_check_up_when_quiet(cfg):
    cfg = _pressure_cfg(cfg)
    src = _prom()
    ok, msg = checks.host_edge.check_pi_pressure(cfg, src)
    assert ok
    assert "load5 0.20/core" in msg


def test_pi_check_readonly_root_pages_on_the_first_cycle(cfg):
    # No grace, like kubelet_plugin_readonly: a read-only remount does not self-heal, so two
    # consecutive cycles must both page.
    cfg = _pressure_cfg(cfg)
    src = _prom(readonly={"/": 1.0, "/boot/firmware": 0.0})
    ok1, msg = checks.host_edge.check_pi_pressure(cfg, src)
    ok2, _ = checks.host_edge.check_pi_pressure(cfg, src)
    assert not ok1 and not ok2
    assert msg.startswith("READ-ONLY /")


def test_pi_check_pages_when_the_pi_stops_reporting(cfg):
    # Prometheus answers, the node-pi series are gone: a Pi nothing is watching. The
    # `prometheus` gate and EXPORTER_DEPENDENT are what keep this from double-paging.
    cfg = _pressure_cfg(cfg)
    src = _prom(load5=None, cores=None, avail=None, disk={}, readonly={})
    ok, msg = checks.host_edge.check_pi_pressure(cfg, src)
    assert not ok
    assert "missing" in msg


def test_pi_check_zero_cores_alerts_not_divides(cfg):
    cfg = _pressure_cfg(cfg)
    src = _prom(cores=0.0)
    ok, msg = checks.host_edge.check_pi_pressure(cfg, src)
    assert not ok
    assert "missing" in msg


# ── pi_ports_verdict (a Pi reboot leaves containers up with no network) ──


PUBLISHED = (
    ("wg-easy", 51821),
    ("alloy", 12345),
)


def test_every_port_listening_is_clean():
    ok, msg = checks.host_edge.pi_ports_verdict([], 2)
    assert ok
    assert msg == "2 pi port(s) listening"


def test_dead_port_is_named_with_the_recreate_hint():
    ok, msg = checks.host_edge.pi_ports_verdict([("alloy", 12345)], 2)
    assert not ok
    assert msg.startswith("1 pi port(s) not listening: alloy:12345")
    assert "RECREATE" in msg


def test_non_publishing_containers_are_never_named():
    ok, msg = checks.host_edge.pi_ports_verdict([], 2)
    assert ok
    for name in ("docker-proxy", "autoheal", "docker-proxy-lifecycle"):
        assert name not in msg


def _arm_ports(cfg, open_ports, streak=0, host="10.0.0.139"):
    cfg = replace(
        cfg,
        PI_ORIGIN="daniel-pi",
        PI_HOST=host,
        PI_PUBLISHED_PORTS=PUBLISHED,
        PI_PORTS_CONSECUTIVE=2,
    )
    probed = []

    def tcp_open(host, port, timeout):
        probed.append((host, port))
        return port in open_ports

    src = _prom()
    src.state.down_streaks["pi_ports"] = streak
    return cfg, probed, tcp_open, src


def test_pi_check_arm_disabled_when_no_ports_configured(cfg):
    cfg, probed, tcp_open, src = _arm_ports(cfg, set())
    cfg = replace(cfg, PI_PUBLISHED_PORTS=())
    ok, msg = checks.host_edge.check_pi_pressure(cfg, src, tcp_open=tcp_open)
    assert ok
    assert "listening" not in msg
    assert probed == []


def test_pi_check_arm_skipped_without_a_host(cfg):
    # An origin label is not an address: with no PI_HOST there is nothing to connect to,
    # and the pressure arms still report.
    cfg, probed, tcp_open, src = _arm_ports(cfg, set(), host="")
    ok, msg = checks.host_edge.check_pi_pressure(cfg, src, tcp_open=tcp_open)
    assert ok
    assert "load5" in msg and "listening" not in msg
    assert probed == []


def test_pi_check_probes_every_published_port_on_the_pi_host(cfg):
    all_ports = {p for _, p in PUBLISHED}
    cfg, probed, tcp_open, src = _arm_ports(cfg, all_ports)
    ok, msg = checks.host_edge.check_pi_pressure(cfg, src, tcp_open=tcp_open)
    assert ok
    assert sorted(probed) == sorted(("10.0.0.139", p) for p in all_ports)
    assert "2 pi port(s) listening" in msg


def test_pi_check_dead_port_leads_the_message(cfg):
    all_ports = {p for _, p in PUBLISHED}
    cfg, _probed, tcp_open, src = _arm_ports(cfg, all_ports - {12345}, streak=1)
    ok, msg = checks.host_edge.check_pi_pressure(cfg, src, tcp_open=tcp_open)
    assert not ok
    # The pager must see the fault, not the load figure it is not about.
    assert msg.startswith("1 pi port(s) not listening: alloy:12345")
    assert "load5" in msg


def test_pi_check_holds_the_first_dead_cycle_for_the_deploy_window(cfg):
    # A Pi deploy recreates containers, so one cycle of dead ports is expected.
    all_ports = {p for _, p in PUBLISHED}
    cfg, _probed, tcp_open, src = _arm_ports(cfg, all_ports - {12345}, streak=0)
    ok, msg = checks.host_edge.check_pi_pressure(cfg, src, tcp_open=tcp_open)
    assert ok
    assert "down streak 1/2" in msg


def test_pi_check_resets_the_streak_once_ports_return(cfg):
    all_ports = {p for _, p in PUBLISHED}
    cfg, _probed, tcp_open, src = _arm_ports(cfg, all_ports, streak=1)
    ok, _ = checks.host_edge.check_pi_pressure(cfg, src, tcp_open=tcp_open)
    assert ok
    assert src.state.down_streaks["pi_ports"] == 0
