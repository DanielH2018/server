"""scrutiny's InfluxDB and collector take no restart stamp from a scrutiny manifest change.

The collector re-reads a kubelet-synced ConfigMap on each cron run, and InfluxDB reads its
credentials only at first-run setup, so a restart on every scrutiny manifest change would only
take InfluxDB down. Both entries carry `restart_on: []`, which the renderer honours.

Run: uv run pytest ansible/tests/services/test_scrutiny_rollouts.py
"""

from _helpers import ANSIBLE
from renderer.renderer_lib.task_inputs import include_vars

SCRUTINY = ANSIBLE / "roles/k8s/scrutiny"


def test_influxdb_and_the_collector_are_listed_and_restart_on_nothing():
    declared = {
        entry["name"]: entry["restart_on"]
        for entry in include_vars(SCRUTINY)["manifests_extra_rollouts"]
    }
    assert declared == {"scrutiny-influxdb": [], "scrutiny-collector": []}
