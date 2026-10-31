"""The container's stand-in ufw against reconcile's real ufw arm (H7.30).

The Pi container cases run the real Pi package's ufw arm against `pi_host_seed`'s stand-in, so a
stand-in the arm disagrees with fails three cases in CI's `host_packages` job, which no local run
reaches. These tests run the arm itself against the stand-in, with the Pi's rendered declarations.

Run: uv run pytest scripts/validate/tests/test_pi_host_seed.py
"""

import json

import pytest
from validate.validate_lib import pi_host_seed as seed

from lib.proc_testing import write_exec
from reconcile_lib import ufw
from renderer.renderer_lib import host as host_mode


@pytest.fixture(scope="module")
def section() -> dict:
    package = host_mode.render_host("daniel-pi")
    return json.loads(package.files["/usr/lib/homelab/manifest.json"][0])["ufw"]


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    write_exec(bin_dir / "ufw", seed.UFW_STUB_SOURCE)
    write_exec(bin_dir / "iptables", seed.IPTABLES_STUB_SOURCE)
    state = tmp_path / "ufw-stub.json"
    monkeypatch.setenv("UFW_STUB_STATE", str(state))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return state


def test_the_arm_reconciles_the_pi_rules_against_the_stand_in(section, stand_in):
    ok, record, drift = ufw.reconcile(section, None)

    assert ok and drift == 0
    assert record is not None
    rules = json.loads(stand_in.read_text())["rules"]
    labels = [comment for _, comment in rules]
    assert labels.index("homelab:keep:lan-ssh") < labels.index("homelab:keep:ssh-limit")
    assert {r["id"] for r in section["rules"]} == set(record["ids"])
    assert len(rules) == len(section["rules"])


def test_a_second_run_against_the_stand_in_changes_nothing(section, stand_in):
    ufw.reconcile(section, None)
    before = stand_in.read_text()

    ok, _, _ = ufw.reconcile(section, None)

    assert ok
    assert stand_in.read_text() == before


def test_the_stand_in_relabels_an_appended_spec_and_skips_an_inserted_one(stand_in):
    ufw.ufw("allow", "80/tcp", "comment", "x")
    ufw.ufw("allow", "80/tcp", "comment", "y")
    ufw.ufw("insert", "1", "allow", "80/tcp", "comment", "z")

    assert ["allow 80/tcp", "y"] in json.loads(stand_in.read_text())["rules"]
    assert ("allow 80/tcp", "y") in (ufw.added() or [])
