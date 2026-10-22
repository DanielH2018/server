"""The fixture API server and builders the longhorn-reconcile script tests share.

Not a test module: `test_longhorn_reconcile.py` and the cases beside it import this. `World`
answers the script's reads from dicts, applies its merge and strategic patches the way the API
server does (a patch of `metadata.labels` and of `spec`), and records every call.
"""

from collections.abc import Mapping

import longhorn_reconcile as lr

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


def _volume(
    name: str,
    claim: str,
    *groups: str,
    extra: Mapping | None = None,
    target: str | None = None,
) -> dict:
    namespace, pvc = claim.split("/")
    labels = {f"{P}{group}": "enabled" for group in groups}
    labels.update(extra or {})
    volume = {
        "metadata": {"name": name, "labels": labels},
        "status": {"kubernetesStatus": {"namespace": namespace, "pvcName": pvc}},
    }
    if target is not None:
        volume["spec"] = {"backupTargetName": target}
    return volume


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
            volume = self.volumes[path.rsplit("/", 1)[1]]
            labels = volume["metadata"]["labels"]
            for key, value in body.get("metadata", {}).get("labels", {}).items():
                if value is None:
                    labels.pop(key, None)
                else:
                    labels[key] = value
            volume.setdefault("spec", {}).update(body.get("spec", {}))
            return 200, {}
        raise AssertionError(f"unexpected call {method} {path}")

    def writes(self) -> list[tuple[str, str, str | None]]:
        return [call for call in self.calls if call[0] == "PATCH"]


# Each claim's StorageClass, for the `longhorn`-class return-to-default case.
PVC_CLASS = {
    "homelab/crowdsec-db": "longhorn",
    "homelab/sonarr-config": "longhorn",
    "homelab/radarr-config": "longhorn",
    "homelab/prometheus-data": "longhorn-nobackup",
    "homelab/authelia-config": "longhorn",
    "homelab/orphan": "longhorn",
}
