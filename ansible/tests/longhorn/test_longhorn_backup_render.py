"""The `longhorn-backup` render: the arm flags, the Secrets and the Kustomization (H1.7b).

The plan's *Verification* section asks for these as permanent tests. Each case renders the
role's templates with `_k8s_render.render_role_template`, which lays `overrides` over the role's
defaults, so a flag flips while the template stays fixed.

- Each arm flag blanks its own target's `backupTargetURL` and no other's, and an armed render
  carries a URL on each. The incident lever (ADR-0007) rests on this: blanking B2 must not blank
  R2.
- The two credential Secrets render `stringData` with the three S3 keys, and `AWS_ENDPOINTS`
  follows the region variable. The `data:`/`b64encode` form of H1.7a's predecessor is gone.
- The Kustomization Flux applies carries no `suspend` field: H1 never suspends this tag (D5.1b's
  `flux_suspend` is the only temporary stop).

Run: uv run pytest ansible/tests/longhorn/test_longhorn_backup_render.py
"""

import pytest
from _k8s_render import render_role_template

from lib import yaml_fast
from renderer import homelab_render

ROLE = "longhorn-backup"
DEFAULT_TARGET = "longhorn-backuptarget-default.yaml.j2"
R2_TARGET = "longhorn-backuptarget-r2.yaml.j2"
B2_SECRET = "longhorn-b2-secret.yaml.j2"
R2_SECRET = "longhorn-r2-secret.yaml.j2"


def _url(template: str, **overrides) -> str:
    doc = yaml_fast.safe_load(render_role_template(ROLE, template, overrides))
    return doc["spec"]["backupTargetURL"]


@pytest.mark.parametrize(
    ("b2_armed", "r2_armed"),
    [(True, True), (False, True), (True, False), (False, False)],
)
def test_each_arm_flag_blanks_only_its_own_target(b2_armed, r2_armed):
    flags = {
        "k3s_longhorn_backup_armed": b2_armed,
        "k3s_longhorn_r2_armed": r2_armed,
    }
    assert bool(_url(DEFAULT_TARGET, **flags)) is b2_armed
    assert bool(_url(R2_TARGET, **flags)) is r2_armed


def test_an_armed_render_names_the_bucket_region_and_prefix():
    # The bucket renders as the stub the test context substitutes for a secret; the region and the
    # prefix are plain variables.
    assert _url(DEFAULT_TARGET, k3s_longhorn_b2_region="us-west-004").endswith(
        "@us-west-004/longhorn"
    )
    assert _url(R2_TARGET).endswith("@auto/longhorn")


def test_the_credential_secrets_render_string_data_with_the_three_s3_keys():
    for template in (B2_SECRET, R2_SECRET):
        doc = yaml_fast.safe_load(render_role_template(ROLE, template, {}))
        assert doc["kind"] == "Secret"
        assert "data" not in doc
        assert set(doc["stringData"]) == {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_ENDPOINTS",
        }


def test_the_b2_endpoint_follows_the_region_variable():
    doc = yaml_fast.safe_load(
        render_role_template(ROLE, B2_SECRET, {"k3s_longhorn_b2_region": "us-west-004"})
    )
    assert (
        doc["stringData"]["AWS_ENDPOINTS"] == "https://s3.us-west-004.backblazeb2.com"
    )


def test_the_kustomization_flux_applies_carries_no_suspend(tmp_path):
    out = tmp_path / "out"
    assert homelab_render.main(["cluster", "--out", str(out), "--role", ROLE]) == 0
    kustomization = yaml_fast.safe_load((out / "flux" / f"{ROLE}.yaml").read_text())
    assert kustomization["kind"] == "Kustomization"
    assert "suspend" not in kustomization["spec"]
