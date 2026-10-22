"""The longhorn-reconcile script against a fixture API server (H1 plan, H1.6).

`World` answers the script's reads from dicts, applies its merge and strategic patches the way
the API server does, and records every call. The label cases are a transition table with the
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
from collections.abc import Mapping
from email.message import Message

import longhorn_reconcile as lr
import pytest
from longhorn_groups import backup_group_label, weekly_backup_group

NS = "longhorn-system"
VOLUMES = f"/apis/longhorn.io/v1beta2/namespaces/{NS}/volumes"
DEPLOYMENTS = f"/apis/apps/v1/namespaces/{NS}/deployments"
REQUEST = "192Mi"
P = "recurring-job-group.longhorn.io/"

TIERS = {
    "r2": ["homelab/traefik-acme"],
    "weekly": {"homelab/sonarr-config": 3, "homelab/radarr-config": 5},
    "nobackup": ["homelab/crowdsec-db"],
}


def _volume(name: str, claim: str, *groups: str, extra: Mapping | None = None) -> dict:
    namespace, pvc = claim.split("/")
    labels = {f"{P}{group}": "enabled" for group in groups}
    labels.update(extra or {})
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {"kubernetesStatus": {"namespace": namespace, "pvcName": pvc}},
    }


def _pvc(claim: str, storage_class: str) -> dict:
    namespace, name = claim.split("/")
    return {
        "metadata": {"namespace": namespace, "name": name},
        "spec": {"storageClassName": storage_class},
    }


def _deployment(name: str, memory: str | None) -> dict:
    requests = {"memory": memory} if memory else {}
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": name,
                            "image": "x",
                            "resources": {"requests": requests},
                        }
                    ]
                }
            }
        }
    }


class World(lr.Kube):
    """A fixture API server: Volume CRs, PVCs and the four CSI Deployments."""

    def __init__(self, volumes, pvcs, deployments=None):
        super().__init__("https://api", "token")
        self.volumes = {v["metadata"]["name"]: v for v in volumes}
        self.pvcs = pvcs
        self.deployments = (
            {n: _deployment(n, REQUEST) for n in lr.CSI_DEPLOYMENTS}
            if deployments is None
            else deployments
        )
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail: dict[tuple[str, str], int] = {}

    def call(self, method, path, body=None, content_type=lr.MERGE_PATCH):
        self.calls.append((method, path, content_type if body is not None else None))
        if (method, path) in self.fail:
            return self.fail[(method, path)], {"message": "refused"}
        if method == "GET" and path == VOLUMES:
            return 200, {"items": list(self.volumes.values())}
        if method == "GET" and path == "/api/v1/persistentvolumeclaims":
            return 200, {"items": self.pvcs}
        if path.startswith(DEPLOYMENTS + "/"):
            name = path.rsplit("/", 1)[1]
            if name not in self.deployments:
                return 404, {"message": f"deployments {name} not found"}
            if method == "PATCH":
                assert content_type == lr.STRATEGIC_PATCH
                assert body is not None
                container = body["spec"]["template"]["spec"]["containers"][0]
                held = self.deployments[name]["spec"]["template"]["spec"]["containers"][
                    0
                ]
                held["resources"]["requests"].update(container["resources"]["requests"])
            return 200, self.deployments[name]
        if method == "PATCH" and path.startswith(VOLUMES + "/"):
            assert content_type == lr.MERGE_PATCH
            assert body is not None
            labels = self.volumes[path.rsplit("/", 1)[1]]["metadata"]["labels"]
            for key, value in body["metadata"]["labels"].items():
                if value is None:
                    labels.pop(key, None)
                else:
                    labels[key] = value
            return 200, {}
        raise AssertionError(f"unexpected call {method} {path}")

    def writes(self) -> list[tuple[str, str, str | None]]:
        return [call for call in self.calls if call[0] == "PATCH"]


@pytest.fixture
def env(tmp_path):
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps(TIERS))
    return {
        "RECONCILE_NAMESPACE": NS,
        "RECONCILE_CSI_MEMORY_REQUEST": REQUEST,
        "RECONCILE_TIERS_FILE": str(tiers),
        "RECONCILE_CSI_WAIT_SECONDS": "0",
    }


# (claim, class, labels before) -> labels after, the group labels only.
PVC_CLASS = {
    "homelab/crowdsec-db": "longhorn",
    "homelab/sonarr-config": "longhorn",
    "homelab/radarr-config": "longhorn",
    "homelab/prometheus-data": "longhorn-nobackup",
    "homelab/authelia-config": "longhorn",
    "homelab/orphan": "longhorn",
}
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
        ],
        [_pvc(c, k) for c, k in PVC_CLASS.items()],
        {n: _deployment(n, None) for n in lr.CSI_DEPLOYMENTS},
    )
    assert lr.main([], env, world) == 0
    # Four volumes move groups and four Deployments gain the request.
    assert len(world.writes()) == 8
    first = len(world.calls)
    assert lr.main([], env, world) == 0
    assert [c for c in world.calls[first:] if c[0] == "PATCH"] == []
    assert "0 label change(s)" in capsys.readouterr().out


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
        {n: _deployment(n, "64Mi") for n in lr.CSI_DEPLOYMENTS},
    )
    assert lr.main(["--report-only"], env, world) == 0
    assert world.writes() == []
    out = capsys.readouterr().out
    assert (
        "would patch volume pvc-1 (homelab/crowdsec-db): default- no-backup=enabled"
        in out
    )
    assert "would patch deployment csi-attacher: memory request 64Mi -> 192Mi" in out
    assert "1 label change(s) planned" in out


def test_a_converged_cluster_reports_nothing_to_change_in_report_only(env, capsys):
    world = World(
        [_volume("pvc-1", "homelab/crowdsec-db", "no-backup")],
        [_pvc(c, k) for c, k in PVC_CLASS.items()],
    )
    assert lr.main(["--report-only"], env, world) == 0
    out = capsys.readouterr().out
    assert "would patch" not in out
    assert "0 label change(s) planned" in out


def test_a_csi_deployment_without_the_request_is_patched_with_a_strategic_merge(env):
    world = World([], [], {n: _deployment(n, None) for n in lr.CSI_DEPLOYMENTS})
    assert lr.main([], env, world) == 0
    patches = [c for c in world.calls if c[0] == "PATCH"]
    assert [p[1] for p in patches] == [f"{DEPLOYMENTS}/{n}" for n in lr.CSI_DEPLOYMENTS]
    assert {p[2] for p in patches} == {"application/strategic-merge-patch+json"}


def test_the_sidecar_patch_names_its_container_so_the_image_survives():
    assert lr.sidecar_patch("csi-attacher", "192Mi") == {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "csi-attacher",
                            "resources": {"requests": {"memory": "192Mi"}},
                        }
                    ]
                }
            }
        }
    }


def test_a_csi_deployment_that_never_appears_fails_the_run_after_the_wait(env, capsys):
    deployments = {n: _deployment(n, REQUEST) for n in lr.CSI_DEPLOYMENTS[:3]}
    world = World([], [], deployments)
    sleeps = []
    ticks = iter(range(0, 1000, 10))
    lines, problems = lr.reconcile_sidecars(
        world,
        NS,
        REQUEST,
        report_only=False,
        wait_seconds=30,
        sleep=sleeps.append,
        clock=lambda: float(next(ticks)),
    )
    assert problems == ["deployment csi-snapshotter does not exist"]
    assert sleeps and set(sleeps) == {lr.CSI_PAUSE_SECONDS}
    assert len(lines) == 3


def test_a_csi_deployment_that_appears_during_the_wait_is_patched(monkeypatch):
    world = World([], [], {})
    appear = {"left": 2}
    real = world.call

    def call(method, path, body=None, content_type=lr.MERGE_PATCH):
        if method == "GET" and path.endswith("/csi-attacher") and appear["left"] == 0:
            world.deployments.update(
                {n: _deployment(n, None) for n in lr.CSI_DEPLOYMENTS}
            )
        if method == "GET" and path.endswith("/csi-attacher"):
            appear["left"] -= 1
        return real(method, path, body, content_type)

    monkeypatch.setattr(world, "call", call)
    lines, problems = lr.reconcile_sidecars(
        world, NS, REQUEST, report_only=False, wait_seconds=60, sleep=lambda s: None
    )
    assert problems == []
    assert len(lines) == 4 and all(line.startswith("patched") for line in lines)


def test_report_only_does_not_wait_for_an_absent_deployment():
    world = World([], [], {})
    slept = []
    lines, problems = lr.reconcile_sidecars(
        world, NS, REQUEST, report_only=True, wait_seconds=600, sleep=slept.append
    )
    assert slept == []
    assert len(problems) == 4 and lines == []


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
    kube.call("PATCH", "/x", {"a": 1}, lr.STRATEGIC_PATCH)
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
