"""The secrets-unit container case's host-side parts (H4.1c).

The case itself boots a systemd container, so it runs only in the `host_packages` job. These
tests cover the `sealed` fixture's render and the assertion that two builds of it differ only at
the ciphertext path, which need no container.

Run: uv run pytest scripts/validate/tests/test_secrets_case.py
"""

import json
import shutil
from pathlib import Path

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


class _Container(case.Container):
    """A container that runs nothing and records the scripts it was asked to run."""

    def __init__(self):
        super().__init__("image", "homelab-host-test-stub", Path("/nonexistent"))
        self.commands = []

    def ok(self, script):
        self.commands.append(script)
        return ""


class _Cases:
    """The part of `host_package_cases.Cases` that `seal_pi` reads and writes."""

    def __init__(self, tmp_path, tools=("tools-head",)):
        self.work = tmp_path
        self.tools = tools
        self.debs = {"tools-head": Path("/x/homelab-tools_1_amd64.deb")}
        self.versions = {"pi-head": "1.0", "pi-dropin": "1.0.3", "pi-disarmed": "1.0.4"}
        self.added = {}

    def add(self, label, deb, name=None):
        self.added[label] = (deb, name)


def test_seal_pi_rebuilds_the_real_pi_package_and_both_variants_with_the_sealed_ciphertext(
    tmp_path,
):
    built = {}

    def fake_build(directory, version, out):
        built[version] = directory
        return out / f"{version}.deb"

    c, cases = _Container(), _Cases(tmp_path)

    case.seal_pi(
        cases,
        c,
        seal=lambda c, values: f"sealed:{sorted(values)}\n",
        make_deb=fake_build,
    )

    assert any("age-keygen -o" in command for command in c.commands)
    # The real package's compose unit runs in a container with no engine, so `docker` is stubbed.
    assert any(case.DOCKER_STUB_PATH in command for command in c.commands)
    assert any("homelab-tools_1_amd64.deb" in command for command in c.commands)
    assert set(cases.added) == {"pi-head", "pi-dropin", "pi-disarmed"}
    assert all(
        name.startswith("sealed-homelab-host-test-stub-")
        for _, name in cases.added.values()
    )
    # Every build, at the real build's version, ships the sealed ciphertext for the two tokens.
    assert set(built) == {"1.0", "1.0.3", "1.0.4"}
    for directory in built.values():
        shipped = directory / "root/usr/lib/homelab/secrets/daniel-pi.sops.yaml"
        assert shipped.read_text() == (
            "sealed:['pi_recovery_push_token', 'pi_sd_health_push_token']\n"
        )


def test_seal_pi_refuses_a_job_that_built_no_homelab_tools(tmp_path):
    with pytest.raises(case.CaseFailed, match="no homelab-tools"):
        case.seal_pi(_Cases(tmp_path, tools=()), _Container())
