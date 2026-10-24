"""The trim arm of the longhorn-reconcile script (H2.2a), against the fixture API server.

The arm puts the per-job label `recurring-job.longhorn.io/trim` on a volume that is attached and
has a Running ReplicaSet or StatefulSet workload, bounded by `trim_claims`, and sets the `default`
group explicitly on a daily-tier volume that gains the label. The label strings are typed out
here, as in test_longhorn_reconcile.py: an expectation built from the script's constants would
follow a rename and pass.

Run: uv run pytest ansible/roles/k8s/longhorn-reconcile/tests/test_longhorn_reconcile_trim.py
"""

import json

import longhorn_reconcile as lr
import pytest
from _helpers import load_defaults
from _k8s_render import render_role_template
from _reconcile_world import NS, P, PVC_CLASS, TIERS, VOLUMES, World, _pvc, _volume
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

TRIM = "recurring-job.longhorn.io/trim"
RUNNING_RS = (("ReplicaSet", "Running"),)


def _env(tmp_path, tiers):
    path = tmp_path / "tiers.json"
    path.write_text(json.dumps(tiers))
    return {"RECONCILE_NAMESPACE": NS, "RECONCILE_TIERS_FILE": str(path)}


def _world(*volumes):
    return World(list(volumes), [_pvc(c, k) for c, k in PVC_CLASS.items()])


def _groups(world, name):
    labels = world.volumes[name]["metadata"]["labels"]
    return {k.removeprefix(P) for k in labels if k.startswith(P)}


def _trimmed(world):
    return sorted(
        n for n, v in world.volumes.items() if TRIM in v["metadata"]["labels"]
    )


@pytest.mark.parametrize(
    ("state", "workloads", "joins"),
    [
        pytest.param("attached", RUNNING_RS, True, id="attached-replicaset"),
        pytest.param(
            "attached", (("StatefulSet", "Running"),), True, id="attached-statefulset"
        ),
        pytest.param("detached", RUNNING_RS, False, id="detached"),
        pytest.param("attached", (("Job", "Running"),), False, id="cronjob-job-only"),
        pytest.param(
            "attached", (("ReplicaSet", "Failed"),), False, id="replicaset-not-running"
        ),
        pytest.param("attached", (), False, id="scaled-to-zero-no-workload"),
        pytest.param(None, RUNNING_RS, False, id="no-state-read"),
    ],
)
def test_the_trim_rule_reads_state_and_a_running_replicaset_or_statefulset(
    state, workloads, joins, tmp_path
):
    volume = _volume(
        "pvc-1", "homelab/traefik-acme", "default", state=state, workloads=workloads
    )
    world = _world(volume)
    assert lr.main([], _env(tmp_path, TIERS), world) == 0
    assert (TRIM in world.volumes["pvc-1"]["metadata"]["labels"]) is joins


def test_the_label_is_the_per_job_key_and_never_a_trim_group(tmp_path):
    world = _world(
        _volume(
            "pvc-1",
            "homelab/traefik-acme",
            "default",
            state="attached",
            workloads=RUNNING_RS,
        )
    )
    assert lr.main([], _env(tmp_path, TIERS), world) == 0
    labels = world.volumes["pvc-1"]["metadata"]["labels"]
    assert labels[TRIM] == "enabled"
    assert "recurring-job-group.longhorn.io/trim" not in labels
    assert lr.TRIM_LABEL == TRIM


# (claim, group labels before) -> group labels after a run that gives the volume the trim label.
DEFAULT_ARM = [
    pytest.param("homelab/traefik-acme", [], ["default"], id="no-group-gains-default"),
    pytest.param("homelab/traefik-acme", ["default"], ["default"], id="has-default"),
    pytest.param(
        "homelab/crowdsec-db",
        ["no-backup"],
        ["no-backup"],
        id="nobackup-never-gains-default",
    ),
    pytest.param(
        "homelab/sonarr-config",
        ["weekly-backup-d3"],
        ["weekly-backup-d3"],
        id="weekly-shard-never-gains-default",
    ),
    pytest.param(
        "homelab/crowdsec-db",
        [],
        [],
        id="nobackup-listed-claim-with-no-group-stays-out",
    ),
]


@pytest.mark.parametrize(("claim", "before", "after"), DEFAULT_ARM)
def test_default_is_set_only_on_a_daily_tier_volume_that_gains_the_trim_label(
    claim, before, after, tmp_path
):
    world = _world(
        _volume("pvc-1", claim, *before, state="attached", workloads=RUNNING_RS)
    )
    assert lr.main([], _env(tmp_path, TIERS), world) == 0
    assert TRIM in world.volumes["pvc-1"]["metadata"]["labels"]
    assert _groups(world, "pvc-1") == set(after)
    assert len(world.writes()) == 1


def _two_eligible():
    return _world(
        _volume(
            "pvc-a",
            "homelab/traefik-acme",
            "default",
            target="r2",  # the TIERS fixture routes this claim to r2; keep the target arm quiet
            state="attached",
            workloads=RUNNING_RS,
        ),
        _volume(
            "pvc-b",
            "homelab/authelia-config",
            "default",
            state="attached",
            workloads=RUNNING_RS,
        ),
    )


def test_a_bound_keeps_the_trim_label_off_every_unlisted_claim(tmp_path):
    # fact: ansible/roles/k8s/longhorn-reconcile/CLAUDE.md#What it writes, and how often (the autonomous-role contract)
    world = _two_eligible()
    env = _env(tmp_path, {**TIERS, "trim_claims": ["homelab/authelia-config"]})
    assert lr.main([], env, world) == 0
    assert _trimmed(world) == ["pvc-b"]


def test_an_empty_bound_admits_no_volume(tmp_path):
    world = _two_eligible()
    assert lr.main([], _env(tmp_path, {**TIERS, "trim_claims": []}), world) == 0
    assert _trimmed(world) == []


def test_an_absent_bound_admits_every_volume_the_rule_admits(tmp_path):
    world = _two_eligible()
    assert "trim_claims" not in TIERS
    assert lr.main([], _env(tmp_path, TIERS), world) == 0
    assert _trimmed(world) == ["pvc-a", "pvc-b"]


def test_the_label_leaves_a_volume_the_bound_or_the_rule_no_longer_admits(tmp_path):
    outside = _volume(
        "pvc-a",
        "homelab/traefik-acme",
        "default",
        extra={TRIM: "enabled"},
        state="attached",
        workloads=RUNNING_RS,
    )
    detached = _volume(
        "pvc-b",
        "homelab/authelia-config",
        "default",
        extra={TRIM: "enabled"},
        state="detached",
        workloads=RUNNING_RS,
    )
    world = _world(outside, detached)
    env = _env(tmp_path, {**TIERS, "trim_claims": ["homelab/authelia-config"]})
    assert lr.main([], env, world) == 0
    assert _trimmed(world) == []
    # The group label stays: only the trim label moves.
    assert _groups(world, "pvc-a") == {"default"}


def test_a_second_run_with_the_trim_arm_changes_nothing(tmp_path, capsys):
    world = _two_eligible()
    world.volumes["pvc-b"]["metadata"]["labels"].clear()
    env = _env(tmp_path, {**TIERS, "trim_claims": ["homelab/authelia-config"]})
    assert lr.main([], env, world) == 0
    assert len(world.writes()) == 1
    first = len(world.calls)
    assert lr.main([], env, world) == 0
    assert [c for c in world.calls[first:] if c[0] == "PATCH"] == []
    assert _groups(world, "pvc-b") == {"default"}


def test_the_would_admit_line_names_the_unbounded_set_whatever_the_bound(
    tmp_path, capsys
):
    detached = _volume(
        "pvc-c",
        "homelab/sonarr-config",
        "weekly-backup-d3",
        state="detached",
        workloads=RUNNING_RS,
    )
    job_only = _volume(
        "pvc-d",
        "homelab/radarr-config",
        "weekly-backup-d5",
        state="attached",
        workloads=(("Job", "Running"),),
    )
    world = _two_eligible()
    world.volumes.update({"pvc-c": detached, "pvc-d": job_only})
    env = _env(tmp_path, {**TIERS, "trim_claims": ["homelab/authelia-config"]})
    assert lr.main([], env, world) == 0
    out = capsys.readouterr().out
    assert (
        "longhorn-reconcile: trim would-admit: "
        "homelab/authelia-config, homelab/traefik-acme\n"
    ) in out


def test_the_would_admit_line_reads_none_when_the_rule_admits_nothing(tmp_path, capsys):
    world = _world(_volume("pvc-1", "homelab/traefik-acme", "default"))
    assert lr.main(["--report-only"], _env(tmp_path, TIERS), world) == 0
    assert "trim would-admit: none\n" in capsys.readouterr().out


def test_report_only_names_the_trim_change_and_writes_nothing(tmp_path, capsys):
    world = _world(
        _volume(
            "pvc-1",
            "homelab/traefik-acme",
            state="attached",
            workloads=RUNNING_RS,
        )
    )
    assert lr.main(["--report-only"], _env(tmp_path, TIERS), world) == 0
    out = capsys.readouterr().out
    assert "would patch volume pvc-1" in out
    assert f"{TRIM}=enabled" in out
    assert world.writes() == []


@pytest.mark.parametrize("bad", ["homelab/a", [1], {"homelab/a": 1}])
def test_a_malformed_bound_is_refused(bad, tmp_path):
    path = tmp_path / "tiers.json"
    path.write_text(json.dumps({**TIERS, "trim_claims": bad}))
    with pytest.raises(lr.Failed, match="trim_claims"):
        lr.load_tiers(path)


# The reconcile Role and ClusterRole grants against a run that exercises the trim arm. The
# repo-wide derivation is ansible/tests/k8s/test_rbac_against_rest_script.py; this one keeps the
# arm's own calls inside the grants without leaving the role's tests.
def _granted() -> set[tuple[str, str]]:
    docs = yaml_fast.safe_load_all(
        render_role_template("longhorn-reconcile", "rbac.yaml.j2")
    )
    return {
        (resource, verb)
        for doc in docs
        if doc["kind"] in ("Role", "ClusterRole")
        for rule in doc["rules"]
        for resource in rule["resources"]
        for verb in rule["verbs"]
    }


def _needs(calls) -> set[tuple[str, str]]:
    needs = set()
    for method, path, _ in calls:
        if method == "GET" and path == VOLUMES:
            needs.add(("volumes", "list"))
        elif method == "GET" and path == "/api/v1/persistentvolumeclaims":
            needs.add(("persistentvolumeclaims", "list"))
        elif method == "PATCH" and path.startswith(VOLUMES + "/"):
            needs.add(("volumes", "patch"))
        else:
            raise AssertionError(
                f"the trim arm made an unexpected call {method} {path}"
            )
    return needs


def _trim_run_calls(tmp_path):
    world = _two_eligible()
    world.volumes["pvc-b"]["metadata"]["labels"].clear()
    env = _env(tmp_path, {**TIERS, "trim_claims": ["homelab/authelia-config"]})
    assert lr.main([], env, world) == 0
    return world.calls


def test_the_trim_arm_calls_stay_inside_the_rendered_reconcile_rbac(tmp_path):
    needs = _needs(_trim_run_calls(tmp_path))
    # Non-vacuity: the run listed both collections and patched a volume.
    assert needs == {
        ("volumes", "list"),
        ("volumes", "patch"),
        ("persistentvolumeclaims", "list"),
    }
    assert needs <= _granted()


def test_a_grant_without_patch_is_flagged_for_the_trim_arm(tmp_path):
    needs = _needs(_trim_run_calls(tmp_path))
    assert needs - (_granted() - {("volumes", "patch")}) == {("volumes", "patch")}


def test_the_configmap_renders_the_canary_bound_and_an_empty_bound_stays_empty():
    canary = load_defaults(K8S_ROLES / "longhorn-reconcile")[
        "longhorn_reconcile_trim_claims"
    ]
    assert canary == ["homelab/authelia-config"]

    def tiers(overrides):
        (doc,) = yaml_fast.safe_load_all(
            render_role_template("longhorn-reconcile", "configmap.yaml.j2", overrides)
        )
        return json.loads(doc["data"]["tiers.json"])

    assert tiers({})["trim_claims"] == canary
    assert tiers({"longhorn_reconcile_trim_claims": []})["trim_claims"] == []
