"""A rotation of a key another role's Secret holds rolls the flux-owned reader (#5416).

pihole-exporter reads `PIHOLE_PASSWORD` from the pihole role's `pihole-env` Secret. The
render below is real, through `homelab_render.cluster`, with a fixture `.source` that records
every key the tree's `.source` records, each under a fake digest.

Run: uv run pytest scripts/renderer/tests/test_renderer_cross_role_restart.py
"""

import importlib

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from renderer import homelab_render
from renderer.renderer_lib.restart import (
    RESTART_ANNOTATION,
    any_pod_template,
    source_digests,
)

TAG = "pihole-exporter"
KEYS = tuple(source_digests(homelab_render.SOURCE_FILE))


def _exporter_checksum(tmp_path, name: str, password_digest: str) -> str | None:
    """The exporter Deployment's `checksum/restart` with `pihole_password` at a digest."""
    digests = {key: "d" * 64 for key in KEYS} | {"pihole_password": password_digest}
    source = tmp_path / f"{name}.source"
    source.write_text("".join(f"{k} {v}\n" for k, v in digests.items()))
    cluster = tmp_path / f"{name}.sops.env"
    cluster.write_text("k=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n")
    out = tmp_path / name
    code = homelab_render.cluster(
        out,
        [TAG],
        None,
        flux_owned=lambda: {TAG},
        cluster_file=cluster,
        source_file=source,
    )
    assert code == RENDER_OK
    found = {}
    for path in sorted((out / "cluster" / TAG).glob("*.yaml")):
        for obj in yaml_fast.safe_load_all(path.read_text()):
            template = any_pod_template(obj)
            if template is not None:
                annotations = (template.get("metadata") or {}).get("annotations") or {}
                found[obj["metadata"]["name"]] = annotations.get(RESTART_ANNOTATION)
    assert len(found) >= 1, f"{TAG} rendered no workload"
    return found[TAG]


def test_a_pihole_password_rotation_moves_the_pihole_exporter_checksum(tmp_path):
    assert "pihole_password" in KEYS
    before = _exporter_checksum(tmp_path, "before", "a" * 64)
    after = _exporter_checksum(tmp_path, "after", "b" * 64)
    assert before is not None, f"{TAG} carries no {RESTART_ANNOTATION}"
    assert before != after


def test_the_outside_role_exemptions_hold_no_pihole_exporter_entry():
    """With the checksum covering the password, the D5.5 exemption has no reason left."""
    exempt = importlib.import_module("test_renderer_restart").OUTSIDE_ROLE_EXEMPT
    assert [entry for entry in exempt if entry[0] == TAG] == []
