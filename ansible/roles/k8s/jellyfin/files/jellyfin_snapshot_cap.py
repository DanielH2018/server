"""jellyfin's snapshot-cap Job: set `spec.snapshotMaxSize` on the Longhorn volume behind its claim.

Beside the verify Job in `templates/verify/` (target slice
D5, *jellyfin's cap* and *Post-apply mutation Jobs share `<tag>-verify`*). It replaces the
three cap tasks in `tasks/main.yml`, and makes the same three calls they make through `kubectl`:
read the claim's `spec.volumeName`, read the volume's cap, and patch the cap only when it
differs. So a second run reads twice and writes nothing, which
`tests/test_jellyfin_snapshot_cap.py::test_a_second_run_changes_nothing` proves.

The volume's name is Longhorn's `pvc-<uid>`, which the renderer cannot know, so the Job
resolves it at run time and its Role grants `get` and `patch` on every Longhorn volume.

It talks to the API server with the pod's ServiceAccount token, through `flux_job_connect`,
and the API server's CA reaches `urlopen` through `SSL_CERT_FILE`, which the Job sets.
Inputs come from the Job's environment: `CAP_NAMESPACE` and `CAP_CLAIM` name the claim, and
`CAP_MAX_SIZE` is the cap in bytes as Longhorn stores it, a decimal string.
`JELLYFIN_DEPLOYMENT_DIGEST8` is in the spec only to rename the Job, and the script prints it.
Exit 0 when the cap holds the value, 1 when a call fails, naming it.

Run: python3 jellyfin_snapshot_cap.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/jellyfin/tests/test_jellyfin_snapshot_cap.py
"""

import argparse
import json
import os
import sys
import urllib.error
from collections.abc import Callable, Mapping
from pathlib import Path

from flux_job_connect import Request, open_url

API = "https://kubernetes.default.svc"
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
VOLUMES = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes"
# The seconds each API call may take.
CALL_TIMEOUT = 10


class Failed(Exception):
    """A call failed; the message names the object and the answer."""


class Kube:
    """The API server, as one JSON call per method.

    Attributes:
        base: The API server's URL.
        token: The ServiceAccount token sent as a bearer credential.
        opener: Opens a `Request`; `open_url` unless a test passes its own.
    """

    def __init__(self, base: str, token: str, opener: Callable = open_url):
        self.base = base
        self.token = token
        self.opener = opener

    def call(
        self, method: str, path: str, body: Mapping | None = None
    ) -> tuple[int, dict]:
        """`method` on `path`; the HTTP status and the parsed body, an error status included.

        A body goes as a JSON merge patch, the only write this script makes. The API server
        answers a PATCH sent as plain `application/json` with 415.
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/merge-patch+json"
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener(request, timeout=CALL_TIMEOUT) as response:
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
        raise Failed(f"{what} answered HTTP {status}: {body.get('message', '')}")
    return body


def volume_name(kube: Kube, namespace: str, claim: str) -> str:
    """The Longhorn volume bound to `claim`.

    The Job runs in `<tag>-verify`, which starts once jellyfin's Kustomization is Ready, so
    the claim is already Bound. An empty `volumeName` fails the Job rather than waiting.
    """
    path = f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim}"
    status, body = kube.call("GET", path)
    name = (_ok(status, body, f"PVC {claim}").get("spec") or {}).get("volumeName")
    if not name:
        raise Failed(f"PVC {claim} has no volumeName yet, so it is not Bound")
    return name


def apply_cap(kube: Kube, volume: str, want: str) -> str:
    """Patch `volume`'s `spec.snapshotMaxSize` to `want` unless it already holds it."""
    path = f"{VOLUMES}/{volume}"
    status, body = kube.call("GET", path)
    have = str(
        (_ok(status, body, f"volume {volume}").get("spec") or {}).get(
            "snapshotMaxSize", ""
        )
    )
    if have == want:
        return f"volume {volume} already caps snapshots at {want}"
    status, body = kube.call("PATCH", path, {"spec": {"snapshotMaxSize": want}})
    _ok(status, body, f"patching volume {volume}")
    return f"volume {volume} capped snapshots at {want}, was {have or 'unset'}"


def run(kube: Kube, env: Mapping[str, str]) -> list[str]:
    """Resolve the claim's volume and hold its cap, and return one report line each.

    Raises:
        Failed: A call answered with an error, or the claim is not Bound.
    """
    volume = volume_name(kube, env["CAP_NAMESPACE"], env["CAP_CLAIM"])
    return [
        "capping for jellyfin's Deployment "
        f"{env.get('JELLYFIN_DEPLOYMENT_DIGEST8', 'unknown')}",
        f"PVC {env['CAP_CLAIM']} is bound to volume {volume}",
        apply_cap(kube, volume, env["CAP_MAX_SIZE"]),
    ]


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    kube: Kube | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    if kube is None:
        kube = Kube(API, Path(TOKEN_FILE).read_text().strip())
    try:
        lines = run(kube, os.environ if env is None else env)
    except Failed as err:
        print(f"jellyfin-snapshot-cap: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"jellyfin-snapshot-cap: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
