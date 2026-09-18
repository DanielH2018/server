"""One way to run kubectl from a script, and every call names the cluster it must reach.

WHY. ``lib.git`` and ``lib.gh`` are the single invokers for those tools; kubectl had none, and
by 2026-09-18 four argv conventions were hand-built across nine modules — ``k3s kubectl``,
bare ``kubectl``, tool discovery plus an explicit ``--kubeconfig``, and ``sudo k3s kubectl``
for an exec (#2062). Only one of them checked WHICH cluster it reached: the health gate's
refusal, added after a staging deploy read green about production (#1663). Every other caller
ran against whatever the local kubectl happened to serve, with no way to be refused.

Here the intended cluster is a required positional argument of every invocation, and the
identity check runs before the first call in a process and is cached after it. "Ran against
an unnamed cluster" is unrepresentable rather than a per-caller guard.

Two of the old conventions were deliberate and both survive as behaviour rather than as argv
shapes. ``infra_map/live.py`` runs under cron, whose PATH omits ``/usr/local/bin`` (where
kubectl lives, as a symlink to k3s) and whose environment carries no ``KUBECONFIG`` — so k3s's
kubectl falls back to the root-owned ``/etc/rancher/k3s/k3s.yaml`` and prints an empty
result that reads exactly like a cluster with nothing on it. ``find_tool`` and
``find_kubeconfig`` moved here from that module and apply to every caller: the binary is
resolved beyond the inherited PATH, and the kubeconfig is discovered, checked for
readability, and passed explicitly. The health gate's ``k3s kubectl`` was the same binary
reading the same kubeconfig (measured 2026-09-18: ``kubectl`` and ``k3s kubectl`` both
authenticate as ``homelab-readonly``), so it needed no shape of its own.

``privileged=True`` is the exec convention: ``sudo`` with the k3s kubeconfig, because the
discovered one is the read-only ServiceAccount, for which ``exec`` is Forbidden. The
identity check still runs unprivileged — it asks which cluster the API server is, which does
not depend on who is asking.

Pure pieces (``kubectl_argv``, ``node_names``, ``cluster_of``, ``cluster_refusal``,
``cluster_for_host``) run nothing and are assertable without a cluster. ``kubectl`` and
``kubectl_json`` are the runners.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── which cluster a kubectl serves ──────────────────────────────────────────────────────────
#
# Node names rather than an API-server address: the staging VM's k3s serves the same
# `https://127.0.0.1:6443` its production counterpart does, so the address discriminates
# nothing. Named rather than derived from the inventory at runtime — probe.py must answer
# without parsing Ansible — and pinned to it by
# scripts/diagnostics/tests/test_probe_health_cluster.py, which fails if a name here stops
# appearing in ansible/inventory/hosts.ini.
CLUSTER_NODES = {
    "prod": frozenset({"daniel-box", "daniel-server"}),
    "stage": frozenset({"daniel-stage"}),
}
DEFAULT_CLUSTER = "prod"


class WrongCluster(RuntimeError):
    """The local kubectl serves a different cluster than the call named, or none it knows.

    Raised before the call runs, so no caller reads an answer about the wrong subject. The
    message is ``cluster_refusal``'s, ready to print.
    """


def node_names(nodes_doc) -> list:
    """The node names in a `kubectl get nodes -o json` document."""
    items = (nodes_doc or {}).get("items") or []
    return [(item.get("metadata") or {}).get("name") for item in items]


def cluster_of(names) -> str | None:
    """Which cluster a node-name list belongs to, or None when no name is recognised.

    Membership, not equality: a node added to either cluster must not turn the answer into
    "unknown" and start refusing every call. A name list spanning both clusters is impossible
    — they are separate k3s installs — so the first match wins.
    """
    known = set(names or [])
    for cluster, expected in CLUSTER_NODES.items():
        if known & expected:
            return cluster
    return None


def cluster_for_host(hostname: str) -> str | None:
    """The cluster a node hostname belongs to, or None for a host in neither.

    For a script that runs ON a node and wants to name the cluster it is standing in, rather
    than default to production.
    """
    return cluster_of([hostname])


def cluster_refusal(requested: str, served: str | None) -> str | None:
    """A refusal message when `served` is not `requested`, else None.

    `served` is what `cluster_of` made of the live node list, or None when that read failed.
    Fails closed on None: a kubectl that cannot answer who its nodes are cannot support a
    claim about which cluster it just checked either.
    """
    if served == requested:
        return None
    if served is None:
        return (
            f"cannot confirm this kubectl serves the {requested} cluster — `kubectl get nodes` "
            "returned no recognised node. Refusing rather than running against an unknown "
            "cluster."
        )
    return (
        f"this kubectl serves the {served} cluster, not {requested} — refusing. The call reads "
        "whatever the local kubectl resolves to, so run it on a node of the cluster you "
        f"deployed to. There is no host-side kubeconfig for {requested} from here (#1663)."
    )


# ── discovery ───────────────────────────────────────────────────────────────────────────────

# Directories searched for the binary, on top of whatever PATH the caller happens to have.
# cron runs with PATH=/usr/bin:/bin, which omits /usr/local/bin — where kubectl lives as a
# symlink to k3s.
TOOL_DIRS = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/snap/bin")

# k3s ships kubectl as a symlink to itself and defaults it at this file, which is root-owned
# 0640. An interactive shell exports KUBECONFIG to the user copy, so `kubectl get` works by
# hand and fails under cron — the same ambient-environment trap as PATH, one variable over.
K3S_KUBECONFIG = Path("/etc/rancher/k3s/k3s.yaml")
USER_KUBECONFIG = Path.home() / ".kube" / "config"


def find_tool(name: str) -> str | None:
    """Resolve a binary by absolute path, searching beyond the inherited PATH."""
    found = shutil.which(name)
    if found:
        return found
    return shutil.which(name, path=os.pathsep.join(TOOL_DIRS))


def find_kubeconfig(
    user: Path = USER_KUBECONFIG, k3s: Path = K3S_KUBECONFIG
) -> Path | None:
    """Pick a kubeconfig this process can actually read.

    Returned explicitly and passed as ``--kubeconfig`` rather than left to kubectl's own
    lookup, so the answer does not change with the caller's environment. Readability is
    checked here, not assumed: the k3s default is root-only, and discovering that at exec time
    yields a warning on stderr and an empty result, which reads exactly like a cluster with
    no deployments.
    """
    candidates = []
    env_path = os.environ.get("KUBECONFIG", "").strip()
    if env_path:
        # KUBECONFIG is a path LIST; kubectl merges the entries left to right.
        candidates.extend(Path(p) for p in env_path.split(os.pathsep) if p)
    candidates.extend((user, k3s))
    for candidate in candidates:
        if os.access(candidate, os.R_OK):
            return candidate
    return None


class MissingKubectl(RuntimeError):
    """No kubectl binary, or no readable kubeconfig — a broken setup, not a cluster state.

    Kept distinct from a failed call on purpose: a process that could never have reached
    the cluster must not report what it did not see as an empty result.
    """


# ── argv ────────────────────────────────────────────────────────────────────────────────────


def kubectl_argv(
    *args: str,
    binary: str = "kubectl",
    kubeconfig: str | Path | None = None,
    privileged: bool = False,
) -> list[str]:
    """The one argv shape: ``[sudo] <binary> [--kubeconfig <path>] <args>``.

    Pure. ``binary`` and ``kubeconfig`` default to the bare names so a ``--dry-run`` prints
    the command a reader can run by hand; the runner passes what it discovered.
    """
    argv = [binary]
    if privileged:
        argv = ["sudo", binary]
    if kubeconfig is not None:
        argv += ["--kubeconfig", str(kubeconfig)]
    return argv + list(args)


def nodes_args() -> list[str]:
    """The arguments of the cluster-identity read."""
    return ["get", "nodes", "-o", "json"]


# ── runners ─────────────────────────────────────────────────────────────────────────────────


def _run(argv: list[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, check=False, timeout=timeout
    )


@dataclass(frozen=True)
class Tools:
    """The three process boundaries a call crosses, as one injectable object.

    A test hands the runner a ``Tools`` with fakes and patches nothing — the pattern
    ``scripts/deploy_tools/land_lib/tools.py`` set. The defaults are the real implementations.

    Attributes:
        run: runs an argv and returns the completed process.
        find_tool: resolves a binary by name to an absolute path, or None.
        find_kubeconfig: picks a readable kubeconfig, or None.
    """

    run: Callable[[list[str], float | None], subprocess.CompletedProcess[str]] = _run
    find_tool: Callable[[str], str | None] = find_tool
    find_kubeconfig: Callable[[], Path | None] = find_kubeconfig


DEFAULT_TOOLS = Tools()


def _resolve(tools: Tools) -> tuple[str, Path]:
    """The binary and kubeconfig this process runs with, or ``MissingKubectl``."""
    binary = tools.find_tool("kubectl")
    if binary is None:
        raise MissingKubectl("kubectl not found on this host")
    kubeconfig = tools.find_kubeconfig()
    if kubeconfig is None:
        raise MissingKubectl(
            f"no readable kubeconfig (tried $KUBECONFIG, {USER_KUBECONFIG}, {K3S_KUBECONFIG})"
        )
    return binary, kubeconfig


# One identity read per (tools, binary, kubeconfig) per process. probe.py runs several kubectl
# calls per subcommand; the check must cost one `get nodes`, not one per call.
_SERVED: dict[tuple[Tools, str, str], str | None] = {}


def served_cluster(
    timeout: float | None = 30.0, tools: Tools = DEFAULT_TOOLS
) -> str | None:
    """Which cluster the discovered kubectl reaches, or None when the node read failed."""
    binary, kubeconfig = _resolve(tools)
    key = (tools, binary, str(kubeconfig))
    if key not in _SERVED:
        proc = tools.run(
            kubectl_argv(*nodes_args(), binary=binary, kubeconfig=kubeconfig), timeout
        )
        served = None
        if proc.returncode == 0:
            try:
                served = cluster_of(node_names(json.loads(proc.stdout)))
            except json.JSONDecodeError:
                served = None
        _SERVED[key] = served
    return _SERVED[key]


def forget_served_cluster() -> None:
    """Drop the cached identity, for a process that outlives a cluster change."""
    _SERVED.clear()


def kubectl(
    cluster: str,
    *args: str,
    check: bool = False,
    timeout: float | None = 60.0,
    privileged: bool = False,
    tools: Tools = DEFAULT_TOOLS,
) -> subprocess.CompletedProcess[str]:
    """Run ``kubectl <args>`` against ``cluster`` and return the completed process.

    Raises ``WrongCluster`` before running when the local kubectl serves a different cluster
    or one it cannot name, and ``MissingKubectl`` when there is no binary or readable
    kubeconfig. ``check=False`` by default, unlike ``lib.gh``: most callers here treat a
    non-zero exit as "not found" rather than as an error. Pass ``check=True`` to raise
    ``CalledProcessError`` with kubectl's stderr attached.
    """
    if cluster not in CLUSTER_NODES:
        raise ValueError(f"unknown cluster {cluster!r}; one of {sorted(CLUSTER_NODES)}")
    refusal = cluster_refusal(cluster, served_cluster(timeout=timeout, tools=tools))
    if refusal:
        raise WrongCluster(refusal)
    binary, kubeconfig = _resolve(tools)
    argv = kubectl_argv(
        *args,
        binary=binary,
        kubeconfig=K3S_KUBECONFIG if privileged else kubeconfig,
        privileged=privileged,
    )
    proc = tools.run(argv, timeout)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, argv, proc.stdout, proc.stderr
        )
    return proc


def kubectl_json(cluster: str, *args: str, **kwargs: Any) -> Any:
    """``kubectl(...)`` with stdout parsed as JSON, or None on a non-zero exit or bad output.

    None covers both because a missing workload, Service or EndpointSlice is an expected
    result for the probes that call this, not a failure to raise on.
    """
    proc = kubectl(cluster, *args, **kwargs)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
