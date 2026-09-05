"""check_kubelet_plugin_readonly — #1243's detection arm.

A CSI global mount ext4 remounts read-only after a journal abort with Volume CRs staying
`attached healthy` throughout, so this is the only signal that sees it. No grace: unlike
Longhorn replica degradation (a node drain, self-healing) a read-only remount does not clear
on its own, so holding it through a streak only delays a real page.
"""

from dataclasses import replace

import bridge.net
import checks.storage
import gates


def test_the_check_is_prom_dependent():
    assert "kubelet_plugin_readonly" in gates.PROM_DEPENDENT, (
        "prom_vector raises on an unreachable Prometheus, and _evaluate turns that into a "
        "down — without this gate a Prometheus outage pages this monitor a second time for "
        "the one root cause the Prometheus monitor already reports"
    )


def _ro_series(mountpoint, origin="daniel-box"):
    return ({"mountpoint": mountpoint, "origin": origin}, 1.0)


def test_no_readonly_mounts_is_up(monkeypatch, cfg):
    monkeypatch.setattr(bridge.net, "prom_vector", lambda _cfg, *a, **k: [])
    ok, msg = checks.storage.check_kubelet_plugin_readonly(cfg)
    assert ok
    assert "no read-only" in msg


def test_one_readonly_mount_pages_naming_host_and_mountpoint(monkeypatch, cfg):
    mp = "/var/lib/kubelet/plugins/kubernetes.io/csi/driver.longhorn.io/abc/globalmount"
    monkeypatch.setattr(
        bridge.net, "prom_vector", lambda _cfg, *a, **k: [_ro_series(mp)]
    )
    ok, msg = checks.storage.check_kubelet_plugin_readonly(cfg)
    assert not ok
    assert "daniel-box" in msg
    assert mp in msg


def test_a_breach_gets_no_grace_and_pages_on_the_first_cycle(monkeypatch, cfg):
    # THE BUG THIS PINS: a Longhorn-style consecutive-down streak here would hold the first
    # cycle `up` and turn #1243's 50-minute silent outage into a merely-shorter one instead of
    # a one-cycle detection. Two consecutive calls with the same breach must both be `down`.
    mp = "/var/lib/kubelet/plugins/kubernetes.io/csi/driver.longhorn.io/abc/globalmount"
    monkeypatch.setattr(
        bridge.net, "prom_vector", lambda _cfg, *a, **k: [_ro_series(mp)]
    )
    ok1, _ = checks.storage.check_kubelet_plugin_readonly(cfg)
    ok2, _ = checks.storage.check_kubelet_plugin_readonly(cfg)
    assert not ok1
    assert not ok2


def test_recovery_is_immediately_up_with_no_streak_to_reset(monkeypatch, cfg):
    mp = "/var/lib/kubelet/plugins/kubernetes.io/csi/driver.longhorn.io/abc/globalmount"
    monkeypatch.setattr(
        bridge.net, "prom_vector", lambda _cfg, *a, **k: [_ro_series(mp)]
    )
    checks.storage.check_kubelet_plugin_readonly(cfg)
    monkeypatch.setattr(bridge.net, "prom_vector", lambda _cfg, *a, **k: [])
    ok, _ = checks.storage.check_kubelet_plugin_readonly(cfg)
    assert ok


def test_several_offenders_are_named_and_sorted(monkeypatch, cfg):
    vec = [
        _ro_series("/var/lib/kubelet/plugins/z", origin="daniel-server"),
        _ro_series("/var/lib/kubelet/plugins/a", origin="daniel-box"),
    ]
    monkeypatch.setattr(bridge.net, "prom_vector", lambda _cfg, *a, **k: vec)
    ok, msg = checks.storage.check_kubelet_plugin_readonly(cfg)
    assert not ok
    assert "2 CSI global mount(s)" in msg
    assert msg.index("daniel-box") < msg.index("daniel-server")


def test_the_query_scopes_to_the_plugins_subtree_and_is_not_origin_pinned(cfg):
    # This is a node_filesystem_* family: origin_sel() would pin it to `origin="daniel-server"`
    # in the deployed env (PROM_ORIGIN), which hides the exact same fault on daniel-box behind a
    # green tile — the mistake HOST_ORIGINS_MIN was added to stop for check_disk/check_mem.
    # host_metric_sel() must be what builds the selector, not origin_sel() — so the pin set
    # here (matching the deployed env, where PROM_URL == CLUSTER_PROM_URL) must NOT reach it.
    cfg = replace(cfg, PROM_ORIGIN='origin="daniel-server"')
    queries = []

    def record(_cfg, promql, *a, **k):
        queries.append(promql)
        return []

    saved = bridge.net.prom_vector
    try:
        bridge.net.prom_vector = record
        checks.storage.check_kubelet_plugin_readonly(cfg)
    finally:
        bridge.net.prom_vector = saved

    assert len(queries) == 1
    assert 'mountpoint=~"/var/lib/kubelet/plugins/.*"' in queries[0]
    assert "node_filesystem_readonly" in queries[0]
    assert "== 1" in queries[0]
    assert 'origin="daniel-server"' not in queries[0]
