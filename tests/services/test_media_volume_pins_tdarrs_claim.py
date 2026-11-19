#!/usr/bin/env python3
"""media-volume pins the local PV that tdarr mounts to `k8s_primary_node`.

tdarr's Deployment requires the same node, so its pod and its pre-pull Job land where the PV
lives. The `tdarr-prepull-lands-on-the-media-node` row of `tests/k8s/test_rendered_properties.py`
ties the rendered Job to the PV's node; this test holds that the PV reads the variable rather
than a literal name.

Run: uv run pytest tests/services/test_media_volume_pins_tdarrs_claim.py
"""

from lib import yaml_fast
from _helpers import K8S_ROLES, load_defaults
from _k8s_render import render_role_template


def test_the_media_volume_pins_tdarrs_claim_to_the_primary_node() -> None:
    # Rendered with a sentinel node, so a PV pinned to a literal name or to any other variable
    # renders something other than the sentinel and fails here.
    pv = yaml_fast.safe_load(
        render_role_template(
            "media-volume", "pv.yaml.j2", {"k8s_primary_node": "sentinel-node"}
        )
    )
    terms = pv["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"]
    assert [v for t in terms for e in t["matchExpressions"] for v in e["values"]] == [
        "sentinel-node"
    ], "media-volume no longer pins the PV to the primary node"
    assert (
        pv["spec"]["claimRef"]["name"]
        == load_defaults(K8S_ROLES / "tdarr")["tdarr_k8s_media_claim"]
    )
