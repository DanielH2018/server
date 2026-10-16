"""One fixture promotion for the soak Job's tests: authelia changed and sonarr did not.

`World` holds the cluster and Prometheus the script reads, T0 is fixed, and the clock moves
only when the script sleeps. `run` drives `flux_soak.main` over it.
"""

import copy
import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime

import flux_soak

R, OTHER = "a" * 40, "b" * 40
T0 = 1_800_000_000.0
SCRAPE = 30
SERVICES = {
    "authelia": {"for": 300, "namespaces": ["authelia"]},
    "sonarr": {"for": 3600, "namespaces": ["media"]},
}
# W for authelia alone: max(60, 300) + 120.
W = 420


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat().replace("+00:00", "Z")


def tarball(digests) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = json.dumps(digests).encode()
        info = tarfile.TarInfo("digests.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def source(name: str, revision: str, data: bytes, verified: bool = True) -> dict:
    return {
        "metadata": {"name": name, "generation": 1},
        "status": {
            "observedGeneration": 1,
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "SourceVerified", "status": str(verified)},
            ],
            "artifact": {
                "revision": "prod@sha256:" + "0" * 64,
                "metadata": {flux_soak.ORIGIN_KEY: f"master@sha1:{revision}"},
                "lastUpdateTime": iso(T0),
                "url": f"http://source-controller/{name}.tar.gz",
                "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
            },
        },
    }


def kustomization(name, revision=R, ready=True, depends=(), suspend=False) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"dependsOn": [{"name": d} for d in depends], "suspend": suspend},
        "status": {
            "lastAppliedOriginRevision": f"master@sha1:{revision}",
            "conditions": [{"type": "Ready", "status": str(ready)}],
        },
    }


def pod(namespace, name, created=T0 + 10, ready=T0 + 60, owner=("ReplicaSet", None)):
    kind, owner_name = owner
    return {
        "metadata": {
            "namespace": namespace,
            "name": name,
            "uid": name,
            "creationTimestamp": iso(created),
            "ownerReferences": [
                {"kind": kind, "name": owner_name or f"{name}-rs", "controller": True}
            ],
        },
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True", "lastTransitionTime": iso(ready)}
            ],
            "containerStatuses": [{"name": "app", "restartCount": 0, "lastState": {}}],
        },
    }


def restart(p: dict, at: float) -> None:
    status = p["status"]["containerStatuses"][0]
    status["restartCount"] += 1
    status["lastState"] = {"terminated": {"finishedAt": iso(at)}}


class World:
    """One promotion's cluster and Prometheus, read through `reads()`."""

    def __init__(self):
        self.new = {"authelia": {"footprint": "2"}, "sonarr": {"footprint": "1"}}
        self.old = {"authelia": {"footprint": "1"}, "sonarr": {"footprint": "1"}}
        self.base_verified = True
        self.base_revision = OTHER
        self.homelab_revision = R
        self.kustomizations = [
            kustomization("authelia"),
            kustomization("sonarr"),
            kustomization(flux_soak.SOAK),
        ]
        self.pods = {"authelia": [pod("authelia", "authelia-1")], "media": []}
        self.replicasets = {}
        self.forbidden = set()
        self.unmapped = set()
        self.ready_samples = None
        self.scrape_gaps = []
        self.alerts = []
        self.pending_alerts = []
        self.firing_at_t0 = []
        self.prometheus_started = T0 - 86400
        self.now = T0 + 300
        self.sleeps = []
        self.on_sleep = lambda world: None
        self.reads_of = []

    def _sources(self):
        new, old = tarball(self.new), tarball(self.old)
        self.artifacts = {
            "http://source-controller/homelab.tar.gz": new,
            "http://source-controller/last-good.tar.gz": old,
        }
        return [
            source("homelab", self.homelab_revision, new),
            source("last-good", self.base_revision, old, self.base_verified),
        ]

    def _pods(self):
        return [p for ps in self.pods.values() for p in ps]

    def api(self, path):
        self.reads_of.append(path)
        if path == flux_soak.OCIREPOSITORIES:
            return {"items": self._sources()}
        if path == flux_soak.KUSTOMIZATIONS:
            return {"items": copy.deepcopy(self.kustomizations)}
        if path.endswith("/pods"):
            return {"items": copy.deepcopy(self.pods.get(path.split("/")[4], []))}
        if path in self.forbidden:
            raise flux_soak.SoakError(f"{path}: HTTP 403")
        name = path.rsplit("/", 1)[1]
        owner = self.replicasets.get(name, {"kind": "Deployment", "name": name[:-3]})
        return {
            "metadata": {
                "ownerReferences": [{**owner, "controller": True}] if owner else []
            }
        }

    def _matrix(self, values_of):
        out = []
        for p in self._pods():
            meta = p["metadata"]
            labels = {"namespace": meta["namespace"], "pod": meta["name"]}
            values = values_of(p)
            if values:
                out.append({"metric": labels, "values": values})
        return out

    def query(self, promql, at):
        if promql.startswith("homelab:pod_service:info"):
            return [
                {
                    "metric": {
                        "namespace": p["metadata"]["namespace"],
                        "pod": p["metadata"]["name"],
                        "service": p["metadata"]["name"].rsplit("-", 1)[0],
                    }
                }
                for p in self._pods()
                if p["metadata"]["name"] not in self.unmapped
            ]
        if promql.startswith("kube_pod_status_ready_time"):
            default = lambda p: [[T0 + 70, str(T0 + 60)]]
            return self._matrix(self.ready_samples or default)
        if promql.startswith("kube_pod_container_status_restarts_total"):
            series = self._matrix(
                lambda p: [
                    [t, "0"]
                    for t in range(int(T0), int(self.now), SCRAPE)
                    if not any(lo <= t <= hi for lo, hi in self.scrape_gaps)
                ]
            )
            for s in series:
                s["metric"]["container"] = "app"
            return series
        if promql.startswith("last_over_time"):
            assert at == T0
            return [
                {"metric": {**m, "alertstate": "firing"}, "value": [T0, "1"]}
                for m in self.firing_at_t0
            ]
        if promql.startswith("ALERTS{"):
            return [
                {"metric": {**m, "alertstate": "firing"}, "value": [self.now, "1"]}
                for m, _ in self.alerts
            ]
        if promql.startswith("ALERTS_FOR_STATE"):
            return [
                {"metric": m, "value": [self.now, str(active)]}
                for m, active in self.alerts + self.pending_alerts
            ]
        raise AssertionError(promql)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        self.on_sleep(self)

    def reads(self):
        return flux_soak.Reads(
            api=self.api,
            fetch=lambda url: self.artifacts[url],
            query=self.query,
            prometheus_start=lambda: self.prometheus_started,
            clock=lambda: self.now,
            sleep=self.sleep,
        )


def run(world, tmp_path, wait_seconds=600):
    services = tmp_path / "services.json"
    services.write_text(json.dumps(SERVICES))
    log = tmp_path / "termination-log"
    code = flux_soak.main(
        [
            "--revision",
            R,
            "--services",
            str(services),
            "--stabilise-seconds",
            "60",
            "--scrape-seconds",
            str(SCRAPE),
            "--wait-seconds",
            str(wait_seconds),
            "--prometheus",
            "http://prometheus:9090",
            "--termination-log",
            str(log),
        ],
        reads=world.reads(),
    )
    return code, log.read_text() if log.exists() else ""


def alert(service="authelia", severity="critical", **labels):
    return {"alertname": "Down", "service": service, "severity": severity, **labels}
