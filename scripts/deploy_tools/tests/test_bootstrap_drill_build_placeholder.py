"""The drill's `build` refuses hand-applied documents holding `${` or `$$` (#8254).

Holds only while `bootstrap_drill.py` defines `placeholder_problems`; once the predicate moves
into the renderer test, `test_bootstrap_drill_check_wiring.py` covers the issue alone.

Run: uv run pytest scripts/deploy_tools/tests/test_bootstrap_drill_build_placeholder.py
"""

import copy

import pytest
from lib import yaml_fast

import bootstrap_drill as drill

SYNC_DOCS = list(
    yaml_fast.safe_load_all(
        """
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata: {name: homelab, namespace: flux-system}
spec:
  url: oci://ghcr.io/example/manifests
  ref: {tag: prod}
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata: {name: homelab, namespace: flux-system}
spec: {path: ./flux, prune: true}
"""
    )
)


@pytest.mark.parametrize("placeholder", ["./${cluster_path}", "./$$cluster_path"])
def test_build_refuses_a_hand_applied_root_holding_a_placeholder(tmp_path, placeholder):
    if not hasattr(drill, "placeholder_problems"):
        return
    docs = copy.deepcopy(SYNC_DOCS)
    docs[1]["spec"]["path"] = placeholder
    try:
        refusal = drill.build(tmp_path, docs, "reg:5000", lambda text: "ENC:" + text)
    except (Exception, SystemExit) as error:
        refusal = error
    assert refusal, f"build wrote a hand-applied root whose path is {placeholder!r}"
