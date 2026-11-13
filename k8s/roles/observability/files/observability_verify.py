"""observability's verify Job: prove the live OTLP receiver's hostPort is pinned to loopback.

`templates/verify/` holds the Job, and the renderer emits it into `observability-verify`
(target slice D5). It replaces the role's former "Read back the OTLP listener's bind address"
and "Fail if the OTLP receiver is not pinned to loopback" tasks.

The collector receives prompts and responses verbatim, and Claude Code on the host reaches it
through a hostPort. A hostPort with no `hostIP` binds every node address, which publishes the
receiver to the whole LAN behind a green rollout. So the Job reads the live DaemonSet and
fails unless every hostPort it declares carries the expected `hostIP`. A DaemonSet with no
hostPort at all fails too: the check would have no subject, and Claude Code could not reach
the receiver either.

The Job reads the API server with its ServiceAccount token, through `flux_job_connect`. Its
Role grants `get` on the one DaemonSet and nothing else, and the API server's CA reaches
`urlopen` through `SSL_CERT_FILE`, which the Job sets. It dials no pod, so it needs no fence
peer.

Inputs come from the Job's environment: `OTLP_NAMESPACE`, `OTLP_DAEMONSET` and
`OTLP_HOST_IP`. `OBSERVABILITY_RENDER_DIGEST8` is in the spec only to rename the Job, and the
script prints it. Exit 0 when every hostPort is pinned, 1 otherwise, naming the port.

Run: python3 observability_verify.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/observability/tests/test_observability_verify.py
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
# The socket timeout of the one API call, in seconds.
TIMEOUT = 10


class Failed(Exception):
    """The check failed; the message names what is wrong."""


def read_daemonset(namespace: str, name: str, token: str, base: str = API) -> dict:
    """The live DaemonSet `namespace/name`, parsed.

    Raises:
        Failed: The API server answered anything but 200.
    """
    request = Request(
        f"{base}/apis/apps/v1/namespaces/{namespace}/daemonsets/{name}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with open_url(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as err:
        with err:
            raise Failed(
                f"reading daemonset {namespace}/{name} answered HTTP {err.code}"
            ) from err


def host_ports(daemonset: dict) -> list[tuple[str, int, str]]:
    """(container, hostPort, hostIP) for every port in the pod template that sets a hostPort.

    A port with no `hostIP` reads as the empty string, the every-address bind.
    """
    spec = daemonset.get("spec", {}).get("template", {}).get("spec", {})
    return [
        (container.get("name", "?"), int(port["hostPort"]), port.get("hostIP", ""))
        for container in spec.get("containers") or []
        for port in container.get("ports") or []
        if port.get("hostPort")
    ]


def check(daemonset: dict, expected_ip: str) -> list[str]:
    """One report line per pinned hostPort.

    Raises:
        Failed: No hostPort is declared, or one binds an address other than `expected_ip`.
    """
    ports = host_ports(daemonset)
    if not ports:
        raise Failed(
            "the DaemonSet declares no hostPort, so nothing pins the OTLP receiver and the "
            "host cannot reach it"
        )
    unpinned = [p for p in ports if p[2] != expected_ip]
    if unpinned:
        listed = ", ".join(
            f"{name}:{port} bound to {ip or '0.0.0.0'}" for name, port, ip in unpinned
        )
        raise Failed(
            f"OTLP receiver NOT pinned to {expected_ip}: {listed}. The receiver carries "
            "prompts and responses verbatim and must not be reachable off the node."
        )
    return [f"pinned: {name}:{port} on {ip}" for name, port, ip in ports]


def run(env: Mapping[str, str], read: Callable[[str, str], dict]) -> list[str]:
    """Read the DaemonSet through `read(namespace, name)` and check it.

    Raises:
        Failed: The read or the check failed.
    """
    daemonset = read(env["OTLP_NAMESPACE"], env["OTLP_DAEMONSET"])
    return [
        f"verifying observability's render {env.get('OBSERVABILITY_RENDER_DIGEST8', 'unknown')}",
        *check(daemonset, env["OTLP_HOST_IP"]),
    ]


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    read: Callable[[str, str], dict] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    if read is None:
        token = Path(TOKEN_FILE).read_text().strip()

        def read(namespace: str, name: str) -> dict:
            return read_daemonset(namespace, name, token)

    try:
        lines = run(os.environ if env is None else env, read)
    except Failed as err:
        print(f"observability-verify: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"observability-verify: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
