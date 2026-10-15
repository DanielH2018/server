"""The two host-side boundaries `promote.py land-wait` crosses on daniel-box.

`FluxHost` reads the Flux Kustomizations with the read-only `kubectl`, and writes the `logger`
line that records a settled promotion as a deploy annotation. `promote_lib/wait.py` starts no
subprocess itself, as `promote.py` starts none (`forge.py` holds their GitHub and GHCR
calls), so both process boundaries live here. A GitHub runner never constructs one.

Run: imported only, by `promote_lib/wait.py`.
"""

import contextlib
import json
import subprocess
from collections.abc import Callable

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib import kubectl as _kubectl

from forge import ForgeError

# The namespace every Kustomization `promote` publishes lives in.
FLUX_NAMESPACE = "flux-system"
KUSTOMIZATIONS = "kustomizations.kustomize.toolkit.fluxcd.io"
# `lib.kubectl` names the cluster each call must reach, and land-wait reads production.
CLUSTER = "prod"
KUBECTL_TIMEOUT_S = 60


class FluxHost:
    """The read-only `kubectl` and `logger` on the host that runs `promote.py land-wait`."""

    def __init__(
        self,
        kubectl: Callable[..., subprocess.CompletedProcess] = _kubectl.kubectl,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self._kubectl = kubectl
        self._run = run

    def kustomizations(self) -> list[dict]:
        """Every Kustomization in `FLUX_NAMESPACE`, as `kubectl get -o json` lists them.

        Raises:
            ForgeError: kubectl is missing, reaches the wrong cluster, exits non-zero or
                prints something other than a list.
        """
        try:
            proc = self._kubectl(
                CLUSTER,
                "get",
                KUSTOMIZATIONS,
                "-n",
                FLUX_NAMESPACE,
                "-o",
                "json",
                timeout=KUBECTL_TIMEOUT_S,
            )
        except (
            OSError,
            subprocess.SubprocessError,
            _kubectl.MissingKubectl,
            _kubectl.WrongCluster,
        ) as err:
            raise ForgeError(f"kubectl get {KUSTOMIZATIONS}: {err}") from err
        if proc.returncode != 0:
            raise ForgeError(
                f"kubectl get {KUSTOMIZATIONS} exited {proc.returncode}: "
                f"{proc.stderr.strip()}"
            )
        try:
            doc = json.loads(proc.stdout)
        except ValueError as err:
            raise ForgeError(f"kubectl get {KUSTOMIZATIONS}: not JSON") from err
        items = doc.get("items") if isinstance(doc, dict) else None
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ForgeError(f"kubectl get {KUSTOMIZATIONS}: no list of items")
        return items

    def log(self, tag: str, line: str) -> None:
        """Write `line` to syslog under `tag`. Fire-and-forget: a failed `logger` is ignored.

        `logger` is absent in a container and can fail on a full disk, and neither may turn a
        promotion that settled into a reported failure.
        """
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            self._run(
                ["logger", "-t", tag, line],
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
