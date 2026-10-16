"""Snapshot a flux-owned role's claims before Flux applies its revision, then prune the old ones.

The Job `snapshot-<tag>-<spec digest8>` runs this in `flux-system`, as the only work of the
Kustomization `<tag>-snapshot`, which `<tag>` depends on (target slice D5, *Snapshots as a Job
per role revision (D5.2a)*). It keeps every check `ansible/roles/k8s/volume-snapshot/tasks/
claim.yml` makes. Every claim is resolved, named and checked against its snapshot-space cap
before any snapshot is created, so a failing claim leaves nothing behind. Then, per claim, it
creates `autodeploy-<tag>-<digest8>-<claim>-<token>`, waits for `readyToUse`, and prunes that
claim's `autodeploy-<tag>-` snapshots down to the retention window.

A claim whose PVC is absent live is new in this revision when `<tag>`'s `status.inventory`
does not list it, because `<tag>` has never applied it and it holds no data. The Job skips it
and logs the skip. A PVC the inventory lists and the cluster lacks fails the Job, naming it.

The Job's environment carries the inputs, and its spec digest covers them:
`SNAPSHOT_TAG`, `SNAPSHOT_DIGEST8`, `SNAPSHOT_CLAIMS` (a JSON list of `{name, namespace}`),
`SNAPSHOT_RETAIN`, `SNAPSHOT_TIMEOUT` and `SNAPSHOT_POLL`, the last two in seconds. It reaches
the API server with the pod's ServiceAccount token, through `flux_job_connect`, and the API
server's CA reaches `urlopen` through `SSL_CERT_FILE`, which the Job sets. Standard library
only. Exit 0 when every claim is snapshotted or skipped, 1 on the first failure, naming it.

Run: python3 flux_snapshot.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/manifests/tests/test_flux_snapshot.py
"""

import argparse
import json
import os
import sys
import time
import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from flux_job_connect import Request, open_url

API = "https://kubernetes.default.svc"
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
LONGHORN = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
KUSTOMIZATIONS = (
    "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flux-system/kustomizations"
)
LONGHORN_DRIVER = "driver.longhorn.io"
# `volume-snapshot`'s run token: the UTC time the Job's pod starts.
TOKEN_FORMAT = "%Y%m%d%H%M%S"
# Longhorn's delete webhook refuses a longer name through every route, so nothing could prune
# the snapshot (#2734).
NAME_LIMIT = 63
# The retention floor `claim.yml` clamps to: a rollback takes its own snapshot and prunes
# before `volume-revert` reads the chain, so both must survive one run's prune.
RETAIN_FLOOR = 2
# The seconds each API call may take.
CALL_TIMEOUT = 10


class Failed(Exception):
    """A check failed; the message names the claim and what to do."""


@dataclass(frozen=True)
class Claim:
    name: str
    namespace: str


@dataclass(frozen=True)
class Config:
    tag: str
    digest8: str
    claims: tuple[Claim, ...]
    retain: int
    timeout: float
    poll: float

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        claims = json.loads(env["SNAPSHOT_CLAIMS"])
        return cls(
            env["SNAPSHOT_TAG"],
            env["SNAPSHOT_DIGEST8"],
            tuple(Claim(c["name"], c["namespace"]) for c in claims),
            int(env["SNAPSHOT_RETAIN"]),
            float(env["SNAPSHOT_TIMEOUT"]),
            float(env["SNAPSHOT_POLL"]),
        )


class Kube:
    """The API server, as one JSON call per method."""

    def __init__(self, base: str, token: str):
        self.base = base
        self.token = token

    def call(
        self, method: str, path: str, body: Mapping | None = None
    ) -> tuple[int, dict]:
        """`method` on `path`; the HTTP status and the parsed body, an error status included."""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with open_url(request, timeout=CALL_TIMEOUT) as response:
                return response.status, _parse(response.read())
        except urllib.error.HTTPError as err:
            with err:
                return err.code, _parse(err.read())


def _parse(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _ok(status: int, body: dict, what: str) -> dict:
    if status != 200:
        raise Failed(
            f"reading {what} answered HTTP {status}: {body.get('message', '')}"
        )
    return body


def snapshot_name(tag: str, digest8: str, claim: str, token: str) -> str:
    """`autodeploy-<tag>-<digest8>-<claim>-<token>`, `<claim>` less a leading `<tag>-`."""
    short = claim.removeprefix(f"{tag}-") or claim
    return f"autodeploy-{tag}-{digest8}-{short}-{token}"


def inventory(kube: Kube, tag: str) -> set[str] | None:
    """The ids `<tag>`'s `status.inventory` lists, or None when `<tag>` has never applied.

    A 404 or an absent inventory is a tag that has never applied. Any other answer fails, so
    a refused read never reads as "every claim is new".
    """
    status, body = kube.call("GET", f"{KUSTOMIZATIONS}/{tag}")
    if status == 404:
        return None
    entries = (_ok(status, body, f"the Kustomization {tag}").get("status") or {}).get(
        "inventory"
    )
    if entries is None:
        return None
    return {str(e.get("id")) for e in entries.get("entries") or []}


def resolve(kube: Kube, claim: Claim, listed: set[str] | None, log) -> str | None:
    """The Longhorn volume behind `claim`, or None for a claim new in this revision."""
    label = f"{claim.namespace}/{claim.name}"
    status, pvc = kube.call(
        "GET",
        f"/api/v1/namespaces/{claim.namespace}/persistentvolumeclaims/{claim.name}",
    )
    if status == 404:
        if (
            listed is not None
            and f"{claim.namespace}_{claim.name}__PersistentVolumeClaim" in listed
        ):
            raise Failed(
                f"PVC {label} is gone, and the Kustomization's inventory lists it, so a claim "
                "that held data has been deleted. Stopping before the apply"
            )
        log(
            f"skip {label}: absent live and from the inventory, so it is new in this revision"
        )
        return None
    volume = (_ok(status, pvc, f"PVC {label}").get("spec") or {}).get("volumeName")
    if not volume:
        raise Failed(f"PVC {label} has no spec.volumeName: it is still Pending")
    status, pv = kube.call("GET", f"/api/v1/persistentvolumes/{volume}")
    csi = (_ok(status, pv, f"PV {volume}").get("spec") or {}).get("csi") or {}
    if csi.get("driver") != LONGHORN_DRIVER or not csi.get("volumeHandle"):
        raise Failed(f"PV {volume} of PVC {label} is not a Longhorn volume")
    return str(csi["volumeHandle"])


def _snapshots(kube: Kube) -> list[dict] | None:
    status, body = kube.call("GET", f"{LONGHORN}/snapshots")
    return body.get("items") or [] if status == 200 else None


def _bytes(value) -> int:
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() else 0


def check_cap(kube: Kube, volume: str, label: str, log) -> None:
    """Fail only when the volume's numbers positively say its snapshot-space cap is full.

    A reached cap latches, because Longhorn refuses new snapshots rather than pruning, so
    without this check every later revision would hold on a bare readiness timeout.
    """
    status, body = kube.call("GET", f"{LONGHORN}/volumes/{volume}")
    # DECIDED: this gate FAILS OPEN, as `claim.yml`'s marker on the same gate explains. `"0"`
    # is Longhorn's uncapped value and the fleet default, an absent field reads empty, and a
    # read that fails or matches nothing reads as no cap. A gate in front of every snapshot
    # role, guarding a hazard nobody can reach today, must never turn a bad read into a
    # fleet-wide deploy stop.
    cap = (
        _bytes((body.get("spec") or {}).get("snapshotMaxSize")) if status == 200 else 0
    )
    if not cap:
        return
    used = sum(
        _bytes((s.get("status") or {}).get("size"))
        for s in _snapshots(kube) or []
        if (s.get("spec") or {}).get("volume") == volume
        and s["metadata"]["name"] != "volume-head"
        and not (s.get("status") or {}).get("markRemoved")
    )
    if used >= cap:
        raise Failed(
            f"Longhorn volume {volume} ({label}) is at its snapshot-space cap: {used} bytes "
            f"of snapshots against a spec.snapshotMaxSize of {cap} bytes, {max(cap - used, 0)} "
            "bytes of headroom. Longhorn refuses new snapshots rather than pruning. Delete "
            "some of this volume's snapshots, then delete this failed Job so Flux creates it "
            "again; Flux never re-runs a failed Job under the same name"
        )
    log(f"{label}: {used} of {cap} snapshot bytes used")


def create(kube: Kube, name: str, volume: str) -> None:
    """Create the Snapshot CR `name`; an existing one fails and is never adopted."""
    body = {
        "apiVersion": "longhorn.io/v1beta2",
        "kind": "Snapshot",
        "metadata": {"name": name, "namespace": "longhorn-system"},
        "spec": {"volume": volume, "createSnapshot": True},
    }
    status, answer = kube.call("POST", f"{LONGHORN}/snapshots", body)
    if status == 409:
        raise Failed(
            f"Snapshot CR longhorn-system/{name} already exists. It is another run's recovery "
            "point or a markRemoved one, so it is never adopted. The Job's retry starts at "
            "least a second later and takes another token"
        )
    if status not in (200, 201):
        raise Failed(
            f"creating Snapshot {name} answered HTTP {status}: {answer.get('message', '')}"
        )


def wait_ready(kube: Kube, name: str, volume: str, cfg: Config, sleep, clock) -> None:
    deadline = clock() + cfg.timeout
    while True:
        status, body = kube.call("GET", f"{LONGHORN}/snapshots/{name}")
        # A refused or vanished read is no readiness answer, so it fails at once, naming the
        # status, rather than polling out to a timeout that blames Longhorn.
        state = _ok(status, body, f"Snapshot {name}").get("status") or {}
        if state.get("readyToUse") is True:
            return
        if clock() >= deadline:
            _, vol = kube.call("GET", f"{LONGHORN}/volumes/{volume}")
            raise Failed(
                f"Snapshot {name} of volume {volume} did not report readyToUse within "
                f"{cfg.timeout:g}s (readyToUse={state.get('readyToUse')}, "
                f"markRemoved={state.get('markRemoved')}, volume state "
                f"{(vol.get('status') or {}).get('state') or 'unknown'}). Stopping before "
                "the apply, which would otherwise run with no way back"
            )
        sleep(cfg.poll)


def prune(kube: Kube, name: str, volume: str, cfg: Config, log) -> None:
    """Delete this claim's live `autodeploy-<tag>-` snapshots beyond the retention window.

    A `markRemoved` CR is already being deleted, so it never counts toward the window.
    """
    items = _snapshots(kube)
    if items is None:
        raise Failed(
            f"listing the snapshots of volume {volume} failed; nothing was deleted"
        )
    prefix = f"autodeploy-{cfg.tag}-"
    live = sorted(
        (
            s
            for s in items
            if (s.get("spec") or {}).get("volume") == volume
            and s["metadata"]["name"].startswith(prefix)
            and not (s.get("status") or {}).get("markRemoved")
        ),
        key=lambda s: (
            s["metadata"].get("creationTimestamp") or "",
            s["metadata"]["name"],
        ),
        reverse=True,
    )
    names = [s["metadata"]["name"] for s in live]
    if name not in names:
        raise Failed(
            f"the snapshot listing for volume {volume} lacks {name}, which this run just "
            f"created and waited for. The listing is the input to a delete, so nothing was "
            f"deleted. It read {len(names)} live snapshot(s) with prefix {prefix}"
        )
    for old in names[max(cfg.retain, RETAIN_FLOOR) :]:
        status, body = kube.call("DELETE", f"{LONGHORN}/snapshots/{old}")
        if status not in (200, 202, 404):
            raise Failed(
                f"deleting Snapshot {old} answered HTTP {status}: {body.get('message', '')}"
            )
        log(f"pruned {old}")


def run(
    kube: Kube,
    cfg: Config,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> None:
    """Snapshot every claim of `cfg`; raise `Failed` on the first check that fails."""
    token = now().strftime(TOKEN_FORMAT)
    listed = inventory(kube, cfg.tag)
    planned = []
    for claim in cfg.claims:
        label = f"{claim.namespace}/{claim.name}"
        name = snapshot_name(cfg.tag, cfg.digest8, claim.name, token)
        if len(name.encode()) > NAME_LIMIT:
            raise Failed(
                f"the snapshot name {name} is {len(name.encode())} bytes, over Longhorn's "
                f"{NAME_LIMIT}-byte delete limit; shorten the tag or the claim name"
            )
        volume = resolve(kube, claim, listed, log)
        if volume is None:
            continue
        check_cap(kube, volume, label, log)
        planned.append((name, volume, label))
    for name, volume, label in planned:
        create(kube, name, volume)
        wait_ready(kube, name, volume, cfg, sleep, clock)
        prune(kube, name, volume, cfg, log)
        log(f"{label}: {name} is ready")


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    cfg = Config.from_env(os.environ if env is None else env)
    kube = Kube(API, Path(TOKEN_FILE).read_text().strip())
    try:
        run(kube, cfg)
    except Failed as err:
        print(f"snapshot-{cfg.tag}: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
