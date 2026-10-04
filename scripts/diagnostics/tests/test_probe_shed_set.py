"""Tests for `probe.py shed-set`'s rendering of the lost-node shed set."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.probe_lib import shed_set as ss


def test_scalable_workloads_are_listed_and_unscalable_roles_named():
    text, code = ss.format_shed_set(
        {
            "sonarr": [("homelab", "Deployment", "sonarr")],
            "node-exporter": [],
            "deploy-ui": None,
        }
    )
    assert code == 0
    assert "homelab/deployment/sonarr" in text
    assert "no Deployment or StatefulSet to scale: node-exporter, deploy-ui" in text


def test_a_set_with_nothing_to_scale_is_inconclusive():
    text, code = ss.format_shed_set({"node-exporter": [], "deploy-ui": None})
    assert code == 2 and text.startswith("INCONCLUSIVE")
