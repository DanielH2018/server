#!/usr/bin/env python3
"""Prove Longhorn can bind a volume on a new cluster, by binding one and removing it.

A Ready node and a longhorn-system pod count say the driver is running, which is not the same
claim as a PVC reaching Bound. Prod's provisioning is proven by its live volumes, so nothing
runs this on a schedule: it is step 6 of *Bootstrapping Flux on a rebuilt cluster* in
docs/flux-deploy-plane.md.

The claim goes in `default`, because a new cluster has no workload namespace, and no backup
carries its name. A failed bind still deletes the claim, because a Pending PVC would otherwise
hold the name the next run needs.

Run from the repo root as root, on a node holding the k3s kubeconfig:
    sudo .venv/bin/python -B scripts/diagnostics/storage_smoke.py
"""

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib.cli_help import ArgumentParser, error
from lib.exit_codes import FAILED, OK
from lib.kubectl import MissingKubectl
from lib.operator_kubectl import Kubectl, KubectlFailed, for_mode

CLAIM = "storage-smoke"
NAMESPACE = "default"
STORAGE_CLASS = "longhorn"
SIZE = "64Mi"
RETRIES = 24
DELAY_S = 5
SCRIPT = "scripts/diagnostics/storage_smoke.py"


def manifest() -> str:
    """The smoke test's PVC."""
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": CLAIM, "namespace": NAMESPACE},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": SIZE}},
            },
        }
    )


def wait_for_bound(kubectl: Kubectl, sleep: Callable[[float], None]) -> str:
    """Poll the PVC's phase until Bound or out of retries, and return the last phase read.

    A failed read is retried, because the PVC can lag the apply.
    """
    phase = ""
    for attempt in range(RETRIES):
        try:
            phase = kubectl(
                "-n", NAMESPACE, "get", "pvc", CLAIM, "-o", "jsonpath={.status.phase}"
            ).strip()
        except KubectlFailed:
            phase = ""
        if phase == "Bound":
            return phase
        if attempt < RETRIES - 1:
            sleep(DELAY_S)
    return phase


def smoke(kubectl: Kubectl, sleep: Callable[[float], None] = time.sleep) -> int:
    """Bind the PVC, report the phase, and delete it whatever happened.

    Returns:
        OK when the PVC reached Bound and was deleted, else FAILED.
    """
    code = FAILED
    try:
        kubectl("apply", "-f", "-", stdin=manifest())
        phase = wait_for_bound(kubectl, sleep)
        if phase == "Bound":
            print(f"PVC {CLAIM} reached Bound.")
            code = OK
        else:
            error(
                f"PVC {CLAIM} reached {phase or 'no phase at all'} rather than Bound. Longhorn "
                "is running but cannot provision a volume on this cluster. Check that a node "
                "allows scheduling and that a disk is attached: "
                "kubectl -n longhorn-system get nodes.longhorn.io"
            )
    except KubectlFailed as err:
        error(f"could not create PVC {CLAIM}: {err}")
    finally:
        try:
            kubectl(
                "-n",
                NAMESPACE,
                "delete",
                "pvc",
                CLAIM,
                "--ignore-not-found",
                "--wait=false",
            )
        except KubectlFailed as err:
            error(f"could not delete PVC {CLAIM}, so delete it by hand: {err}")
            code = FAILED
    return code


def main(argv: list[str] | None = None) -> int:
    """Run the smoke test.

    Args:
        argv: the command line; None reads `sys.argv`.

    Returns:
        OK when a volume bound, and FAILED otherwise.
    """
    parser = ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.parse_args(argv)
    try:
        kubectl = for_mode(True, SCRIPT, cluster=None)
    except (KubectlFailed, MissingKubectl) as err:
        error(str(err))
        return FAILED
    return smoke(kubectl)


if __name__ == "__main__":
    sys.exit(main())
