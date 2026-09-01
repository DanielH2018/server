"""daniel-pi: resource pressure, and the ports arm that catches a detached container.

`pi_pressure` reads load, memory and disk headroom off the Pi's glances API. The ports arm is
separate: a Pi reboot leaves containers `Up (healthy)` with an empty network, so the published
port is dead while every container-level signal reads fine. It leads the message when it fires,
and a failed attribution fetch downgrades the diagnosis rather than the verdict.
"""

import pytest

import check

MB = 1048576


LOAD_OK = {"min5": 0.8, "cpucore": 4}


MEM_OK = {"available": 150 * MB}


# Glances in its container sees its own bind-mounts (/etc/resolv.conf etc.), all backed
# by the SD card device with the HOST fs usage percent — so entries are keyed by
# device_name, and one device appears many times.
FS_OK = [
    {"device_name": "/dev/mmcblk0p2", "mnt_point": "/etc/resolv.conf", "percent": 3.3},
    {"device_name": "/dev/mmcblk0p2", "mnt_point": "/etc/hostname", "percent": 3.3},
]


def test_pi_pressure_ok():
    ok, msg = check.pi_pressure(LOAD_OK, MEM_OK, FS_OK, 1.5, 50, 90)
    assert ok
    assert "0.20/core" in msg and "150MB" in msg and "disk 3%" in msg


def test_pi_pressure_high_load_alerts():
    # 2026-06-11 fwupd incident signature: load5 ~7.2 on 4 cores while every
    # container healthcheck timed out (mem available still ~150MB at that instant)
    ok, msg = check.pi_pressure({"min5": 7.2, "cpucore": 4}, MEM_OK, FS_OK, 1.5, 50, 90)
    assert not ok
    assert "load5 1.80/core" in msg


def test_pi_pressure_low_mem_alerts():
    ok, msg = check.pi_pressure(
        {"min5": 0.4, "cpucore": 4}, {"available": 13 * MB}, FS_OK, 1.5, 50, 90
    )
    assert not ok
    assert "13MB" in msg


def test_pi_pressure_full_disk_alerts_naming_device():
    fs = [
        {"device_name": "/dev/mmcblk0p2", "mnt_point": "/etc/hostname", "percent": 94.0}
    ]
    ok, msg = check.pi_pressure(LOAD_OK, MEM_OK, fs, 1.5, 50, 90)
    assert not ok
    assert "/dev/mmcblk0p2" in msg and "94" in msg


def test_pi_pressure_duplicate_device_entries_alert_once():
    fs = [
        {
            "device_name": "/dev/mmcblk0p2",
            "mnt_point": "/etc/resolv.conf",
            "percent": 94.0,
        },
        {
            "device_name": "/dev/mmcblk0p2",
            "mnt_point": "/etc/hostname",
            "percent": 94.0,
        },
    ]
    ok, msg = check.pi_pressure(LOAD_OK, MEM_OK, fs, 1.5, 50, 90)
    assert not ok
    assert msg.count("/dev/mmcblk0p2") == 1


def test_pi_pressure_both_breaches_named():
    ok, msg = check.pi_pressure(
        {"min5": 8.0, "cpucore": 4}, {"available": 10 * MB}, FS_OK, 1.5, 50, 90
    )
    assert not ok
    assert "load5" in msg and "available" in msg


def test_pi_pressure_at_threshold_is_ok():
    # strictly greater / strictly less, like the other checks' threshold semantics
    fs = [{"device_name": "/dev/mmcblk0p2", "mnt_point": "/", "percent": 90.0}]
    ok, _ = check.pi_pressure(
        {"min5": 6.0, "cpucore": 4}, {"available": 50 * MB}, fs, 1.5, 50, 90
    )
    assert ok


@pytest.mark.parametrize(
    ("load", "fs"),
    [
        pytest.param({}, FS_OK, id="missing_fields_alert"),
        # a glances fs-plugin regression must surface, not silently pass (same principle
        # as the load/mem missing-field handling)
        pytest.param(LOAD_OK, [], id="empty_fs_alerts"),
        pytest.param(
            {"min5": 1.0, "cpucore": 0}, FS_OK, id="zero_cores_alerts_not_divides"
        ),
    ],
)
def test_pi_pressure_missing_input_alerts(load, fs):
    ok, msg = check.pi_pressure(load, MEM_OK, fs, 1.5, 50, 90)
    assert not ok
    assert "missing" in msg


def test_pi_check_disabled_without_url():
    # PI_GLANCES_URL defaults to "" in tests -> monitoring disabled, never a false page
    ok, msg = check.check_pi_pressure()
    assert ok
    assert "disabled" in msg.lower()


def test_pi_check_down_on_pressure(monkeypatch, seq):
    monkeypatch.setattr(check, "PI_GLANCES_URL", "http://pi:61208")
    monkeypatch.setattr(
        check, "_get_json", seq({"min5": 7.2, "cpucore": 4}, MEM_OK, FS_OK)
    )
    ok, msg = check.check_pi_pressure()
    assert not ok
    assert "load5" in msg


def test_pi_check_up_when_quiet(monkeypatch, seq):
    monkeypatch.setattr(check, "PI_GLANCES_URL", "http://pi:61208")
    monkeypatch.setattr(
        check, "_get_json", seq({"min5": 0.4, "cpucore": 4}, MEM_OK, FS_OK)
    )
    ok, _ = check.check_pi_pressure()
    assert ok


# ── pi_ports_verdict (a Pi reboot leaves containers up with no network) ──


PUBLISHED = (
    ("wg-easy", 51821),
    ("glances", 61208),
    ("dozzle", 8080),
    ("node-exporter", 9100),
    ("promtail", 9080),
)


def _container(name, ports, status="healthy"):
    return {"name": name, "status": status, "ports": ports}


# The live payload, 2026-08-27. wg-easy publishes both TCP and UDP; glances publishes one
# mapping alongside a merely-exposed 61209/tcp, which is why the match is on "->".
CONTAINERS_OK = [
    _container("glances", "61208->61208/tcp,61209/tcp"),
    _container("promtail", "9080->9080/tcp"),
    _container("dozzle", "8080->8080/tcp"),
    _container("node-exporter", "9100->9100/tcp"),
    _container("wg-easy", "51821->51821/tcp,51822->51822/udp"),
    # The three that publish nothing forever, present so a rule that flagged them would fail
    # here rather than page for a day.
    _container("docker-proxy", ""),
    _container("autoheal", ""),
    _container("docker-proxy-lifecycle", ""),
]


def _without(name):
    return [c for c in CONTAINERS_OK if c["name"] != name]


def _with(name, **changes):
    return [dict(c, **changes) if c["name"] == name else c for c in CONTAINERS_OK]


def test_every_port_listening_is_clean():
    ok, msg = check.pi_ports_verdict([], len(PUBLISHED))
    assert ok
    assert "5 pi port(s) listening" in msg


def test_dead_port_on_an_up_container_reads_as_detached():
    # The reboot signature: up, healthy, healthcheck passing on loopback, no mappings.
    ok, msg = check.pi_ports_verdict([("dozzle", 8080)], 5, _with("dozzle", ports=""))
    assert not ok
    assert "dozzle:8080" in msg and "RECREATE" in msg


def test_exposed_but_unpublished_port_reads_as_detached():
    # An exposed port carries no "->" and is not a published mapping — the whole basis of the
    # diagnosis, so a container showing only exposed ports must not read as publishing.
    ok, msg = check.pi_ports_verdict(
        [("promtail", 9080)], 5, _with("promtail", ports="9080/tcp")
    )
    assert not ok
    assert "RECREATE" in msg


def test_dead_port_on_a_stopped_container_is_not_called_detached():
    ok, msg = check.pi_ports_verdict(
        [("dozzle", 8080)], 5, _with("dozzle", ports="", status="exited")
    )
    assert not ok
    assert "dozzle:8080 (exited)" in msg
    assert "RECREATE" not in msg


def test_dead_port_on_an_absent_container_says_so():
    ok, msg = check.pi_ports_verdict([("wg-easy", 51821)], 5, _without("wg-easy"))
    assert not ok
    assert "container absent" in msg
    assert "RECREATE" not in msg


def test_dead_port_while_docker_says_publishing_is_a_separate_diagnosis():
    # Mapping present, port unreachable: a bind-address or firewall fault, not a detached
    # container — and telling someone to recreate would be the wrong remediation.
    ok, msg = check.pi_ports_verdict([("dozzle", 8080)], 5, CONTAINERS_OK)
    assert not ok
    assert "publishing but unreachable" in msg
    assert "RECREATE" not in msg


def test_failed_attribution_fetch_downgrades_the_diagnosis_not_the_verdict():
    # containers_json=None is the fetch having failed. The port is still dead, so the arm
    # must still be down — failing open here is what would make it inert.
    ok, msg = check.pi_ports_verdict([("dozzle", 8080)], 5, None)
    assert not ok
    assert "dozzle:8080 (cause unknown)" in msg


def test_non_publishing_containers_are_never_named():
    ok, msg = check.pi_ports_verdict([], 5)
    assert ok
    for name in ("docker-proxy", "autoheal", "docker-proxy-lifecycle"):
        assert name not in msg


def test_pi_check_arm_disabled_when_no_ports_configured(monkeypatch, seq):
    monkeypatch.setattr(check, "PI_GLANCES_URL", "http://pi:61208")
    monkeypatch.setattr(check, "PI_PUBLISHED_PORTS", ())
    monkeypatch.setattr(
        check, "_get_json", seq({"min5": 0.4, "cpucore": 4}, MEM_OK, FS_OK)
    )
    ok, msg = check.check_pi_pressure()
    assert ok
    assert "listening" not in msg


def _arm_ports(monkeypatch, open_ports, containers=None, streak=0):
    monkeypatch.setattr(check, "PI_GLANCES_URL", "http://10.0.0.139:61208")
    monkeypatch.setattr(check, "PI_PUBLISHED_PORTS", PUBLISHED)
    monkeypatch.setattr(check, "PI_PORTS_CONSECUTIVE", 2)
    check._down_streaks["pi_ports"] = streak
    monkeypatch.setattr(check, "_tcp_open", lambda h, p, t: p in open_ports)
    fetched = []

    def _get(url, **kwargs):
        if url.endswith("/containers"):
            fetched.append(url)
            if containers is None:
                raise OSError("docker plugin unavailable")
            return containers
        return {
            "/api/4/load": {"min5": 0.4, "cpucore": 4},
            "/api/4/mem": MEM_OK,
            "/api/4/fs": FS_OK,
        }[url[len("http://10.0.0.139:61208") :]]

    monkeypatch.setattr(check, "_get_json", _get)
    return fetched


def test_pi_check_does_not_fetch_containers_when_every_port_is_up(monkeypatch):
    # The whole point of the port-first design: /api/4/containers costs seconds on the Pi and
    # has been measured timing out, so the happy path must never touch it.
    all_ports = {p for _, p in PUBLISHED}
    fetched = _arm_ports(monkeypatch, all_ports)
    ok, msg = check.check_pi_pressure()
    assert ok
    assert fetched == []
    assert "5 pi port(s) listening" in msg


def test_pi_check_detached_leads_the_message(monkeypatch):
    all_ports = {p for _, p in PUBLISHED}
    fetched = _arm_ports(
        monkeypatch, all_ports - {8080}, _with("dozzle", ports=""), streak=1
    )
    ok, msg = check.check_pi_pressure()
    assert not ok
    assert fetched, "a dead port must trigger the attribution fetch"
    # The pager must see the fault, not the load figure it is not about.
    assert msg.startswith("1 pi container(s) up with no published ports")


def test_pi_check_holds_the_first_dead_cycle_for_the_deploy_window(monkeypatch):
    # A Pi deploy recreates containers, so one cycle of dead ports is expected.
    all_ports = {p for _, p in PUBLISHED}
    _arm_ports(monkeypatch, all_ports - {8080}, _with("dozzle", ports=""), streak=0)
    ok, msg = check.check_pi_pressure()
    assert ok
    assert "down streak 1/2" in msg


def test_pi_check_reports_dead_port_when_attribution_fetch_fails(monkeypatch):
    all_ports = {p for _, p in PUBLISHED}
    _arm_ports(monkeypatch, all_ports - {8080}, None, streak=1)
    ok, msg = check.check_pi_pressure()
    assert not ok
    assert "dozzle:8080 (cause unknown)" in msg


def test_pi_check_resets_the_streak_once_ports_return(monkeypatch):
    all_ports = {p for _, p in PUBLISHED}
    _arm_ports(monkeypatch, all_ports, streak=1)
    ok, _ = check.check_pi_pressure()
    assert ok
    assert check._down_streaks["pi_ports"] == 0


# check_longhorn_volumes — replica redundancy on the storage layer
