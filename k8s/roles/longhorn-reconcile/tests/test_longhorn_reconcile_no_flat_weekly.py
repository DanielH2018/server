"""longhorn-reconcile leaves a volume on the retired flat weekly group label alone.

The job neither migrates nor removes `recurring-job-group.longhorn.io/weekly-backup`.

Run: uv run pytest k8s/roles/longhorn-reconcile/tests/test_longhorn_reconcile_no_flat_weekly.py
"""

import json

import longhorn_reconcile as lr
import pytest
from _reconcile_world import NS, P, PVC_CLASS, TIERS, World, _pvc, _volume

FLAT_GROUP = "weekly-backup"


@pytest.mark.parametrize(
    "claim",
    ["homelab/radarr-config", "homelab/authelia-config"],
    ids=["claim-on-the-weekly-list", "claim-off-the-weekly-list"],
)
def test_a_volume_on_the_flat_weekly_label_is_no_longer_patched(claim, tmp_path):
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps(TIERS))
    env = {"RECONCILE_NAMESPACE": NS, "RECONCILE_TIERS_FILE": str(tiers)}
    world = World(
        [_volume("pvc-1", claim, FLAT_GROUP)],
        [_pvc(c, k) for c, k in PVC_CLASS.items()],
    )
    assert lr.main([], env, world) == 0
    assert world.writes() == []
    assert world.volumes["pvc-1"]["metadata"]["labels"] == {
        f"{P}{FLAT_GROUP}": "enabled"
    }
