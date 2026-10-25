"""The longhorn-reconcile script against a fixture API server (H1 plan, H1.6).

`World` answers the script's reads from dicts, applies its merge patches the way the API server
does, and records every call. The label cases are a transition table with the
label strings typed out here, as `setup/k3s`'s `test_every_label_task_renders_the_live_labels`
did for the tasks this script replaces: an expectation built from the script's own constants
would follow a rename and pass, and a one-byte change in a live label drops the volume out of
its RecurringJob with nothing failing.

The CronJob's RBAC against these calls is in
ansible/tests/k8s/test_rbac_against_rest_script.py, and the render in test_longhorn_reconcile_render.py.

Run: uv run pytest ansible/roles/k8s/longhorn-reconcile/tests/test_longhorn_reconcile.py
"""

import io
import json
import urllib.error
from email.message import Message

import longhorn_reconcile as lr
import pytest
from _reconcile_world import (
    NS,
    P,
    PVC_CLASS,
    TIERS,
    VOLUMES,
    World,
    _pvc,
    _volume,
)
from longhorn_groups import (
    backup_group_label,
    longhorn_backup_name,
    weekly_backup_group,
)


@pytest.fixture
def env(tmp_path):
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps(TIERS))
    return {
        "RECONCILE_NAMESPACE": NS,
        "RECONCILE_TIERS_FILE": str(tiers),
    }


# (claim, labels before) -> labels after, the group labels only.
TRANSITIONS = [
    pytest.param(
        "homelab/crowdsec-db", ["default"], ["no-backup"], id="nobackup-leaves-daily"
    ),
    pytest.param(
        "homelab/sonarr-config",
        ["default"],
        ["weekly-backup-d3"],
        id="weekly-leaves-daily",
    ),
    pytest.param(
        "homelab/authelia-config",
        ["no-backup"],
        ["default"],
        id="delisted-longhorn-class-volume-returns-to-daily",
    ),
    pytest.param(
        "homelab/prometheus-data",
        ["no-backup"],
        ["no-backup"],
        id="nobackup-class-volume-stays-out-of-daily",
    ),
    pytest.param(
        "homelab/sonarr-config",
        ["no-backup"],
        ["no-backup"],
        id="weekly-listed-volume-is-never-moved-out-of-nobackup",
    ),
    pytest.param(
        "homelab/radarr-config",
        ["weekly-backup"],
        ["weekly-backup-d5"],
        id="legacy-flat-label-migrates-to-its-shard",
    ),
    pytest.param(
        "homelab/authelia-config",
        ["weekly-backup"],
        ["default"],
        id="legacy-flat-label-off-the-list-returns-to-daily",
    ),
    pytest.param(
        "homelab/authelia-config",
        ["weekly-backup-d2"],
        ["default"],
        id="delisted-shard-member-returns-to-daily",
    ),
    pytest.param(
        "homelab/sonarr-config",
        ["weekly-backup-d1"],
        ["weekly-backup-d3"],
        id="edited-shard-moves-the-volume-to-its-declared-weekday",
    ),
    pytest.param(
        "homelab/sonarr-config",
        ["weekly-backup-d3"],
        ["weekly-backup-d3"],
        id="settled-shard",
    ),
    pytest.param(
        "homelab/authelia-config", ["default"], ["default"], id="settled-daily"
    ),
    pytest.param("homelab/orphan", [], [], id="a-volume-in-no-group-is-left-alone"),
]


@pytest.mark.parametrize(("claim", "before", "after"), TRANSITIONS)
def test_each_group_transition_writes_the_live_labels(
    claim, before, after, env, capsys
):
    volume = _volume("pvc-1", claim, *before, extra={"longhorn.io/other": "kept"})
    world = World([volume], [_pvc(c, k) for c, k in PVC_CLASS.items()])
    assert lr.main([], env, world) == 0
    labels = world.volumes["pvc-1"]["metadata"]["labels"]
    assert {k.removeprefix(P) for k in labels if k.startswith(P)} == set(after)
    assert all(v == "enabled" for k, v in labels.items() if k.startswith(P))
    assert labels["longhorn.io/other"] == "kept"
    # One merge patch per changed volume, and none when it already sits where it belongs.
    assert len(world.writes()) == (0 if before == after else 1)


def test_a_volume_with_no_claim_is_left_alone_unless_it_is_on_the_legacy_label(env):
    bound = _volume("pvc-1", "homelab/x", "default")
    bound["status"] = {}
    world = World([bound], [])
    assert lr.main([], env, world) == 0
    assert world.writes() == []


def test_a_second_run_changes_nothing(env, capsys):
    """`homelab/rerun-safe` names this test: the gate Job's second run reads and writes nothing."""
    world = World(
        [
            _volume("pvc-1", "homelab/crowdsec-db", "default"),
            _volume("pvc-2", "homelab/authelia-config", "no-backup"),
            _volume("pvc-3", "homelab/radarr-config", "weekly-backup"),
            _volume("pvc-4", "homelab/sonarr-config", "default"),
            _volume("pvc-5", "homelab/traefik-acme", "default", target=""),
            _volume("pvc-6", "homelab/authelia-config", "default", target="r2"),
        ],
        [_pvc(c, k) for c, k in PVC_CLASS.items()],
    )
    assert lr.main([], env, world) == 0
    # Four volumes move groups and two change target.
    assert len(world.writes()) == 6
    first = len(world.calls)
    assert lr.main([], env, world) == 0
    assert [c for c in world.calls[first:] if c[0] == "PATCH"] == []
    assert "0 volume change(s)" in capsys.readouterr().out


def test_the_group_names_are_the_ones_setup_k3s_and_the_recurring_jobs_use():
    """The label contract, typed out and held equal to the module the RecurringJobs read."""
    assert lr.label(lr.DEFAULT) == "recurring-job-group.longhorn.io/default"
    assert lr.label(lr.NO_BACKUP) == "recurring-job-group.longhorn.io/no-backup"
    assert lr.label(lr.LEGACY_WEEKLY) == "recurring-job-group.longhorn.io/weekly-backup"
    assert (
        lr.label(lr.shard_group(6))
        == "recurring-job-group.longhorn.io/weekly-backup-d6"
    )
    for group in ("default", "no-backup", "weekly-backup"):
        assert lr.label(group) == backup_group_label(group)
    for shard in range(7):
        assert lr.label(lr.shard_group(shard)) == backup_group_label(
            weekly_backup_group(shard)
        )
    assert len(lr.SHARDS) == 7


# (claim, target before) -> target after. The r2 list holds `homelab/traefik-acme` only.
# A blank target on an unlisted claim stays blank: the Ansible routing tasks never wrote it.
TARGET_TRANSITIONS = [
    pytest.param(
        "homelab/traefik-acme", "default", "r2", id="listed-claim-moves-to-r2"
    ),
    pytest.param("homelab/traefik-acme", "", "r2", id="listed-claim-with-no-target"),
    pytest.param("homelab/traefik-acme", "r2", "r2", id="listed-claim-already-on-r2"),
    pytest.param(
        "homelab/authelia-config", "r2", "default", id="delisted-claim-returns-to-b2"
    ),
    pytest.param(
        "homelab/authelia-config", "default", "default", id="unlisted-claim-on-b2"
    ),
    pytest.param("homelab/authelia-config", "", "", id="blank-target-stays-blank"),
    pytest.param(
        "homelab/authelia-config", "other", "other", id="unknown-target-is-not-ours"
    ),
]


@pytest.mark.parametrize(("claim", "before", "after"), TARGET_TRANSITIONS)
def test_each_target_transition_writes_the_live_target_name(claim, before, after, env):
    volume = _volume("pvc-1", claim, "default", target=before)
    world = World([volume], [_pvc(c, k) for c, k in PVC_CLASS.items()])
    assert lr.main([], env, world) == 0
    assert world.volumes["pvc-1"]["spec"]["backupTargetName"] == after
    assert len(world.writes()) == (0 if before == after else 1)


def test_a_volume_that_changes_group_and_target_gets_one_patch_carrying_both(env):
    """Labels and target are one merge patch, so a half-applied volume cannot exist."""
    volume = _volume("pvc-1", "homelab/sonarr-config", "default", target="r2")
    world = World([volume], [_pvc(c, k) for c, k in PVC_CLASS.items()])
    assert lr.main([], env, world) == 0
    (write,) = world.writes()
    assert write[1] == f"{VOLUMES}/pvc-1"
    assert world.volumes["pvc-1"]["spec"]["backupTargetName"] == "default"
    assert f"{P}weekly-backup-d3" in world.volumes["pvc-1"]["metadata"]["labels"]


def test_the_target_names_are_the_ones_setup_k3s_and_the_readers_use():
    """The target contract, typed out and held equal to the filter the tasks and the health reader read."""
    assert (lr.B2_TARGET, lr.R2_TARGET) == ("default", "r2")
    assert lr.B2_TARGET == longhorn_backup_name("b2_target")
    assert lr.R2_TARGET == longhorn_backup_name("r2_target")


def test_report_only_names_a_target_change_and_writes_nothing(env, capsys):
    world = World(
        [_volume("pvc-1", "homelab/traefik-acme", "default", target="default")],
        [_pvc(c, k) for c, k in PVC_CLASS.items()],
    )
    assert lr.main(["--report-only"], env, world) == 0
    assert world.writes() == []
    assert (
        "would patch volume pvc-1 (homelab/traefik-acme): backupTargetName=r2"
        in capsys.readouterr().out
    )


def test_a_failed_read_writes_nothing(env, capsys):
    world = World(
        [_volume("pvc-1", "homelab/crowdsec-db", "default")],
        [_pvc(c, k) for c, k in PVC_CLASS.items()],
    )
    world.fail[("GET", "/api/v1/persistentvolumeclaims")] = 403
    assert lr.main([], env, world) == 1
    assert world.writes() == []
    assert "listing PVCs answered HTTP 403: refused" in capsys.readouterr().err


def test_a_refused_label_patch_fails_the_run_naming_the_volume(env, capsys):
    world = World(
        [_volume("pvc-1", "homelab/crowdsec-db", "default")],
        [_pvc(c, k) for c, k in PVC_CLASS.items()],
    )
    world.fail[("PATCH", f"{VOLUMES}/pvc-1")] = 400
    assert lr.main([], env, world) == 1
    assert "patching volume pvc-1 answered HTTP 400" in capsys.readouterr().err


def test_report_only_prints_the_plan_and_writes_nothing(env, capsys):
    world = World(
        [_volume("pvc-1", "homelab/crowdsec-db", "default")],
        [_pvc(c, k) for c, k in PVC_CLASS.items()],
    )
    assert lr.main(["--report-only"], env, world) == 0
    assert world.writes() == []
    out = capsys.readouterr().out
    assert (
        "would patch volume pvc-1 (homelab/crowdsec-db): default- no-backup=enabled"
        in out
    )
    assert "1 volume change(s) planned" in out


def test_a_converged_cluster_reports_nothing_to_change_in_report_only(env, capsys):
    world = World(
        [_volume("pvc-1", "homelab/crowdsec-db", "no-backup")],
        [_pvc(c, k) for c, k in PVC_CLASS.items()],
    )
    assert lr.main(["--report-only"], env, world) == 0
    out = capsys.readouterr().out
    assert "would patch" not in out
    assert "0 volume change(s) planned" in out


def test_a_run_reads_and_writes_no_deployment(env, capsys):
    # fact: ansible/roles/k8s/longhorn-reconcile/CLAUDE.md#Traps
    """The CSI sidecars' request is the `longhorn` role's Setting, and the manager owns it (#5958).

    A second writer here made longhorn-manager clear the request on its next tick and each CSI
    Deployment roll twice an hour, so the run keeps off every Deployment.
    """
    world = World(
        [_volume("pvc-1", "homelab/crowdsec-db", "default")],
        [_pvc(c, k) for c, k in PVC_CLASS.items()],
    )
    assert lr.main([], env, world) == 0
    assert [c for c in world.calls if "/deployments" in c[1]] == []
    assert "deployment" not in capsys.readouterr().out


def _tiers(tmp_path, doc):
    path = tmp_path / "tiers.json"
    path.write_text(json.dumps(doc))
    return path


def test_the_real_tier_shape_is_accepted(tmp_path):
    tiers = lr.load_tiers(_tiers(tmp_path, TIERS))
    assert tiers["weekly"] == TIERS["weekly"]
    assert tiers["nobackup"] == {"homelab/crowdsec-db"}


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ({**TIERS, "weekly": {"homelab/a": 7}}, "not 0-6"),
        ({**TIERS, "weekly": {"homelab/a": True}}, "not 0-6"),
        ({**TIERS, "weekly": ["homelab/a"]}, "must map a claim to its shard"),
        ({**TIERS, "nobackup": "homelab/a"}, "must be lists"),
        (
            {**TIERS, "nobackup": ["homelab/sonarr-config"]},
            "on both the weekly and the no-backup list",
        ),
        ([], "not a JSON object"),
    ],
)
def test_a_tier_document_the_run_cannot_act_on_is_refused(tmp_path, doc, message):
    with pytest.raises(lr.Failed, match=message):
        lr.load_tiers(_tiers(tmp_path, doc))


def test_an_unreadable_tier_file_is_refused(tmp_path):
    with pytest.raises(lr.Failed, match="tier file"):
        lr.load_tiers(tmp_path / "absent.json")


class _Response(io.BytesIO):
    status = 200


def test_a_patch_carries_the_content_type_it_was_given():
    sent = []

    def record(request, *, timeout):
        sent.append(request)
        return _Response(b"{}")

    kube = lr.Kube("https://api", "token", opener=record)
    kube.call("PATCH", "/x", {"a": 1}, "application/strategic-merge-patch+json")
    kube.call("PATCH", "/y", {"a": 1})
    kube.call("GET", "/z")
    assert [r.get_header("Content-type") for r in sent] == [
        "application/strategic-merge-patch+json",
        "application/merge-patch+json",
        None,
    ]
    assert json.loads(sent[0].data) == {"a": 1}
    assert sent[2].data is None


def test_an_error_status_is_returned_as_an_answer():
    def refuse(request, *, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            Message(),
            io.BytesIO(b'{"message":"no"}'),
        )

    assert lr.Kube("https://api", "token", opener=refuse).call("GET", "/x") == (
        403,
        {"message": "no"},
    )


def test_the_script_answers_help_and_exits_zero(capsys, env):
    with pytest.raises(SystemExit) as exit_:
        lr.main(["--help"], env, World([], []))
    assert exit_.value.code == 0
    assert "--report-only" in capsys.readouterr().out
