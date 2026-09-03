"""`probe.py kuma-drift`: what is missing, not what is down.

`monitors` answers "what is down". `kuma-drift` answers "what is missing", which `monitors`
structurally cannot — it counts the exporter's own set, so a monitor that is gone rather than
down leaves the ratio at N/N up.
"""

from diagnostics.probe_lib import monitors

TEMPLATE_SAMPLE = """\
stringData:
  discord.json: |
    {"type": "notification", "name": "Homelab Alerts", "active": true}
  root-disk.json: |
    {"type": "push", "name": "Root Disk", "interval": 60, "push_token": "x"}
  peer-backup.json: |
    {"type": "push", "name": "WG Pi Peer Backup", "interval": 216000, "push_token": "x"}
  grafana.json: |
    {"type": "http", "name": "k3s Grafana", "url": "https://g.example", "interval": 60}
{% if etcd_snapshot_push_token | default('') %}
  etcd.json: |
    {"type": "push", "name": "Off-box etcd Snapshot", "interval": 90000, "push_token": "x"}
{% endif %}
"""


def test_parse_declared_monitors_reads_names_types_and_gating():
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    # Notifications are not monitors and never appear in monitor_status — counting them would
    # make every run report two phantom missing entries.
    assert "Homelab Alerts" not in declared
    assert declared["Root Disk"] == {
        "type": "push",
        "interval": 60,
        "gated": False,
        "gate": None,
    }
    assert declared["k3s Grafana"]["type"] == "http"
    assert declared["Off-box etcd Snapshot"]["gated"] is True
    # The variable is captured, not just the fact of being gated — that name is what lets the
    # caller resolve the secret instead of assuming it is unset.
    assert declared["Off-box etcd Snapshot"]["gate"] == "etcd_snapshot_push_token"


def test_kuma_drift_reports_a_declared_monitor_that_is_not_live():
    # The 2026-08-20 case: the tile is absent from the exporter, not down, so `monitors`
    # reported 81/81 up for a day. Long-uptime Kuma, so PENDING cannot be the explanation.
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "k3s Grafana"}
    text, code = monitors.format_kuma_drift(declared, live, 86400 * 3)
    assert code == 1
    assert "WG Pi Peer Backup: declared, not live" in text


def test_kuma_drift_calls_a_push_monitor_pending_inside_its_own_interval():
    # Kuma exports a monitor only after it beats, so a restart empties every push series. A
    # monitor whose interval has not elapsed since the restart is not yet due — flagging it
    # would make this check fail after every deploy.
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"k3s Grafana"}
    text, code = monitors.format_kuma_drift(declared, live, 30)
    assert code == 0
    assert "no beat due yet" in text
    assert "declared, not live" not in text


def test_kuma_drift_treats_every_type_as_pending_after_a_restart():
    # The first live run of this check reported 58 monitors missing 88 seconds into a rollout.
    # Kuma's exporter emits a monitor only after it beats, and that applies to http/port/dns
    # tiles too — restricting the pending rule to push monitors made a routine deploy look like
    # mass drift. The slack covers the exporter's and Prometheus's scrape lag on top.
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    text, code = monitors.format_kuma_drift(declared, set(), 88)
    assert code == 0
    assert "k3s Grafana: no beat due yet" in text


def test_kuma_drift_fails_loud_when_the_pod_age_is_unreadable():
    # Same rule as `health`'s unreadable restart time: an unknown age must not silently excuse
    # a missing monitor, or the check reports green exactly when it cannot tell.
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    text, code = monitors.format_kuma_drift(declared, {"k3s Grafana"}, None)
    assert code == 1
    assert "Root Disk: declared, not live" in text


def test_kuma_drift_reports_a_live_monitor_nobody_declared():
    # `kubectl apply` leaves orphaned objects behind, and AutoKuma's on_delete=delete only
    # removes what it still tracks — a monitor whose declaration was dropped can outlive it.
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana", "Retired Tile"}
    text, code = monitors.format_kuma_drift(declared, live, 86400)
    assert code == 1
    assert "Retired Tile: live, not declared" in text


def test_kuma_drift_skips_a_monitor_whose_gate_is_genuinely_unset():
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    text, code = monitors.format_kuma_drift(
        declared, live, 86400, gate_states={"etcd_snapshot_push_token": False}
    )
    assert code == 0
    assert "Off-box etcd Snapshot" in text
    assert "genuinely unset" in text


def test_kuma_drift_reports_drift_when_the_gate_is_set_but_the_monitor_is_absent():
    """The 2026-08-22 case, and the reason `gate` exists.

    etcd_snapshot_push_token was set (32 chars, in the rotation registry since 2026-07-04) and
    Off-box etcd Snapshot was not live — and the old check called that correctly skipped. A
    gated monitor that vanishes was invisible twice: absent from the exporter, and excused by
    the drift check written to catch exactly that.
    """
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    # Past the monitor's own 90000s interval, so `pending` cannot absorb it — a gate-set
    # monitor inside its interval is still legitimately pending, not drift.
    text, code = monitors.format_kuma_drift(
        declared, live, 86400 * 3, gate_states={"etcd_snapshot_push_token": True}
    )
    assert code == 1
    assert "Off-box etcd Snapshot: declared, not live" in text
    assert "genuinely unset" not in text


#
# kuma-drift --pi: scoping the declared/live sets to daniel-pi's own monitors. Uses the REAL
# static-monitors template rather than TEMPLATE_SAMPLE — pi_monitor_names() is a census over the
# actual file, and a fixture with an invented "-pi" key would prove nothing about it.

with open(monitors.STATIC_MONITORS_PATH) as _f:
    REAL_STATIC_MONITORS_TEXT = _f.read()


def test_pi_monitor_names_finds_at_least_the_known_daniel_pi_monitors():
    names = monitors.pi_monitor_names(REAL_STATIC_MONITORS_TEXT)
    # A frozenset a caller can name, not just a count — see CLAUDE.md's non-vacuity rule.
    assert {"Daniel Pi Host", "Daniel Pi Glances", "Daniel Pi Recovery"} <= names
    assert len(names) >= 3


def test_pi_monitor_names_excludes_a_k8s_monitor_that_merely_contains_pi():
    # "Pi-hole k8s DNS" is declared under the "pihole-k8s-dns" key — "pi" is a substring of
    # "pihole", not a hyphen-delimited token, and it runs on k3s, not daniel-pi.
    names = monitors.pi_monitor_names(REAL_STATIC_MONITORS_TEXT)
    assert "Pi-hole k8s DNS" not in names


def test_is_pi_monitor_key_requires_pi_as_its_own_token():
    assert monitors.is_pi_monitor_key("daniel-pi-host")
    assert monitors.is_pi_monitor_key("monitor-bridge-pi")
    assert not monitors.is_pi_monitor_key("pihole-k8s-dns")


def test_kuma_drift_pi_reports_a_missing_pi_monitor_without_cluster_noise():
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    pi_names = {
        "Root Disk"
    }  # stand in: only "Root Disk" is "pi-plane" for this fixture
    scoped_declared = {n: s for n, s in declared.items() if n in pi_names}
    text, code = monitors.format_kuma_drift(scoped_declared, set(), 86400 * 3)
    assert code == 1
    assert "Root Disk: declared, not live" in text
    # Scoping means a cluster-only miss (k3s Grafana, never in pi_names) must not appear.
    assert "k3s Grafana" not in text


def test_kuma_drift_says_so_when_a_gate_cannot_be_read():
    """An unreadable gate and an unset one must not look alike — that equivalence is what let
    the case above stay silent. Unreadable does not fail the exit code (no age key on this
    host is a normal state), but it is named rather than swallowed."""
    declared = monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    text, code = monitors.format_kuma_drift(
        declared, live, 86400, gate_states={"etcd_snapshot_push_token": None}
    )
    assert code == 0
    assert "could not be read" in text
    assert "genuinely unset" not in text


def test_run_kuma_drift_pi_end_to_end_reports_a_missing_pi_monitor(monkeypatch, capsys):
    """`kuma-drift --pi` through the real template and a stubbed Prometheus response.

    Live carries every DECLARED name except "Daniel Pi Recovery" and every non-Pi monitor
    besides — the fixture that proves the cluster's other ~75 monitors do not leak into a
    Pi-scoped run.
    """
    import json

    from diagnostics.probe_lib import core
    import probe

    pi_names = monitors.pi_monitor_names(REAL_STATIC_MONITORS_TEXT)
    live = (pi_names | {"k3s Grafana", "Root Disk"}) - {"Daniel Pi Recovery"}

    def fake_fetch(url, resolve=None):
        result = [
            {"metric": {"monitor_name": name}, "value": [0, "1"]} for name in live
        ]
        return json.dumps({"data": {"result": result}})

    monkeypatch.setattr(core, "fetch", fake_fetch)
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")
    monkeypatch.setattr(monitors, "kuma_pod_age_seconds", lambda: 86400 * 3)

    ns = probe._build_parser().parse_args(["kuma-drift", "--pi", "--no-secrets"])
    assert monitors.run_kuma_drift(ns) == 1
    out = capsys.readouterr().out
    assert "Daniel Pi Recovery: declared, not live" in out
    # Cluster-only monitors (declared, live, or both) must not leak into a --pi run.
    assert "k3s Grafana" not in out
    assert "Root Disk" not in out
