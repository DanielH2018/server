"""The kubectl an operator-run script calls, with its reads and writes split by kubeconfig.

A dry run reads through the first kubeconfig this user can read, which on daniel-box is the
read-only ServiceAccount. An apply needs the root-only k3s kubeconfig, so the operator runs the
script under sudo, and `for_mode` names that command when they did not.

`for_mode` refuses a kubeconfig that serves another cluster, because each script acts on what it
reads: a drain trusts the live-volume list to refuse a live volume by name.

A runner raises `KubectlFailed` on a non-zero exit, because an unread volume state must never
read as an empty one. A caller that can poll again catches it.

Typical usage example:

    kubectl = operator_kubectl.for_mode(args.apply, "scripts/x.py --apply")
    pvc = json.loads(kubectl("-n", "homelab", "get", "pvc", "app-config", "-o", "json"))
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib.kubectl import (
    DEFAULT_CLUSTER,
    K3S_KUBECONFIG,
    MissingKubectl,
    cluster_of,
    cluster_refusal,
    find_kubeconfig,
    find_kubectl,
    kubectl_argv,
    node_names,
)

DEFAULT_TIMEOUT_S = 60


class KubectlFailed(RuntimeError):
    """A kubectl call exited non-zero, timed out, or could not start."""


class Kubectl(Protocol):
    """Runs one kubectl command and returns its stdout."""

    def __call__(self, *args: str, stdin: str | None = None) -> str: ...


def runner(
    kubeconfig: str | Path,
    binary: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Kubectl:
    """A `Kubectl` bound to one kubeconfig.

    Args:
        kubeconfig: passed as `--kubeconfig`, so the caller's environment cannot change it.
        binary: the kubectl binary to run.
        run: the process boundary; a test passes a fake.
        timeout: seconds one call may take.
    """

    def kubectl(*args: str, stdin: str | None = None) -> str:
        argv = kubectl_argv(*args, binary=binary, kubeconfig=kubeconfig)
        try:
            proc = run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as err:
            raise KubectlFailed(f"kubectl {' '.join(args)}: {err}") from err
        if proc.returncode != 0:
            raise KubectlFailed(f"kubectl {' '.join(args)}: {proc.stderr.strip()}")
        return proc.stdout

    return kubectl


def served_cluster(kubectl: Kubectl) -> str | None:
    """The cluster `kubectl` reaches, or None when its node list names none.

    Raises:
        KubectlFailed: the node read failed.
    """
    try:
        return cluster_of(node_names(json.loads(kubectl("get", "nodes", "-o", "json"))))
    except ValueError:
        return None


def for_mode(
    apply: bool,
    apply_command: str,
    cluster: str | None = DEFAULT_CLUSTER,
    tool: Callable[[], str | None] = find_kubectl,
    kubeconfig: Callable[[], Path | None] = find_kubeconfig,
    readable: Callable[[Path], bool] = lambda path: os.access(path, os.R_OK),
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Kubectl:
    """The runner for a dry run or for an apply.

    Args:
        apply: whether this run writes, which needs the root-only k3s kubeconfig.
        apply_command: the script's path from the repo root and its flags, which the refusal
            prints.
        cluster: the cluster the kubeconfig must serve; None skips the check, for a new
            cluster whose node names no table holds.
        tool: resolves the kubectl binary.
        kubeconfig: picks the kubeconfig a dry run reads through.
        readable: says whether this user can read the k3s kubeconfig.
        run: the process boundary, handed to `runner`.

    Raises:
        KubectlFailed: an apply by a user who cannot read the k3s kubeconfig, whose message
            prints the sudo command, or a kubeconfig that does not serve `cluster`.
        MissingKubectl: this host has no kubectl, or a dry run finds no readable kubeconfig.
    """
    if apply and not readable(K3S_KUBECONFIG):
        raise KubectlFailed(
            f"This write needs the root-only kubeconfig at {K3S_KUBECONFIG}. Re-run from the "
            f"repo root as: sudo {sys.executable} -B {apply_command}"
        )
    binary = tool()
    if binary is None:
        raise MissingKubectl("kubectl not found on this host")
    chosen = K3S_KUBECONFIG if apply else kubeconfig()
    if chosen is None:
        raise MissingKubectl("no readable kubeconfig on this host")
    kubectl = runner(chosen, binary, run=run)
    refusal = cluster and cluster_refusal(cluster, served_cluster(kubectl))
    if refusal:
        raise KubectlFailed(f"{chosen}: {refusal}")
    return kubectl
