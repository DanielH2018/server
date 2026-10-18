"""pihole's sibling guard: hold instance 1's Flux roll while pihole-2 has no ready DNS endpoint.

The Flux form of `tasks/roll_one.yml`'s sibling check, inert until pihole's flip, in
`templates/guard-inert/` (target slice D5, issue #5695). Under Flux the `pihole` Kustomization
rolls instance 1 whatever state instance 2 is in, and `pihole` cannot wait on `pihole-deferred`
without a cycle. This Job sits in front of `pihole` instead: while it fails, Flux holds `pihole`,
so instance 1 keeps serving at its old revision rather than rolling into a LAN-wide outage.

It reads two EndpointSlice lists, one per web Service, because each of those Services selects
exactly one instance; `pihole-dns` selects both, so instance 1 would satisfy it. An endpoint
counts as serving when its `ready` condition is not false and it is not `terminating`, so a
sibling inside its grace period counts as gone, as `roll_one.yml`'s `deletionTimestamp` check
does.

- No EndpointSlice for `pihole` passes at once. That is a first apply, or a rebuild, where the
  Service does not exist yet; the EndpointSlice controller keeps an empty placeholder slice for
  a Service with no endpoints, so an existing Service always has one. Refusing would deadlock:
  pihole-2 exists only after `pihole-deferred`, which waits on `pihole`, which would wait on
  this Job. It is the Flux form of `roll_one.yml` skipping the instance its apply just created.
- Otherwise the Job polls pihole-2's slices until one serving endpoint appears, for
  `GUARD_WAIT_SECONDS`, as `roll_one.yml` waits 300s for its sibling. It then fails.

It talks to the API server with the pod's ServiceAccount token, through `flux_job_connect`, and
the API server's CA reaches `urlopen` through `SSL_CERT_FILE`, which the Job sets. An error
answer from the API server fails the Job at once; only an empty read is retried.
`PIHOLE_ROLL_DIGEST8` is in the spec only to rename the Job, and the script prints it.
Exit 0 when instance 1 may roll, 1 when it may not or a call fails, naming why.

Run: python3 pihole_sibling_guard.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/pihole/tests/test_pihole_sibling_guard.py
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path

from flux_job_connect import Request, open_url

API = "https://kubernetes.default.svc"
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
# The label the EndpointSlice controller puts on every slice it builds for a Service.
SERVICE_LABEL = "kubernetes.io/service-name"
# The seconds each API call may take.
CALL_TIMEOUT = 10
# The pause between two reads of the sibling's slices, in seconds.
POLL_SECONDS = 5


class Failed(Exception):
    """A call failed; the message names the object and the answer."""


class Kube:
    """The API server, as one JSON GET per call.

    Attributes:
        base: The API server's URL.
        token: The ServiceAccount token sent as a bearer credential.
        opener: Opens a `Request`; `open_url` unless a test passes its own.
    """

    def __init__(self, base: str, token: str, opener: Callable = open_url):
        self.base = base
        self.token = token
        self.opener = opener

    def get(self, path: str) -> tuple[int, dict]:
        """GET `path`; the HTTP status and the parsed body, an error status included."""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        request = Request(self.base + path, headers=headers, method="GET")
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


def slices_path(namespace: str, service: str) -> str:
    """The list path of `service`'s EndpointSlices in `namespace`."""
    selector = urllib.parse.quote(f"{SERVICE_LABEL}={service}")
    return (
        f"/apis/discovery.k8s.io/v1/namespaces/{namespace}/endpointslices"
        f"?labelSelector={selector}"
    )


def slices(kube: Kube, namespace: str, service: str) -> list[dict]:
    """`service`'s EndpointSlices in `namespace`.

    Raises:
        Failed: The list answered with an error status.
    """
    status, body = kube.get(slices_path(namespace, service))
    if status != 200:
        raise Failed(
            f"listing {service}'s EndpointSlices answered HTTP {status}: "
            f"{body.get('message', '')}"
        )
    return body.get("items") or []


def serving(items: list[dict]) -> int:
    """How many endpoints in the slices `items` are ready and not terminating.

    The API reads a missing `ready` as true and a missing `terminating` as false.
    """
    return sum(
        1
        for item in items
        for endpoint in item.get("endpoints") or []
        if (endpoint.get("conditions") or {}).get("ready") is not False
        and not (endpoint.get("conditions") or {}).get("terminating")
    )


def run(
    kube: Kube,
    env: Mapping[str, str],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Decide whether instance 1 may roll, and return one report line each.

    Raises:
        Failed: A list failed, or the sibling served nothing for the whole wait.
    """
    namespace = env["GUARD_NAMESPACE"]
    instance, sibling = env["GUARD_INSTANCE_SERVICE"], env["GUARD_SIBLING_SERVICE"]
    lines = [f"guarding pihole's roll {env.get('PIHOLE_ROLL_DIGEST8', 'unknown')}"]
    if not slices(kube, namespace, instance):
        return [
            *lines,
            f"{instance} has no EndpointSlice, so this is its first apply",
        ]
    deadline = clock() + float(env["GUARD_WAIT_SECONDS"])
    while True:
        count = serving(slices(kube, namespace, sibling))
        if count:
            return [
                *lines,
                f"{sibling} has {count} ready endpoint(s), so {instance} may roll",
            ]
        if clock() >= deadline:
            raise Failed(
                f"{sibling} had no ready endpoint for {env['GUARD_WAIT_SECONDS']}s, "
                f"so rolling {instance} would leave the LAN with no resolver"
            )
        sleep(POLL_SECONDS)


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    kube: Kube | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    if kube is None:
        kube = Kube(API, Path(TOKEN_FILE).read_text().strip())
    try:
        lines = run(kube, os.environ if env is None else env, sleep, clock)
    except Failed as err:
        print(f"pihole-sibling-guard: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"pihole-sibling-guard: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
