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


class PerNamespaceApi(FakeApi):
    """Serves each namespace its own pod list; a namespace in `refused` answers 403."""

    def __init__(self, pods_by_namespace, refused=()):
        super().__init__([])
        self.pods_by_namespace = pods_by_namespace
        self.refused = refused

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        namespace = path.split("/")[4]
        if namespace in self.refused:
            return 403, {"message": "forbidden"}
        if method == "GET":
            return 200, {"items": self.pods_by_namespace.get(namespace, [])}
        return 200, {}


def run_with_addons(api):
    env = {
        "REAPER_NAMESPACE": "homelab",
        "REAPER_JOB_NAMESPACES": "kube-system longhorn-system",
    }
    return dri_admission_reaper.main([], env, api)


def test_a_failed_job_pod_is_deleted_in_an_addon_namespace_and_kept_in_homelab(capsys):
    api = PerNamespaceApi(
        {
            "homelab": [pod("gate-run", reason=None, owner="Job")],
            "kube-system": [
                pod("helm-install-traefik-x7k2p", reason=None, owner="Job"),
                pod("coredns-1", reason="Evicted"),
            ],
            "longhorn-system": [
                pod("backup-weekly-29881895-b4kd7", reason=None, owner="Job")
            ],
        }
    )
    assert run_with_addons(api) == 0
    assert deleted(api) == [
        "helm-install-traefik-x7k2p",
        "backup-weekly-29881895-b4kd7",
    ]
    out = capsys.readouterr().out
    assert "deleted kube-system/helm-install-traefik-x7k2p (no reason)" in out
    assert [line for line in out.splitlines() if " Failed pods in " in line] == [
        "dri-admission-reaper: deleted 0 of 1 Failed pods in homelab",
        "dri-admission-reaper: deleted 1 of 2 Failed pods in kube-system",
        "dri-admission-reaper: deleted 1 of 1 Failed pods in longhorn-system",
    ]


def test_a_refused_namespace_fails_the_run_but_the_others_are_still_reaped(capsys):
    api = PerNamespaceApi(
        {"longhorn-system": [pod("backup-1", owner="Job")]}, refused={"kube-system"}
    )
    assert run_with_addons(api) == 1
    assert deleted(api) == ["backup-1"]
    assert "listing Failed pods answered HTTP 403: forbidden" in capsys.readouterr().err
