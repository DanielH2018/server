"""The SUC rehearsal helper: the pins it reads, the overlay it applies, the verdict it judges (H6.1b2).

Each decision has an accept case and the reject cases that a silent pass would hide behind. The
verdict tests matter most: a rehearsal that passes on a Kustomization that is red for another
reason, or on a Plan that never ran, proves nothing about the Plan it was run for.

Run: uv run pytest ansible/roles/setup/hypervisor/tests/test_suc_rehearsal.py
"""

import copy
from pathlib import Path

import pytest
import suc_rehearsal
import yaml
from _helpers import REPO
from _suc_bundle import (
    DIGEST,
    GUEST,
    KUSTOMIZATION,
    TARGET,
    condition,
    plan_without,
    red_snapshot,
    snapshot,
    write_rendered,
)


def judged(expect, snap):
    return suc_rehearsal.judge(expect, "k3s-server", TARGET, GUEST, DIGEST, snap)


def overlaid(tmp_path, **tree):
    rendered = write_rendered(tmp_path / "rendered", **tree)
    version = suc_rehearsal.overlay(
        str(rendered), str(tmp_path / "out"), GUEST, "k3s-server"
    )
    return version, tmp_path / "out"


def test_the_pins_come_from_the_checkout_whichever_role_holds_them():
    pins = suc_rehearsal.pins(str(REPO))
    assert pins["k3s_version"].startswith("v")
    assert (
        pins["k3s_version"] in pins["installer_url"]
        and "{{" not in pins["installer_url"]
    )
    assert pins["registry_image"].startswith("registry:")
    assert pins["flux_url"].endswith("_linux_amd64.tar.gz")
    assert len(pins["installer_sha256"]) == len(pins["flux_sha256"]) == 64


@pytest.mark.parametrize("holders", [0, 2], ids=["none", "two"])
def test_a_pin_held_by_none_or_two_defaults_files_is_refused(tmp_path, holders):
    for name in ("a", "b")[:holders]:
        defaults = tmp_path / "ansible/roles/k8s" / name / "defaults"
        defaults.mkdir(parents=True)
        (defaults / "main.yml").write_text("k3s_version: v1.0.0+k3s1\n")
    with pytest.raises(suc_rehearsal.Refusal, match="k3s_version is held by"):
        suc_rehearsal.pins(str(tmp_path))


def test_the_overlay_selects_the_guest_and_drops_prepare_and_the_unresolvable_fields(
    tmp_path,
):
    version, out = overlaid(tmp_path)
    assert version == TARGET
    plan = yaml.safe_load(
        (out / "bundle/cluster/system-upgrade/plans.yaml").read_text()
    )
    clauses = {
        c["key"]: c.get("values")
        for c in plan["spec"]["nodeSelector"]["matchExpressions"]
    }
    assert clauses["kubernetes.io/hostname"] == [GUEST]
    assert "prepare" not in plan["spec"]
    ks = yaml.safe_load((out / "hand/kustomization.yaml").read_text())
    assert not {"dependsOn", "postBuild", "decryption"} & set(ks["spec"])
    assert ks["spec"]["healthCheckExprs"] and ks["spec"]["timeout"] == "100m"
    assert (out / "hand/components.yaml").exists()


def without_health(field):
    ks = copy.deepcopy(KUSTOMIZATION)
    ks["spec"].pop(field, None)
    return ks


def other_source():
    ks = copy.deepcopy(KUSTOMIZATION)
    ks["spec"]["sourceRef"]["name"] = "prod"
    return ks


@pytest.mark.parametrize(
    ("tree", "why"),
    [
        ({"plan": plan_without("nodeSelector")}, "select no node"),
        ({"kustomization": without_health("healthCheckExprs")}, "no healthCheckExprs"),
        ({"kustomization": other_source()}, "OCIRepository `homelab`"),
        ({"components": False}, "components.yaml is missing"),
    ],
    ids=[
        "no-hostname-clause",
        "no-health-expressions",
        "other-source",
        "no-components",
    ],
)
def test_the_overlay_refuses_a_bundle_that_would_prove_nothing(tmp_path, tree, why):
    with pytest.raises(suc_rehearsal.Refusal, match=why):
        overlaid(tmp_path, **tree)


def test_the_overlay_refuses_an_unknown_plan_and_a_placeholder(tmp_path):
    rendered = write_rendered(tmp_path / "rendered")
    with pytest.raises(suc_rehearsal.Refusal, match="no Plan named k3s-agent"):
        suc_rehearsal.overlay(str(rendered), str(tmp_path / "o1"), GUEST, "k3s-agent")
    plans = rendered / "cluster/system-upgrade/plans.yaml"
    plans.write_text(plans.read_text().replace(TARGET, "${k3s_version}"))
    with pytest.raises(suc_rehearsal.Refusal, match="placeholder"):
        suc_rehearsal.overlay(str(rendered), str(tmp_path / "o2"), GUEST, "k3s-server")


def test_an_upgrade_passes_on_the_label_the_kubelet_and_a_ready_kustomization():
    ok, why = judged("upgrade", snapshot())
    assert ok, why


@pytest.mark.parametrize(
    "snap",
    [
        snapshot(label="old"),
        snapshot(label="", latest=""),
        snapshot(kubelet="v1.0.0+k3s1"),
        snapshot(ready="False", reason="HealthCheckFailed"),
        snapshot(revision="rehearsal@sha256:" + "cd" * 32),
        snapshot(plan=False),
    ],
    ids=[
        "label-differs",
        "no-hash-at-all",
        "not-upgraded",
        "not-ready",
        "stale-revision",
        "no-plan",
    ],
)
def test_an_upgrade_does_not_pass_on_any_one_missing_proof(snap):
    assert not judged("upgrade", snap)[0]


def test_a_red_run_passes_when_the_job_failed_and_the_kustomization_is_not_ready():
    ok, why = judged("red", red_snapshot())
    assert ok and "HealthCheckFailed" in why and "a message" in why


@pytest.mark.parametrize(
    "snap",
    [
        red_snapshot(ready="True"),
        red_snapshot(plan=False),
        red_snapshot(complete=[condition("Complete", "False", "Progressing")]),
        red_snapshot(complete=[condition("Complete", "True")]),
        red_snapshot(revision="rehearsal@sha256:" + "cd" * 32),
    ],
    ids=[
        "ready",
        "plan-never-existed",
        "job-not-failed",
        "plan-completed",
        "stale-revision",
    ],
)
def test_a_red_run_does_not_pass_on_a_kustomization_that_is_red_for_another_reason(
    snap,
):
    assert not judged("red", snap)[0]


def test_the_command_line_reports_pass_and_wait_in_its_exit_status(
    tmp_path, monkeypatch, capsys
):
    import io
    import json

    args = ["judge", "upgrade", "k3s-server", TARGET, GUEST, DIGEST]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(snapshot())))
    assert suc_rehearsal.main(args) == 0 and capsys.readouterr().out.startswith("PASS ")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(snapshot(label="old"))))
    assert suc_rehearsal.main(args) == 1 and capsys.readouterr().out.startswith("WAIT ")
    assert suc_rehearsal.main(["overlay", "x"]) == 2
    assert Path(suc_rehearsal.__file__).name == "suc_rehearsal.py"
