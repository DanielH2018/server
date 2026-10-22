"""The cluster-plumbing checks `probe.py health` adds for the `coredns` and `longhorn` tags.

H1 hands CoreDNS and Longhorn to Flux and deletes the `setup/k3s` host tasks that checked them
after each apply (`docs/target-architecture/host/h1-cluster-config-to-flux.md`). A rollout
verdict cannot see what those tasks saw, so each check moves here, keyed by deploy tag:

  - `coredns` (#5924): the `NodeHosts` key of ConfigMap `kube-system/coredns` is not empty,
    which the host task `Confirm NodeHosts survived the patch` checked. A cluster name and an
    upstream-only name resolve through the `kube-dns` Service, which the host task `Verify
    cluster DNS resolves through the configured upstream` checked. The second check catches a
    Corefile that loads but resolves wrongly.
  - `longhorn` (#5923): the live `longhorn` StorageClass carries the parameters of the class in
    ConfigMap `longhorn-system/longhorn-storageclass`. Parameters are immutable, and
    longhorn-manager creates the class but is not shown to replace a drifted one. After a
    parameter change the live class keeps the old values, and before this check nothing
    reported it.

`health.py` folds these in after a passing rollout, on the path where the tag resolves to a k8s
role. Each tag resolves that way only once its H1 role lands (H1.4 for `coredns`, H1.5a for
`longhorn`); until then `run_health` takes the guess-the-name path and never calls this module.

A NotFound on any object read here is a FAILED check, not a skip: each object is plumbing the
cluster cannot run without, and none of these messages carries a `NOT_APPLICABLE_MARKERS`
substring. Any other failed kubectl read raises `core.Unreadable` through `read`.

Transport, measured on daniel-box on 2026-10-22: each kubectl read took 0.07-0.10s and each
dig through `kube-dns` 0.01-0.02s, over three runs. The checks add well under a second to a
passing gate, so neither needs the cheap-signal-first shape `.claude/rules/python-layout.md`
asks of a slow source.
"""

import shutil
import socket
import subprocess
import sys as _sys
from pathlib import Path as _Path

import yaml

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib import core
from lib import yaml_fast
from lib.kubectl import cluster_for_host, cluster_of, node_names

# Answered by CoreDNS's own `kubernetes` plugin, so it proves the cluster zone loads.
CLUSTER_NAME = "kubernetes.default.svc.cluster.local"
# Answered by Pi-hole's FTL alone and NXDOMAIN everywhere else, so an answer proves CoreDNS
# forwards to Pi-hole rather than to any upstream. It is the name the deleted host task
# resolved (`k3s_coredns_probe_name` in `setup/k3s`'s defaults).
UPSTREAM_NAME = "pi.hole"
# dig bounds itself at 2 tries of 2s; the subprocess timeout only catches a hung binary.
DIG_FLAGS = ("+short", "+time=2", "+tries=2")
DIG_TIMEOUT_S = 15
# dig's exit code for "no reply from the server", which is CoreDNS failing, not dig.
DIG_NO_REPLY = 9

STORAGECLASS = "longhorn"
STORAGECLASS_CM = ("longhorn-system", "longhorn-storageclass", "storageclass.yaml")


def dig(server, name, run=subprocess.run, which=shutil.which):
    """The answer lines dig prints for `name` asked of `server`, or [] when none came back.

    [] covers both an empty answer (NXDOMAIN exits 0 with nothing on stdout) and a server that
    never replied, because both mean the name did not resolve through `server`. `run` and
    `which` are the process boundary, injectable so a test needs no dig and no cluster.

    Raises:
        core.Unreadable: dig is missing, could not run, or failed for its own reasons.
    """
    binary = which("dig")
    if binary is None:
        raise core.Unreadable("dig is not installed on this host (package dnsutils)")
    argv = [binary, *DIG_FLAGS, f"@{server}", name]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=DIG_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise core.Unreadable(f"{' '.join(argv)} did not run: {exc}") from None
    if proc.returncode == DIG_NO_REPLY:
        return []
    if proc.returncode != 0:
        raise core.Unreadable(
            f"{' '.join(argv)} exited {proc.returncode}: {proc.stderr.strip()[:300]}"
        )
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and not line.startswith(";")
    ]


def require_a_node_of_the_served_cluster(read, hostname=socket.gethostname):
    """Refuse to dig unless this host is a node of the cluster kubectl serves.

    The kubectl reads carry `lib.kubectl`'s cluster refusal, but dig does not: it reaches
    whichever CoreDNS this host's kube-proxy routes the ClusterIP to, and nothing at all from
    a host outside the cluster. Without this, a green answer could be about another cluster,
    and a red one about a host that cannot route a ClusterIP.

    Raises:
        core.Unreadable: this host's cluster is unknown or differs from the served one.
    """
    served = cluster_of(node_names(read(["get", "nodes", "-o", "json"])))
    here = cluster_for_host(hostname())
    if here is None or here != served:
        raise core.Unreadable(
            f"this host ({hostname()}) is a node of {here or 'no known cluster'}, and kubectl "
            f"serves {served or 'an unknown cluster'}; a dig through kube-dns from here would "
            "not read that cluster's CoreDNS. Run probe.py on a node of the gated cluster"
        )


def coredns(read, resolve=dig, hostname=socket.gethostname):
    """(failures, notes) for CoreDNS: a non-empty `NodeHosts`, and both names resolving.

    Raises:
        core.Unreadable: a kubectl read failed, or `require_a_node_of_the_served_cluster`
            refused the dig.
    """
    failures, notes = [], []
    cm = read(["get", "configmap", "coredns", "-n", "kube-system", "-o", "json"])
    hosts = ((cm or {}).get("data") or {}).get("NodeHosts") or ""
    if cm is None:
        failures.append("ConfigMap kube-system/coredns is MISSING")
    elif not hosts.strip():
        failures.append(
            "ConfigMap kube-system/coredns has an empty NodeHosts key — k3s's node "
            "controller writes it, and without it pods cannot resolve the node names"
        )
    else:
        notes.append(f"NodeHosts lists {len(hosts.strip().splitlines())} hosts")

    svc = read(["get", "service", "kube-dns", "-n", "kube-system", "-o", "json"])
    ip = ((svc or {}).get("spec") or {}).get("clusterIP")
    if not ip or ip == "None":
        failures.append(
            "Service kube-system/kube-dns is MISSING or has no clusterIP — no address "
            "to resolve through"
        )
        return failures, notes
    require_a_node_of_the_served_cluster(read, hostname)
    for name in (CLUSTER_NAME, UPSTREAM_NAME):
        answer = resolve(ip, name)
        if answer:
            notes.append(f"{name} resolves through kube-dns {ip}: {', '.join(answer)}")
        else:
            failures.append(
                f"{name} did not resolve through kube-dns {ip} — the Corefile loads "
                "but does not answer it"
            )
    return failures, notes


def _declared_class(text):
    """The StorageClass document in the ConfigMap's key, or None when it holds none."""
    try:
        docs = list(yaml_fast.safe_load_all(text or ""))
    except yaml.YAMLError:
        return None
    return next(
        (d for d in docs if isinstance(d, dict) and d.get("kind") == "StorageClass"),
        None,
    )


def _as_param(value):
    """A parameter value as the API server stores it: a string, or `<absent>`."""
    if value is None:
        return "<absent>"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parameter_diffs(live, declared):
    """`key: live=..., ConfigMap=...` for every parameter whose value differs.

    An absent key and an empty string are different values: `fromBackup: ""` is declared.
    """
    return [
        f"{key}: live={_as_param(live.get(key))}, ConfigMap={_as_param(declared.get(key))}"
        for key in sorted(set(live) | set(declared))
        if _as_param(live.get(key)) != _as_param(declared.get(key))
    ]


def longhorn_storageclass(read):
    """(failures, notes) for the live `longhorn` class against the ConfigMap's class."""
    namespace, name, key = STORAGECLASS_CM
    live = read(["get", "storageclass", STORAGECLASS, "-o", "json"])
    cm = read(["get", "configmap", name, "-n", namespace, "-o", "json"])
    if live is None:
        return [f"StorageClass {STORAGECLASS} is MISSING"], []
    if cm is None:
        return [f"ConfigMap {namespace}/{name} is MISSING"], []
    declared = _declared_class((cm.get("data") or {}).get(key))
    if declared is None:
        return [f"ConfigMap {namespace}/{name}'s {key} key holds no StorageClass"], []
    diffs = parameter_diffs(
        live.get("parameters") or {}, declared.get("parameters") or {}
    )
    if diffs:
        return [
            f"StorageClass {STORAGECLASS}'s parameters differ from ConfigMap "
            f"{namespace}/{name}: {'; '.join(diffs)}. Parameters are immutable, so the fix "
            f"is an operator-only `kubectl delete storageclass {STORAGECLASS}`, which "
            "longhorn-manager recreates from the ConfigMap"
        ], []
    count = len(live.get("parameters") or {})
    return [], [
        f"StorageClass {STORAGECLASS} matches {namespace}/{name} ({count} parameters)"
    ]


CHECKS = {"coredns": coredns, "longhorn": longhorn_storageclass}


def with_checks(tag, text, read, checks=CHECKS):
    """(text, exit code) for the passing rollout verdict `text`, with `tag`'s check folded in.

    `read` is `health._read` bound to the gate's cluster and kubectl boundary. A failing check
    puts its own head line first, because the deploy notifier reads only the first line.
    """
    check = checks.get(tag)
    if check is None:
        return text, 0
    failures, notes = check(read)
    lines = [f"  FAILED: {f}" for f in failures] + [f"  {n}" for n in notes]
    if failures:
        head = f"{tag}: the plumbing check FAILED the gate after a healthy rollout"
        return "\n".join([head, *lines, text]), 1
    return "\n".join([text, *lines]), 0
