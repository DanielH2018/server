"""A published tag's SOPS manifests: the byte copy and `decryption` (D3.3c).

A role that lists `render_sops_manifests` gets each manifest copied byte for byte into
`cluster/<tag>/` and `decryption` on its Kustomization; one that lists none gets neither. A
manifest without the substitute-disabled annotation fails the render, and a missing one is
refused. Split out of `test_renderer_bundle.py`, which holds the rest of the bundle writer's
tests, when D5's namespace PR took that file past its line cap. Every render copies `DOTENV`, a
fixture cluster file in `tmp_path` holding fake ciphertext.

Run: uv run pytest scripts/renderer/tests/test_renderer_bundle_sops.py
"""

import pytest

from lib import yaml_fast
from lib.exit_codes import RENDER_FAILED, RENDER_OK, RENDER_REFUSED
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE
from renderer.renderer_lib.cluster import KUSTOMIZATION

DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"


@pytest.fixture
def cluster_file(tmp_path):
    """A fixture cluster file holding `DOTENV`."""
    path = tmp_path / CLUSTER_VARS_FILE
    path.write_text(DOTENV)
    return path


SOPS_NAME = "pi-peer-backup.ssh-key-secret.sops.yaml"
SOPS_MANIFEST = (
    "apiVersion: v1\nkind: Secret\nmetadata:\n  name: pi-peer-backup-ssh-key\n"
    "  annotations:\n    kustomize.toolkit.fluxcd.io/substitute: disabled\n"
    "stringData:\n    value: ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
    "sops:\n    mac: ENC[AES256_GCM,data:bQ==,iv:aQ==,tag:dA==,type:str]\n"
)


def _sops_render(tmp_path, cluster_file, manifest: str | None):
    """Publish pi-peer-backup, whose role lists a SOPS manifest, beside bento-pdf."""
    manifests = tmp_path / "cluster-manifests"
    manifests.mkdir()
    if manifest is not None:
        (manifests / SOPS_NAME).write_text(manifest)
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out,
        ["pi-peer-backup", "bento-pdf"],
        None,
        flux_owned=lambda: {"pi-peer-backup", "bento-pdf"},
        cluster_file=cluster_file,
        manifests_dir=manifests,
    )
    return code, out


def test_a_role_with_render_sops_manifests_gets_the_copy_and_decryption(
    tmp_path, cluster_file
):
    code, out = _sops_render(tmp_path, cluster_file, SOPS_MANIFEST)
    assert code == RENDER_OK
    directory = out / "cluster" / "pi-peer-backup"
    assert (directory / SOPS_NAME).read_bytes() == SOPS_MANIFEST.encode()
    listed = yaml_fast.safe_load((directory / KUSTOMIZATION).read_text())["resources"]
    assert SOPS_NAME in listed
    docs = yaml_fast.safe_load_all((out / "flux" / "pi-peer-backup.yaml").read_text())
    assert next(docs)["spec"]["decryption"] == {
        "provider": "sops",
        "secretRef": {"name": "sops-age"},
    }


def test_a_role_without_render_sops_manifests_gets_neither(tmp_path, cluster_file):
    code, out = _sops_render(tmp_path, cluster_file, SOPS_MANIFEST)
    assert code == RENDER_OK
    flux = yaml_fast.safe_load((out / "flux" / "bento-pdf.yaml").read_text())
    assert "decryption" not in flux["spec"]
    assert not list((out / "cluster" / "bento-pdf").glob("*.sops.*"))


def test_a_sops_manifest_without_the_substitute_annotation_is_flagged(
    tmp_path, capsys, cluster_file
):
    bare = SOPS_MANIFEST.replace(
        "  annotations:\n    kustomize.toolkit.fluxcd.io/substitute: disabled\n", ""
    )
    code, out = _sops_render(tmp_path, cluster_file, bare)
    assert code == RENDER_FAILED
    assert "lacks the annotation" in capsys.readouterr().err
    assert not out.exists()


def test_a_missing_sops_manifest_is_refused(tmp_path, capsys, cluster_file):
    code, out = _sops_render(tmp_path, cluster_file, None)
    assert code == RENDER_REFUSED
    assert "cannot copy its SOPS manifest" in capsys.readouterr().err
    assert not out.exists()
