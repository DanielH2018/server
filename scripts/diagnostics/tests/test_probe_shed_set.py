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


def test_a_flux_owned_service_gets_its_suspend_command_and_an_ansible_one_none():
    owners = ss.service_owners(
        {"texbrain": {"name": "texbrain"}, "sonarr": {"name": "sonarr"}},
        {"texbrain": "flux"},
    )
    text, code = ss.format_shed_set(
        {
            "texbrain": [("homelab", "Deployment", "texbrain")],
            "sonarr": [("homelab", "Deployment", "sonarr")],
        },
        owners,
    )
    assert code == 0
    assert ss.suspend_command("texbrain") in text
    assert ss.suspend_command("texbrain", suspend=False) in text
    assert "kustomization sonarr" not in text
    assert "--field-manager=homelab-operator" in ss.suspend_command("texbrain")
    rows = {line.split()[0]: line for line in text.splitlines() if "homelab/" in line}
    assert rows["texbrain"].endswith("owner=flux")
    assert rows["sonarr"].endswith("owner=ansible")


def test_an_entry_with_its_own_tags_is_owned_through_each_tag():
    owners = ss.service_owners(
        {"game-stats": {"name": "game-stats", "tags": ["game-stats", "stats"]}},
        {"stats": "flux"},
    )
    assert owners == {"game-stats": {"game-stats": "ansible", "stats": "flux"}}
