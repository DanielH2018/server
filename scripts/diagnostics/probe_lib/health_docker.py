"""`probe.py health --docker <svc>`, and the direct-address lookups the arr probes need.

Split out of probe_lib/health.py, which had grown to 938 lines. Two groups that both answer
"reach this service directly": daniel-pi's Docker containers (the only Docker host left) and a
k8s Service's ClusterIP, which replaced the bridge-IP lookup for the cluster's own workloads.

WHAT "NOT FOUND" IS ALLOWED TO MEAN governs `format_health`'s two absence messages, and the
canonical statement of that rule is health.py's module docstring. A container daniel-pi's
inventory DECLARES and the host lacks is a failed deploy and must not skip; an undeclared name
is a block tag or a typo and is the only absence the notifier may skip. Read that paragraph
before rewording either message.
"""

import subprocess

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# `core.<name>` for anything the tests monkeypatch — binding those into this module's
# globals with a `from core import ...` would take a snapshot the patch never reaches.
from diagnostics.probe_lib import core

from lib.repo_paths import HOST_VARS

PI_HOST_VARS = HOST_VARS / "daniel-pi.yml"


def inspect_ip_argv(container):
    return [
        "docker",
        "inspect",
        "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
        container,
    ]


def parse_ip(inspect_output):
    """First non-empty token of `docker inspect`'s IP list.

    Host can reach any of a container's bridge IPs. None if the container has no
    address.
    """
    for tok in inspect_output.split():
        if tok:
            return tok
    return None


def inspect_argv(container):
    return ["docker", "inspect", container]


# DECIDED: the ClusterIP pair below stays here, not in health_kubectl.py. `k8s_service_ip_argv`
# and `resolve_service_ip` are kubectl by transport but Docker by role — they are the successor
# to `inspect_ip_argv`/`resolve_ip`, answering the same question ("reach this service directly,
# bypassing Traefik and Authelia") for the workloads that left the Pi. Moving them would split
# that pair across two modules and falsify health_kubectl.py's invariant: it imports no sibling
# and runs no command, so every argv shape the gate depends on is assertable without a cluster,
# while `resolve_service_ip` runs a subprocess and imports `core`. The module NAME is what
# misleads a reader here, not the grouping. Enforced by test_probe_health.py::
# test_health_kubectl_imports_nothing_so_its_argv_shapes_need_no_cluster.
def k8s_service_ip_argv(service, namespace):
    """kubectl argv for a Service's ClusterIP.

    The k8s analog of inspect_ip_argv, for apps (arr) that must be reached directly
    rather than through k8s_endpoint.
    """
    return [
        "k3s",
        "kubectl",
        "-n",
        namespace,
        "get",
        "service",
        service,
        "-o",
        "jsonpath={.spec.clusterIP}",
    ]


def format_health(data, container, declared=False):
    """Summarize a container's state + healthcheck from `docker inspect` output.

    Pure: takes the parsed JSON list and returns (text, exit_code). exit_code is 0
    only when the container is running and (has no healthcheck, or is healthy) — so
    `probe.py health <svc>` is usable as a post-deploy gate.

    `declared` says whether daniel-pi's inventory lists a Docker service by this name;
    `run_health` resolves it. It splits the two situations an absent container can mean, which
    a single "not found" message conflated: a declared service that is missing is a deploy that
    failed and must fail the gate, while an undeclared name is a block tag or a typo and is the
    only one the notifier may skip.
    """
    if not data:
        if declared:
            return (
                f"{container}: MISSING — daniel-pi's inventory declares this service and the "
                "host has no such container, so the deploy did not create it",
                1,
            )
        return (
            f"{container}: not found, and not a declared service on any host "
            "(nothing to health-check)",
            1,
        )
    state = data[0].get("State") or {}
    status = state.get("Status", "unknown")
    restarts = data[0].get("RestartCount", 0)
    health = state.get("Health")
    if health:
        hstatus = health.get("Status", "unknown")
        line = f"{container}: {status}, health={hstatus}, restarts={restarts}"
        if hstatus != "healthy":
            line += f" — failing streak {health.get('FailingStreak', 0)}"
            log = health.get("Log") or []
            last = (log[-1].get("Output") or "").strip().splitlines() if log else []
            if last:
                line += f"; last check: {last[-1][:160]}"
        return (line, 0 if status == "running" and hstatus == "healthy" else 1)
    return (
        f"{container}: {status} (no healthcheck), restarts={restarts}",
        0 if status == "running" else 1,
    )


def resolve_service_ip(name):
    """A workload's k8s Service ClusterIP.

    The k8s replacement for `docker inspect`ing a container's bridge IP. A ClusterIP is
    stable across pod restarts and redeploys, so this does not reintroduce
    the hand-copied-IP staleness the docker lookup existed to avoid. Callers reach the
    Service directly rather than through k8s_endpoint, which would put Traefik and Authelia
    in front of an API path that has no bypass rule.
    """
    ns = core.k8s_namespace()
    out = subprocess.run(k8s_service_ip_argv(name, ns), capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"kubectl get service {name} failed: {out.stderr.strip()}")
    ip = out.stdout.strip()
    if not ip:
        raise SystemExit(f"{name} has no ClusterIP (does the Service exist?)")
    return ip


def resolve_ip(container):
    """A Docker container's bridge IP.

    daniel-pi is the only host that still has Docker — on either cluster node this raises
    FileNotFoundError, so use resolve_service_ip.
    """
    out = subprocess.run(inspect_ip_argv(container), capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"docker inspect {container} failed: {out.stderr.strip()}")
    ip = parse_ip(out.stdout)
    if not ip:
        raise SystemExit(f"{container} has no container IP (is it running?)")
    return ip


def declared_on_pi(container, host_vars=None):
    """Does daniel-pi's inventory declare a Docker service by this name?

    The Pi is the only Docker host left, so its `containers_list` is the whole population an
    absent container can be measured against.

    Args:
        container: The service name an absent container was asked about.
        host_vars: The inventory file to read, defaulting to `PI_HOST_VARS`. A parameter
            rather than a module global so the unreadable-inventory case is exercised by
            passing a path, not by patching this module — a patch would pin the name here and
            break the moment it moved. The default resolves per call rather than in the
            signature, so rebinding `PI_HOST_VARS` on the module still reaches this.
    """
    import yaml

    path = host_vars or PI_HOST_VARS
    try:
        entries = (yaml.safe_load(path.read_text()) or {}).get("containers_list") or []
    except OSError, yaml.YAMLError:
        # Fail closed: an unreadable inventory must not turn a missing container into a skip.
        return True
    return container in {
        entry.get("name") for entry in entries if isinstance(entry, dict)
    }
