#!/usr/bin/env python3
"""Revert one service's Longhorn volumes to the snapshot its `<tag>-snapshot` Job took.

DRY RUN BY DEFAULT. Without --apply it resolves each claim's volume and prints the snapshot a
real run would revert to, and it changes nothing. Every apply is operator-only, and is a step
of *The revert runbook* in docs/volume-snapshot-drills.md, which starts from the stateful hold's
Discord post. The post prints this command once per claim namespace.

WHAT IT LEAVES BEHIND. It scales the Deployment to zero and does not scale it back. The
runbook's resume of `<tag>` applies `last-good`'s spec, which restores the replicas.

THE ORDER. Every claim's snapshot is found before anything moves, so a missing snapshot on any
claim leaves the service up. Then, per claim: wait for `detached`, attach with
`disableFrontend`, wait for `attached`, check the flag, revert, detach, wait for `detached`.
Longhorn v1.12.1 answers 500 to a revert on a volume with its frontend enabled and on a plainly
detached one. docs/volume-revert-drill-and-sizing.md has those measurements, the timeouts and
the hand recovery for a run that stops partway. A multi-claim revert is not atomic.

Run from the repo root on daniel-box. A dry run reads through your own kubeconfig, and an apply
needs the root-only one:
    uv run python scripts/backup/volume_revert.py --service <tag> --namespace <ns> \
        --sha <digest8> --claim <pvc> [--claim <pvc>]
    sudo .venv/bin/python -B scripts/backup/volume_revert.py --service <tag> --namespace <ns> \
        --sha <digest8> --claim <pvc> [--claim <pvc>] --apply
"""

import argparse
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib.cli_help import ArgumentParser, error
from lib.exit_codes import FAILED, OK, USAGE_ERROR
from lib.kubectl import MissingKubectl
from lib.operator_kubectl import Kubectl, KubectlFailed, for_mode
from lib.repo_paths import FILTER_PLUGINS
from renderer.renderer_lib.snapshot import snapshot_name

sys.path.insert(0, str(FILTER_PLUGINS))
from longhorn_manager import ready_manager_ip

LONGHORN_NAMESPACE = "longhorn-system"
APPLY_COMMAND = "scripts/backup/volume_revert.py <the same flags> --apply"
STATE_TIMEOUT_S = 90
POLL_INTERVAL_S = 2
API_TIMEOUT_S = 30
_SHA = re.compile(r"[0-9a-f]{8,}")
_RECOVERY = "docs/volume-revert-drill-and-sizing.md has the hand recovery."


class RevertError(Exception):
    """A step failed, so the revert stopped where it was."""


def snapshot_prefix(service: str, sha: str, claim: str) -> str:
    """The prefix every snapshot of this claim and digest shares, up to its run token.

    The trailing `-` keeps `code-server-config` from matching `code-server-configmap`.
    """
    return snapshot_name(service, sha, claim, token="")


def newest_snapshot(items: list[dict], volume: str, prefix: str) -> str | None:
    """The newest usable snapshot of `volume` under `prefix`, or None.

    Newest by `creationTimestamp`, because CR names are not chronological. Longhorn refuses to
    revert to a `markRemoved` snapshot, and a just-created one can leave the field unset.
    """
    usable = [
        item
        for item in items
        if (item.get("spec") or {}).get("volume") == volume
        and item["metadata"]["name"].startswith(prefix)
        and (item.get("status") or {}).get("markRemoved") is not True
    ]
    if not usable:
        return None
    return max(usable, key=lambda item: item["metadata"]["creationTimestamp"])[
        "metadata"
    ]["name"]


def post_action(
    base: str,
    volume: str,
    action: str,
    body: dict,
    urlopen: Callable = urllib.request.urlopen,
) -> None:
    """POST one action to the Longhorn API, demanding HTTP 200.

    Raises:
        RevertError: the call failed or answered anything else.
    """
    request = urllib.request.Request(
        f"{base}/v1/volumes/{volume}?action={action}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=API_TIMEOUT_S) as response:
            status = response.status
    except (urllib.error.URLError, OSError) as err:
        raise RevertError(f"{action} on {volume}: {err}") from err
    if status != 200:
        raise RevertError(f"{action} on {volume} answered HTTP {status}, not 200")


class Revert:
    """One service's revert: the cluster reads and writes it makes, in order.

    Attributes:
        kubectl: runs kubectl.
        post: posts one Longhorn API action, shaped like `post_action`.
        sleep: waits between polls.
        clock: reads monotonic time for the state waits.
    """

    def __init__(
        self,
        kubectl: Kubectl,
        post: Callable[[str, str, str, dict], None] = post_action,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.kubectl = kubectl
        self.post = post
        self.sleep = sleep
        self.clock = clock

    def _volume(self, volume: str) -> dict:
        return json.loads(
            self.kubectl(
                "-n",
                LONGHORN_NAMESPACE,
                "get",
                "volumes.longhorn.io",
                volume,
                "-o",
                "json",
            )
        )

    def api_base(self, node: str) -> str:
        """The Longhorn API on `node`'s own ready manager pod.

        The NetworkPolicy refuses host traffic to any other node's manager, so the
        `longhorn-backend` ClusterIP is no fallback.

        Raises:
            RevertError: this node runs no ready manager pod.
        """
        pods = json.loads(
            self.kubectl(
                "-n",
                LONGHORN_NAMESPACE,
                "get",
                "pod",
                "-l",
                "app=longhorn-manager",
                "--field-selector",
                f"spec.nodeName={node}",
                "-o",
                "json",
            )
        )["items"]
        ip = ready_manager_ip(pods, node)
        if not ip:
            raise RevertError(
                f"no ready longhorn-manager pod on {node}, so the Longhorn API is unreachable "
                "from here. Nothing has been changed."
            )
        return f"http://{ip}:9500"

    def find(
        self, namespace: str, service: str, sha: str, claim: str
    ) -> tuple[str, str]:
        """(volume, snapshot) for one claim, read before anything moves.

        Raises:
            RevertError: the claim is unbound or no snapshot matches.
        """
        volume = self.kubectl(
            "-n",
            namespace,
            "get",
            "pvc",
            claim,
            "-o",
            "jsonpath={.spec.volumeName}",
        ).strip()
        if not volume:
            raise RevertError(
                f"PVC {namespace}/{claim} has no spec.volumeName: it is still Pending. "
                "Nothing has been changed."
            )
        prefix = snapshot_prefix(service, sha, claim)
        items = json.loads(
            self.kubectl(
                "-n",
                LONGHORN_NAMESPACE,
                "get",
                "snapshots.longhorn.io",
                "-o",
                "json",
            )
        )["items"]
        snapshot = newest_snapshot(items, volume, prefix)
        if snapshot is None:
            raise RevertError(
                f"no snapshot named '{prefix}*' exists for Longhorn volume {volume} "
                f"({namespace}/{claim}). This revision never took one, or a retention pass "
                "deleted it. Nothing has been changed, and this script will not guess."
            )
        return volume, snapshot

    def wait_for_state(self, volume: str, state: str) -> dict:
        """Poll until `volume` reads `state`, and return the volume.

        Raises:
            RevertError: the state did not arrive within STATE_TIMEOUT_S.
        """
        deadline = self.clock() + STATE_TIMEOUT_S
        while True:
            found = self._volume(volume)
            current = (found.get("status") or {}).get("state")
            if current == state:
                return found
            if self.clock() >= deadline:
                raise RevertError(
                    f"volume {volume} reads '{current}', not '{state}', after "
                    f"{STATE_TIMEOUT_S}s. {_RECOVERY}"
                )
            self.sleep(POLL_INTERVAL_S)

    def revert_volume(self, base: str, node: str, volume: str, snapshot: str) -> None:
        """Attach `volume` in maintenance mode, revert it to `snapshot`, and detach it.

        The attach and the detach both omit `attachmentID`. Longhorn keys the attachment ticket
        on that field, so sending it on only one call leaves the volume attached.

        Raises:
            RevertError: a call failed, or a state or the frontend flag came back wrong.
        """
        self.wait_for_state(volume, "detached")
        self.post(base, volume, "attach", {"hostId": node, "disableFrontend": True})
        attached = self.wait_for_state(volume, "attached")
        if (attached.get("spec") or {}).get("disableFrontend") is not True:
            raise RevertError(
                f"volume {volume} attached with its frontend enabled, so a revert would "
                f"answer HTTP 500. {_RECOVERY}"
            )
        self.post(base, volume, "snapshotRevert", {"name": snapshot})
        self.post(base, volume, "detach", {})
        # A 200 detach proves only that the request was accepted.
        self.wait_for_state(volume, "detached")

    def run(
        self,
        namespace: str,
        service: str,
        sha: str,
        claims: list[str],
        node: str,
        apply: bool,
    ) -> None:
        """Revert every claim under --apply, or print what would be reverted.

        Raises:
            RevertError: the first step that failed. Its message says what state it left.
            KubectlFailed: a kubectl call failed.
        """
        base = self.api_base(node)
        found = [(c, *self.find(namespace, service, sha, c)) for c in claims]
        for claim, volume, snapshot in found:
            print(f"{claim}: volume {volume}, snapshot {snapshot}")
        if not apply:
            print(f"dry run: nothing changed. Re-run with --apply to revert {service}.")
            return
        self.kubectl("-n", namespace, "scale", f"deployment/{service}", "--replicas=0")
        for claim, volume, snapshot in found:
            try:
                self.revert_volume(base, node, volume, snapshot)
            except (RevertError, KubectlFailed) as err:
                raise RevertError(
                    f"{claim}: {err} {service} is at zero replicas, and a claim before this "
                    "one may already be reverted."
                ) from err
            print(f"{claim}: reverted to {snapshot}")
        print(f"{service} stays at zero replicas until the runbook resumes it.")


def main(
    argv: list[str] | None = None,
    kubectl: Kubectl | None = None,
    post: Callable[[str, str, str, dict], None] = post_action,
    sleep: Callable[[float], None] = time.sleep,
    hostname: Callable[[], str] = socket.gethostname,
) -> int:
    """Parse the flags and run the revert.

    Args:
        argv: the command line; None reads `sys.argv`.
        kubectl: replaces the real runner, for a test.
        post: posts one Longhorn API action.
        sleep: waits between polls.
        hostname: reads this node's name, which is the Longhorn node name up to the first dot.

    Returns:
        OK when it reverted or would revert, FAILED on a refusal or a failed step, and
        USAGE_ERROR on a bad flag.
    """
    parser = ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--service", required=True, help="the tag, also the Deployment name"
    )
    parser.add_argument(
        "--namespace", default="homelab", help="the claims' and Deployment's namespace"
    )
    parser.add_argument(
        "--sha", required=True, help="the digest8 the hold post prints, used verbatim"
    )
    parser.add_argument(
        "--claim", action="append", required=True, help="a PVC name; repeat for each"
    )
    parser.add_argument(
        "--apply", action="store_true", help="revert; without it nothing changes"
    )
    args = parser.parse_args(argv)
    if not _SHA.fullmatch(args.sha):
        error(f"--sha takes eight or more lowercase hex digits, not {args.sha!r}")
        return USAGE_ERROR
    try:
        Revert(kubectl or for_mode(args.apply, APPLY_COMMAND), post, sleep).run(
            args.namespace,
            args.service,
            args.sha,
            args.claim,
            hostname().split(".")[0],
            args.apply,
        )
    except (RevertError, KubectlFailed, MissingKubectl, ValueError) as err:
        error(str(err))
        return FAILED
    return OK


if __name__ == "__main__":
    sys.exit(main())
