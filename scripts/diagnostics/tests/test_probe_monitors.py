"""`probe.py kuma-drift`: what is missing, not what is down.

`monitors` answers "what is down". `kuma-drift` answers "what is missing", which `monitors`
structurally cannot — it counts the exporter's own set, so a monitor that is gone rather than
down leaves the ratio at N/N up.
"""

import probe_monitors

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
    declared = probe_monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
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
    declared = probe_monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "k3s Grafana"}
    text, code = probe_monitors.format_kuma_drift(declared, live, 86400 * 3)
    assert code == 1
    assert "WG Pi Peer Backup: declared, not live" in text


def test_kuma_drift_calls_a_push_monitor_pending_inside_its_own_interval():
    # Kuma exports a monitor only after it beats, so a restart empties every push series. A
    # monitor whose interval has not elapsed since the restart is not yet due — flagging it
    # would make this check fail after every deploy.
    declared = probe_monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"k3s Grafana"}
    text, code = probe_monitors.format_kuma_drift(declared, live, 30)
    assert code == 0
    assert "no beat due yet" in text
    assert "declared, not live" not in text


def test_kuma_drift_treats_every_type_as_pending_after_a_restart():
    # The first live run of this check reported 58 monitors missing 88 seconds into a rollout.
    # Kuma's exporter emits a monitor only after it beats, and that applies to http/port/dns
    # tiles too — restricting the pending rule to push monitors made a routine deploy look like
    # mass drift. The slack covers the exporter's and Prometheus's scrape lag on top.
    declared = probe_monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    text, code = probe_monitors.format_kuma_drift(declared, set(), 88)
    assert code == 0
    assert "k3s Grafana: no beat due yet" in text


def test_kuma_drift_fails_loud_when_the_pod_age_is_unreadable():
    # Same rule as `health`'s unreadable restart time: an unknown age must not silently excuse
    # a missing monitor, or the check reports green exactly when it cannot tell.
    declared = probe_monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    text, code = probe_monitors.format_kuma_drift(declared, {"k3s Grafana"}, None)
    assert code == 1
    assert "Root Disk: declared, not live" in text


def test_kuma_drift_reports_a_live_monitor_nobody_declared():
    # `kubectl apply` leaves orphaned objects behind, and AutoKuma's on_delete=delete only
    # removes what it still tracks — a monitor whose declaration was dropped can outlive it.
    declared = probe_monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana", "Retired Tile"}
    text, code = probe_monitors.format_kuma_drift(declared, live, 86400)
    assert code == 1
    assert "Retired Tile: live, not declared" in text


def test_kuma_drift_skips_a_monitor_whose_gate_is_genuinely_unset():
    declared = probe_monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    text, code = probe_monitors.format_kuma_drift(
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
    declared = probe_monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    # Past the monitor's own 90000s interval, so `pending` cannot absorb it — a gate-set
    # monitor inside its interval is still legitimately pending, not drift.
    text, code = probe_monitors.format_kuma_drift(
        declared, live, 86400 * 3, gate_states={"etcd_snapshot_push_token": True}
    )
    assert code == 1
    assert "Off-box etcd Snapshot: declared, not live" in text
    assert "genuinely unset" not in text


def test_kuma_drift_says_so_when_a_gate_cannot_be_read():
    """An unreadable gate and an unset one must not look alike — that equivalence is what let
    the case above stay silent. Unreadable does not fail the exit code (no age key on this
    host is a normal state), but it is named rather than swallowed."""
    declared = probe_monitors.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    text, code = probe_monitors.format_kuma_drift(
        declared, live, 86400, gate_states={"etcd_snapshot_push_token": None}
    )
    assert code == 0
    assert "could not be read" in text
    assert "genuinely unset" not in text
