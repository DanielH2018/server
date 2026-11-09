"""system-upgrade's verify Job: one node's installed k3s binary against the release's sha256.

`templates/verify/` holds one Job per node in `system_upgrade_nodes`, and the renderer emits
them into `system-upgrade-verify`, which Flux applies once `flux/system-upgrade` reads Ready,
so after the Plans finished at the pin (H6, *The installed binary matches the release*).

The Job reads its node's `kubeletVersion` through the API as `k3s-plan` and executes no host
binary. A node on another version than `K3S_VERSION` reads pending, with a note and exit 0,
because a hold can leave a node on the old version while the pin moved. A node at the pinned
version whose binary hashes to another sum fails, naming the node and both sums. Any read
error fails.

The node's k3s binary arrives read-only by hostPath at `K3S_BINARY`. The script is
this role's own, not one the host package ships: the Job census admits only a script from a
mounted ConfigMap that connects through `flux_job_connect`.

Inputs come from the Job's environment: `NODE_NAME`, `K3S_VERSION`, `K3S_SHA256` and
`K3S_BINARY`. Exit 0 when the binary matches or the node is pending, 1 otherwise.

Run: python3 system_upgrade_verify.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/system-upgrade/tests/test_system_upgrade_verify.py
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path

from flux_job_connect import Request, open_url

API = "https://kubernetes.default.svc"
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
# The socket timeout of the one API call, in seconds.
TIMEOUT = 10


class Failed(Exception):
    """The check failed; the message names the node and what is wrong."""


def read_node(name: str, token: str, base: str = API) -> dict:
    """The live Node `name`, parsed.

    Raises:
        Failed: The API server answered anything but 200.
    """
    request = Request(
        f"{base}/api/v1/nodes/{urllib.parse.quote(name)}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with open_url(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as err:
        with err:
            raise Failed(f"reading node {name} answered HTTP {err.code}") from err


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check(node: dict, env: Mapping[str, str]) -> str:
    """The report line for one node.

    Raises:
        Failed: The node reports no version, or runs the pin and its binary hashes to
            another sum, or the binary cannot be read.
    """
    name, version = env["NODE_NAME"], env["K3S_VERSION"]
    want = env["K3S_SHA256"].lower()
    running = str(
        ((node.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion", "")
    )
    if not running:
        raise Failed(f"node {name} reports no kubeletVersion")
    # SUC writes `+` as `-` (`MungeVersion`), so either form names the pin.
    if running.replace("+", "-") != version.replace("+", "-"):
        return f"node {name} runs {running}, not {version}: pending"
    try:
        got = file_sha256(env["K3S_BINARY"])
    except OSError as err:
        raise Failed(f"node {name}: cannot read the k3s binary: {err}") from err
    if got != want:
        raise Failed(
            f"node {name}: the k3s binary hashes to {got}, the release's sum for "
            f"{version} is {want}"
        )
    return f"node {name} runs {version}, and its k3s binary matches {want}"


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    read: Callable[[str], dict] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    env = os.environ if env is None else env
    if read is None:
        token = Path(TOKEN_FILE).read_text().strip()

        def read(name: str) -> dict:
            return read_node(name, token)

    try:
        line = check(read(env["NODE_NAME"]), env)
    except Failed as err:
        print(f"system-upgrade-verify: {err}", file=sys.stderr)
        return 1
    print(f"system-upgrade-verify: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
