"""The guard the live-API seam tests share: did kubectl fail for want of a cluster to ask?

A seam test runs a role's own kubectl argv against the real API server, and skips where there is
none. "None" has several spellings, and a guard that misses one turns "no cluster" into a red test
on every machine in that state. A rejected jsonpath must never read as one of them: that is the
failure each seam test exists to catch.
"""

import subprocess

# kubectl's ways of saying "there is no cluster here to ask". kubectl does NOT print the bare
# string "connection refused" — it prints "The connection to the server localhost:8080 was
# refused" — and a cluster without Longhorn's CRDs answers "the server doesn't have a resource
# type". "error loading config file" is a kubectl that cannot read the kubeconfig it fell back
# to: as the `claude` agent user, `/etc/rancher/k3s/k3s.yaml` is root-only (#4221).
_NO_CLUSTER = (
    "connection refused",
    "was refused",
    "i/o timeout",
    "no configuration has been provided",
    "doesn't have a resource type",
    "error loading config file",
)


def no_cluster_to_ask(stderr: str) -> bool:
    """Whether kubectl failed for want of a cluster rather than for want of a valid jsonpath.

    A rejected jsonpath never counts as unreachable, whatever else the stderr says: that is the
    one failure the seam test exists to catch, and it reads `error: error parsing jsonpath …`.
    """
    return "jsonpath" not in stderr and any(token in stderr for token in _NO_CLUSTER)


def kubectl_has_a_cluster_configured(
    kubectl: list[str], *, timeout: float = 10
) -> bool:
    """Whether kubectl has a kubeconfig context to ask, decided without touching the network.

    `kubectl` is the argv prefix the seam test runs, `["kubectl"]` or `["/usr/bin/k3s",
    "kubectl"]`. `config current-context` reads only the local kubeconfig, so it fails at once
    where none is set: a GitHub runner ships kubectl with no kubeconfig, and there the live probe
    spent about 15 s on its request timeout and discovery retries before it could skip (#5219).
    The same command fails for a kubeconfig the caller cannot read, the `error loading config
    file` case `no_cluster_to_ask` already treats as no cluster.

    A kubectl that outlasts `timeout` also counts as no cluster, so the seam test skips rather
    than errors: a loaded runner once took over 10 s to start kubectl, and the uncaught
    `TimeoutExpired` turned that skip into a red test (#5243).
    """
    try:
        done = subprocess.run(
            [*kubectl, "config", "current-context"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0 and bool(done.stdout.strip())
