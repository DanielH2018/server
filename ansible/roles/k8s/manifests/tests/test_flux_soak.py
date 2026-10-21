"""The soak Job script's run-time wait and restart legs, over fixture API objects and samples.

Each case is D5's step 16 (D5.3b1) or Open question 19's run-time wait, with the red half the
plan names. `_soak_world.World` is the promotion every case starts from.

Run: uv run pytest ansible/roles/k8s/manifests/tests/test_flux_soak.py
"""

import pytest

import flux_soak
from _soak_world import OTHER, T0, W, R, World, kustomization, pod, restart, run


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "soak-abc-x1")
    return World()


def test_a_clean_promotion_waits_w_past_the_first_ready_and_passes(world, tmp_path):
    assert run(world, tmp_path) == (0, "")
    assert world.now == T0 + 60 + W


# The pass line (#5419) and the deploy annotation (D5.3b3), both on stdout.

ANNOTATION = f"event=deploy services=flux sha={R[:8]} result=ok source=flux"


def test_a_judged_pass_names_its_revision_each_service_and_the_window(
    world, tmp_path, capsys
):
    world.new["sonarr"] = {"footprint": "2"}
    assert run(world, tmp_path) == (0, "")
    # The first Ready is T0 + 60, and W for sonarr's 3600s rule is 3720.
    assert capsys.readouterr().out.splitlines() == [
        f"soak: {R[:8]} judged authelia, sonarr over 3780s; pass",
        ANNOTATION,
    ]


def test_nothing_to_judge_keeps_its_own_line_and_names_no_service(
    world, tmp_path, capsys
):
    world.new = world.old
    assert run(world, tmp_path) == (0, "")
    assert capsys.readouterr().out.splitlines() == [
        "soak: R changes no service; nothing to judge",
        ANNOTATION,
    ]


def test_a_failing_soak_prints_no_pass_line_and_no_annotation(world, tmp_path, capsys):
    restart(world.pods["authelia"][0], T0 + 100)
    assert run(world, tmp_path)[0] == 1
    assert capsys.readouterr().out == ""


# Open question 19: the run-time wait.


def test_a_kustomization_ready_at_an_older_revision_keeps_the_job_waiting(
    world, tmp_path
):
    world.kustomizations[0] = kustomization("authelia", revision=OTHER)
    # The red half: a wait that read only Ready would find nothing pending here.
    assert flux_soak.pending(world.kustomizations, ["authelia"], R) == ["authelia"]
    world.on_sleep = lambda w: w.kustomizations.__setitem__(
        0, kustomization("authelia")
    )
    assert run(world, tmp_path) == (0, "")
    assert world.sleeps[0] == flux_soak.POLL_SECONDS


def test_a_kustomization_not_ready_at_r_keeps_the_job_waiting(world, tmp_path):
    world.kustomizations[0] = kustomization("authelia", ready=False)
    world.on_sleep = lambda w: w.kustomizations.__setitem__(
        0, kustomization("authelia")
    )
    assert run(world, tmp_path) == (0, "")
    assert world.sleeps[0] == flux_soak.POLL_SECONDS


def test_every_kustomization_ready_at_r_proceeds_without_polling(world, tmp_path):
    assert run(world, tmp_path)[0] == 0
    assert flux_soak.POLL_SECONDS not in world.sleeps


def test_a_reset_that_moves_homelab_off_r_ends_the_wait_within_one_poll(
    world, tmp_path
):
    # The 2026-10-18 shape (#5704): the Kustomizations apply last-good, so none names R.
    world.kustomizations[0] = kustomization("authelia", revision=OTHER)
    world.on_sleep = lambda w: setattr(w, "homelab_revision", OTHER)
    code, message = run(world, tmp_path, wait_seconds=100_000)
    assert code == 1
    assert "master@sha1:" + R in message
    assert len(world.sleeps) == 1


def test_the_deadline_fails_naming_every_kustomization_not_ready_at_r(world, tmp_path):
    world.kustomizations = [
        kustomization("authelia", revision=OTHER),
        kustomization("authelia-verify", ready=False, depends=["authelia"]),
        kustomization("sonarr"),
    ]
    code, message = run(world, tmp_path, wait_seconds=60)
    assert code == 1
    assert "not Ready at R by the deadline: authelia, authelia-verify" in message


def test_a_suspended_kustomization_never_blocks_the_wait(world, tmp_path):
    world.new["sonarr"] = {"footprint": "2"}
    world.kustomizations[1] = kustomization("sonarr", ready=False, suspend=True)
    world.pods["media"] = [pod("media", "sonarr-1")]
    restart(world.pods["media"][0], T0 + 100)
    assert run(world, tmp_path) == (0, "")


def test_the_same_tag_unsuspended_and_not_ready_blocks_the_wait(world, tmp_path):
    """The red half of #5317's Job leg: without the suspend, the soak waits on sonarr."""
    world.new["sonarr"] = {"footprint": "2"}
    world.kustomizations[1] = kustomization("sonarr", ready=False)
    code, message = run(world, tmp_path, wait_seconds=60)
    assert code == 1
    assert "not Ready at R by the deadline: sonarr" in message


# Step 16: restarts.


def test_a_restart_after_the_first_ready_fails_and_names_authelia(world, tmp_path):
    restart(world.pods["authelia"][0], T0 + 100)
    assert run(world, tmp_path) == (1, "failed-services: authelia\n")


def test_a_restart_before_the_first_ready_passes(world, tmp_path):
    restart(world.pods["authelia"][0], T0 + 30)
    assert run(world, tmp_path) == (0, "")


def test_a_restart_in_a_pod_older_than_t0_passes(world, tmp_path):
    world.pods["authelia"] = [
        pod("authelia", "authelia-1", created=T0 - 600, ready=T0 - 500)
    ]
    restart(world.pods["authelia"][0], T0 + 100)
    assert run(world, tmp_path) == (0, "")


def test_a_deployment_pod_is_read_through_its_replicaset(world, tmp_path):
    restart(world.pods["authelia"][0], T0 + 100)
    assert run(world, tmp_path)[1] == "failed-services: authelia\n"
    assert (
        "/apis/apps/v1/namespaces/authelia/replicasets/authelia-1-rs" in world.reads_of
    )


def test_a_refused_replicaset_read_exits_1_naming_it(world, tmp_path):
    world.forbidden.add("/apis/apps/v1/namespaces/authelia/replicasets/authelia-1-rs")
    code, message = run(world, tmp_path)
    assert code == 1
    assert "replicasets/authelia-1-rs: HTTP 403" in message


def test_a_replicaset_with_no_deployment_owner_is_not_read(world, tmp_path):
    world.replicasets["authelia-1-rs"] = None
    world.unmapped.add("authelia-1")
    restart(world.pods["authelia"][0], T0 + 100)
    assert run(world, tmp_path) == (0, "")


def test_an_in_scope_pod_with_no_mapping_fails(world, tmp_path):
    world.unmapped.add("authelia-1")
    assert run(world, tmp_path) == (1, "failed-services: unmapped\n")


def test_a_job_pod_with_no_mapping_passes(world, tmp_path):
    world.pods["authelia"].append(
        pod("authelia", "authelia-seed", owner=("Job", "seed"))
    )
    world.unmapped.add("authelia-seed")
    assert run(world, tmp_path) == (0, "")


def test_the_soaks_own_pod_is_never_read(world, tmp_path):
    world.pods["authelia"].append(pod("authelia", "soak-abc-x1"))
    world.unmapped.add("soak-abc-x1")
    assert run(world, tmp_path) == (0, "")


def test_a_ready_pod_with_no_ready_sample_fails_naming_it(world, tmp_path, capsys):
    world.ready_samples = lambda p: []
    assert run(world, tmp_path)[0] == 1
    assert (
        "authelia/authelia-1 is Ready but has no Ready sample"
        in capsys.readouterr().err
    )


def test_a_restart_inside_a_scrape_gap_fails_naming_the_gap(world, tmp_path, capsys):
    restart(world.pods["authelia"][0], T0 + 30)
    world.scrape_gaps = [(T0 - 1, T0 + 100)]
    assert run(world, tmp_path)[0] == 1
    assert "inside a kube-state-metrics scrape gap" in capsys.readouterr().err


def test_a_restart_during_the_gate_legs_wait_fails(world, tmp_path, capsys):
    world.on_sleep = lambda w: restart(w.pods["authelia"][0], T0 + 20)
    assert run(world, tmp_path) == (1, "failed-services: authelia\n")
    assert "restarted during the soak" in capsys.readouterr().err


def test_a_failure_names_each_failing_service_a_snapshot_roles_among_them(
    world, tmp_path
):
    # Only the snapshot Kustomization moved, so sonarr reaches the scope by its stripped name.
    world.new["sonarr-snapshot"] = {"footprint": "2"}
    world.old["sonarr-snapshot"] = {"footprint": "1"}
    world.kustomizations.append(kustomization("sonarr-snapshot"))
    world.pods["media"] = [pod("media", "sonarr-1")]
    restart(world.pods["media"][0], T0 + 100)
    restart(world.pods["authelia"][0], T0 + 100)
    assert run(world, tmp_path)[1] == "failed-services: authelia,sonarr\n"
