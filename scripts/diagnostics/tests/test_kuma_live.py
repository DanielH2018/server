"""`kuma_live`'s census of the monitors Kuma holds, and the verdict `kuma-drift` draws from it.

The census exists for #4005: the exporter alone filed a never-created weekly tile as pending.
"""

import json

from diagnostics.probe_lib import kuma_live, monitors

LONG_INTERVAL_SAMPLE = """\
stringData:
  homelab-eval-sweep.json: |
    {"type": "push", "name": "Homelab Evals", "interval": 626400, "push_token": "x"}
"""


def test_kuma_drift_reports_a_never_created_long_interval_tile_as_missing_is_flagged():
    # #4005: a week-long tile is inside its interval for a week after every Sunday reboot,
    # so the exporter alone filed a tile AutoKuma refused as pending forever. Absent from
    # Kuma's own monitor set, it is missing whatever the pod's uptime.
    declared = monitors.parse_declared_monitors(LONG_INTERVAL_SAMPLE)
    text, code = monitors.format_kuma_drift(declared, set(), 3600, created=set())
    assert code == 1
    assert "Homelab Evals: declared, absent from Kuma's status page" in text
    assert "no beat due yet" not in text


def test_kuma_drift_keeps_a_created_long_interval_tile_pending_is_clean():
    declared = monitors.parse_declared_monitors(LONG_INTERVAL_SAMPLE)
    text, code = monitors.format_kuma_drift(
        declared, set(), 3600, created={"Homelab Evals"}
    )
    assert code == 0
    assert "Homelab Evals: no beat due yet (626400s interval)" in text


def test_kuma_drift_says_existence_is_unverified_when_the_census_is_unreadable():
    # An unreadable census must not read like a verified pending tile, nor turn every pending
    # tile into drift.
    declared = monitors.parse_declared_monitors(LONG_INTERVAL_SAMPLE)
    text, code = monitors.format_kuma_drift(
        declared, set(), 3600, created="status page returned 502"
    )
    assert code == 0
    assert "Homelab Evals: no beat due yet" in text
    assert "unverified: status page returned 502" in text


def test_parse_status_page_reads_names_from_every_group():
    body = json.dumps(
        {
            "publicGroupList": [
                {"name": "Services", "monitorList": [{"id": 1, "name": "k3s Grafana"}]},
                {"name": "Backups", "monitorList": [{"id": 2, "name": "Root Disk"}]},
            ]
        }
    )
    assert kuma_live.parse_status_page(body) == {"k3s Grafana", "Root Disk"}


def test_parse_status_page_reads_a_body_without_a_group_list_as_unreadable():
    # An error object is not an empty census: read as one, every pending tile would be
    # reported as never created.
    assert kuma_live.parse_status_page('{"ok": false}') is None
    assert kuma_live.parse_status_page("<html>502 Bad Gateway</html>") is None
