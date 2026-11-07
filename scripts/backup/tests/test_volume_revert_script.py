"""The revert script's refusals and its whole call sequence, against a fake cluster."""

import json

import pytest
import volume_revert
from lib.exit_codes import FAILED, OK, USAGE_ERROR
from lib.operator_kubectl import KubectlFailed

NODE = "daniel-box"
SHA = "4a3ada11"
ARGS = [
    "--service",
    "app",
    "--namespace",
    "apps",
    "--sha",
    SHA,
    "--claim",
    "app-config",
]
READY_POD = {
    "spec": {"nodeName": NODE},
    "status": {
        "phase": "Running",
        "podIP": "10.42.0.9",
        "containerStatuses": [{"ready": True}],
    },
}


def snapshot(name: str, created: str, volume: str = "pvc-1", removed: bool = False):
    return {
        "metadata": {"name": name, "creationTimestamp": created},
        "spec": {"volume": volume},
        "status": {"markRemoved": removed},
    }


NEWEST = f"autodeploy-app-{SHA}-config-20261023144142"


class Cluster:
    """A cluster whose volume moves through the states the script's own calls cause."""

    def __init__(self, snapshots=None, pods=(READY_POD,), frontend_off=True):
        self.snapshots = (
            [snapshot(NEWEST, "2026-10-23T14:41:42Z")]
            if snapshots is None
            else snapshots
        )
        self.pods = list(pods)
        self.frontend_off = frontend_off
        self.state = "attached"
        self.calls = []

    def kubectl(self, *args, stdin=None):
        verb = args[2]
        if verb == "scale":
            self.calls.append(("scale", args))
            self.state = "detached"
            return ""
        kind = args[3]
        if kind == "pod":
            return json.dumps({"items": self.pods})
        if kind == "pvc":
            return {"app-config": "pvc-1", "app-data": "pvc-2"}.get(args[4], "")
        if kind == "snapshots.longhorn.io":
            return json.dumps({"items": self.snapshots})
        if kind == "volumes.longhorn.io":
            self.calls.append(("read", self.state))
            return json.dumps(
                {
                    "status": {"state": self.state},
                    "spec": {"disableFrontend": self.frontend_off},
                }
            )
        raise AssertionError(args)

    def post(self, base, volume, action, body):
        self.calls.append((action, base, volume, body))
        self.state = {"attach": "attached", "detach": "detached"}.get(
            action, self.state
        )


def run(cluster: Cluster, argv: list[str]) -> int:
    return volume_revert.main(
        argv,
        kubectl=cluster.kubectl,
        post=cluster.post,
        sleep=lambda _: None,
        hostname=lambda: f"{NODE}.lan",
    )


def test_an_apply_runs_the_measured_sequence_in_order():
    cluster = Cluster()
    assert run(cluster, [*ARGS, "--apply"]) == OK
    base = "http://10.42.0.9:9500"
    assert cluster.calls == [
        (
            "scale",
            ("-n", "apps", "scale", "deployment/app", "--replicas=0"),
        ),
        ("read", "detached"),
        ("attach", base, "pvc-1", {"hostId": NODE, "disableFrontend": True}),
        ("read", "attached"),
        ("snapshotRevert", base, "pvc-1", {"name": NEWEST}),
        ("detach", base, "pvc-1", {}),
        ("read", "detached"),
    ]


def test_a_dry_run_names_the_snapshot_and_changes_nothing(capsys):
    cluster = Cluster()
    assert run(cluster, ARGS) == OK
    assert NEWEST in capsys.readouterr().out
    assert cluster.calls == []


@pytest.mark.parametrize(
    "snapshots",
    [
        [],
        [snapshot(NEWEST, "2026-10-23T14:41:42Z", removed=True)],
        [snapshot(NEWEST, "2026-10-23T14:41:42Z", volume="pvc-other")],
        [snapshot(f"autodeploy-app-{SHA}-configmap-1", "2026-10-23T14:41:42Z")],
    ],
    ids=["none", "markRemoved", "another-volume", "longer-claim-name"],
)
def test_no_usable_snapshot_stops_before_the_scale_down(snapshots, capsys):
    cluster = Cluster(snapshots=snapshots)
    assert run(cluster, [*ARGS, "--apply"]) == FAILED
    assert cluster.calls == []
    assert "no snapshot named" in capsys.readouterr().err


def test_the_newest_snapshot_is_chosen_by_timestamp_not_name():
    items = [
        snapshot(f"autodeploy-app-{SHA}-config-9", "2026-10-01T00:00:00Z"),
        snapshot(f"autodeploy-app-{SHA}-config-1", "2026-10-02T00:00:00Z"),
    ]
    found = volume_revert.newest_snapshot(
        items, "pvc-1", volume_revert.snapshot_prefix("app", SHA, "app-config")
    )
    assert found == f"autodeploy-app-{SHA}-config-1"


def test_the_second_claims_missing_snapshot_leaves_the_service_up():
    cluster = Cluster()
    argv = [*ARGS, "--claim", "app-data", "--apply"]
    assert run(cluster, argv) == FAILED
    assert cluster.calls == []


def test_an_unbound_claim_stops_before_the_scale_down(capsys):
    cluster = Cluster()
    argv = ["--service", "app", "--sha", SHA, "--claim", "pending", "--apply"]
    assert run(cluster, argv) == FAILED
    assert cluster.calls == []
    assert "no spec.volumeName" in capsys.readouterr().err


def test_no_ready_manager_on_this_node_stops_before_the_scale_down(capsys):
    other = {**READY_POD, "spec": {"nodeName": "daniel-server"}}
    cluster = Cluster(pods=[other])
    assert run(cluster, [*ARGS, "--apply"]) == FAILED
    assert cluster.calls == []
    assert "no ready longhorn-manager" in capsys.readouterr().err


def test_a_live_frontend_stops_before_the_revert_and_says_the_service_is_down(capsys):
    cluster = Cluster(frontend_off=False)
    assert run(cluster, [*ARGS, "--apply"]) == FAILED
    assert not [c for c in cluster.calls if c[0] == "snapshotRevert"]
    assert "at zero replicas" in capsys.readouterr().err


def test_a_failed_read_after_the_scale_down_says_the_service_is_down(capsys):
    cluster = Cluster()
    real = cluster.kubectl

    def failing(*args, stdin=None):
        if args[2] != "scale" and args[3] == "volumes.longhorn.io":
            raise KubectlFailed("the API server went away")
        return real(*args, stdin=stdin)

    cluster.kubectl = failing
    assert run(cluster, [*ARGS, "--apply"]) == FAILED
    assert "at zero replicas" in capsys.readouterr().err


def test_a_state_that_never_arrives_fails_the_wait():
    ticks = iter(range(0, 1000, 50))
    cluster = Cluster()
    revert = volume_revert.Revert(
        cluster.kubectl, sleep=lambda _: None, clock=lambda: next(ticks)
    )
    with pytest.raises(volume_revert.RevertError, match="reads 'attached'"):
        revert.wait_for_state("pvc-1", "detached")


@pytest.mark.parametrize("sha", ["4a3ada1", "4A3ADA11", "4a3ada1z"])
def test_a_sha_of_the_wrong_shape_is_a_usage_error(sha):
    argv = ["--service", "app", "--sha", sha, "--claim", "app-config"]
    assert volume_revert.main(argv, kubectl=Cluster().kubectl) == USAGE_ERROR


def test_a_nine_character_sha_is_used_verbatim():
    prefix = volume_revert.snapshot_prefix("app", "4a3ada11f", "app-config")
    assert prefix == "autodeploy-app-4a3ada11f-config-"


class Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_post_action_accepts_only_a_200_and_sends_the_json_body():
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        return Response(200)

    volume_revert.post_action("http://ip:9500", "pvc-1", "detach", {}, urlopen)
    assert requests[0].full_url == "http://ip:9500/v1/volumes/pvc-1?action=detach"
    assert requests[0].data == b"{}"
    assert requests[0].method == "POST"

    with pytest.raises(volume_revert.RevertError, match="HTTP 204"):
        volume_revert.post_action(
            "http://ip:9500", "pvc-1", "detach", {}, lambda r, timeout: Response(204)
        )
