"""The admission reaper's rule, driven through main() against a recording fake of the API server.

Run: uv run pytest ansible/roles/k8s/dri-device-plugin/tests/test_dri_admission_reaper.py
"""

import dri_admission_reaper


def pod(name, phase="Failed", reason="UnexpectedAdmissionError", owner="ReplicaSet"):
    owners = [{"kind": owner, "name": f"{name}-owner"}] if owner else []
    return {
        "metadata": {"name": name, "uid": f"uid-{name}", "ownerReferences": owners},
        "status": {"phase": phase, "reason": reason},
    }


class FakeApi(dri_admission_reaper.Kube):
    """Serves one pod list and answers every delete with `delete_status`."""

    def __init__(self, pods, delete_status=200):
        super().__init__("https://api", "token")
        self.pods = pods
        self.delete_status = delete_status
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return 200, {"items": self.pods}
        return self.delete_status, {"message": "nope"}


def run(api):
    return dri_admission_reaper.main([], {"REAPER_NAMESPACE": "homelab"}, api)


def deleted(api):
    return [
        path.rsplit("/", 1)[1] for method, path, _ in api.calls if method == "DELETE"
    ]


def test_a_replicaset_pod_rejected_at_admission_is_deleted_with_a_uid_precondition(
    capsys,
):
    api = FakeApi(
        [pod("tdarr-6bd756fd89-pvwm5"), pod("jellyfin-1", reason="OutOfdevic.es/dri")]
    )
    assert run(api) == 0
    assert deleted(api) == ["tdarr-6bd756fd89-pvwm5", "jellyfin-1"]
    assert api.calls[1][2]["preconditions"] == {"uid": "uid-tdarr-6bd756fd89-pvwm5"}
    assert "fieldSelector=status.phase%3DFailed" in api.calls[0][1]
    assert capsys.readouterr().out.splitlines()[-1] == (
        "dri-admission-reaper: deleted 2 of 2 Failed pods in homelab"
    )


def test_job_pods_evicted_pods_and_running_pods_are_left_alone():
    api = FakeApi(
        [
            pod("crowdsec-home-ips-29881895-b4kd7", reason=None, owner="Job"),
            pod("gate-run", owner="Job"),
            pod("sonarr-evicted", reason="Evicted"),
            pod("orphan", owner=None),
            pod("tdarr-live", phase="Running"),
        ]
    )
    assert run(api) == 0
    assert deleted(api) == []


def test_a_pod_already_gone_or_replaced_is_not_an_error(capsys):
    for status in (404, 409):
        api = FakeApi([pod("tdarr-1")], delete_status=status)
        assert run(api) == 0
        assert capsys.readouterr().out.endswith(
            "deleted 0 of 1 Failed pods in homelab\n"
        )


def test_a_refused_delete_fails_the_run(capsys):
    api = FakeApi([pod("tdarr-1")], delete_status=403)
    assert run(api) == 1
    assert "deleting pod tdarr-1 answered HTTP 403: nope" in capsys.readouterr().err
