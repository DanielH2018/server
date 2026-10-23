"""The secrets-unit container case's host-side parts (H4.1c).

The case itself boots a systemd container, so it runs only in the `host_packages` job. These
tests cover the `sealed` fixture's render and the assertion that two builds of it differ only at
the ciphertext path, which need no container.

Run: uv run pytest scripts/validate/tests/test_secrets_case.py
"""

import json
import shutil

import pytest
from validate.validate_lib import secrets_case as case
from validate.validate_lib.host_fixtures import render_fixture

OTHER = "nut_monitor_password: ENC[AES256_GCM,data:another-stand-in,type:str]\n"


@pytest.fixture
def renders(tmp_path):
    """The sealed fixture rendered with the stand-in ciphertext and with another one."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "daniel-pi.sops.yaml").write_text(OTHER)
    real, fixture = tmp_path / "real", tmp_path / "fixture"
    render_fixture("sealed", real)
    render_fixture("sealed", fixture, sops_dir=other)
    return real, fixture


def test_the_sealed_fixture_ships_its_ciphertext_output_and_credential(renders):
    real, _ = renders
    root = real / case.PACKAGE_ROOT
    assert (root / "usr/lib/homelab/secrets/daniel-pi.sops.yaml").is_file()
    assert (
        root / "usr/lib/homelab/secret-templates/etc/homelab-fixture/sealed.conf"
    ).is_file()
    manifest = json.loads((root / "usr/lib/homelab/secrets.json").read_text())
    assert manifest["credentials"] == ["nut_monitor_password"]
    assert manifest["outputs"][0]["keys"] == ["nut_monitor_password"]


def test_two_builds_that_differ_only_at_the_ciphertext_are_clean(renders):
    assert case.compare_builds(*renders) == []


def test_a_build_that_differs_elsewhere_is_flagged(renders):
    real, fixture = renders
    extra = fixture / case.PACKAGE_ROOT / "etc/extra.conf"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("x\n")
    (problem,) = case.compare_builds(real, fixture)
    assert "etc/extra.conf" in problem


def test_a_build_whose_hash_ignored_the_ciphertext_is_flagged(renders, tmp_path):
    """Two identical renders: the ciphertext path and the hash both fail to differ."""
    real, _ = renders
    twin = tmp_path / "twin"
    shutil.copytree(real, twin, symlinks=True)
    problems = case.compare_builds(real, twin)
    assert any("is missing" in p for p in problems)
    assert any(case.HASH_FIELD in p for p in problems)


def test_a_manifest_that_differs_beyond_the_hash_is_flagged(renders):
    real, fixture = renders
    path = fixture / case.MANIFEST
    manifest = json.loads(path.read_text())
    manifest["units"]["homelab-extra.service"] = {}
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    assert any("beyond" in p for p in case.compare_builds(real, fixture))
