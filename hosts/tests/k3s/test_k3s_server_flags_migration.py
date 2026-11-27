"""The server's flags survive the move from the installer's unit to config.yaml.d (H6.4b).

Before H6.4b, `k3s_server_args` in setup/k3s reached k3s as the installer's `ExecStart`. H6.4b
deletes it and renders `k3s_server_config` into /etc/rancher/k3s/config.yaml.d/50-homelab.yaml
instead, and the next restart window restarts k3s with no command-line flags at all. A flag lost
in the move fails silently or wedges the node: a dropped `disable` brings back the bundled
Traefik, a changed `node-ip` hits the etcd peer-URL hang, and a missing authentication config
stops the API server from starting.

MERGE_BASE_ARGS is `k3s_server_args` as the installer received it at the merge base,
f3b006b77f, with daniel-box's inventory. The live node's `k3s.io/node-args` annotation read the
same 15 flags on 2026-10-29. A later deliberate flag change, such as H6.7's, edits this
fixture in the same PR.
"""

import functools
import re

from lib import yaml_fast
from lib.repo_paths import K3S_HOST_CHECKS_DEFAULTS
from renderer.renderer_lib import host as host_mode

CONFIG = "/etc/rancher/k3s/config.yaml.d/50-homelab.yaml"

MERGE_BASE_ARGS = [
    "server",
    "--cluster-init",
    "--disable=traefik",
    "--disable=servicelb",
    "--disable=local-storage",
    "--write-kubeconfig-mode",
    "0640",
    "--node-ip",
    "10.0.0.215",
    "--advertise-address",
    "10.0.0.215",
    "--kubelet-arg=allowed-unsafe-sysctls=net.ipv4.conf.all.src_valid_mark,net.ipv4.ip_forward",
    "--secrets-encryption",
    "--kube-apiserver-arg=audit-policy-file=/var/lib/rancher/k3s/server/audit.yaml",
    "--kube-apiserver-arg=audit-log-path=/var/lib/rancher/k3s/server/logs/audit.log",
    "--kube-apiserver-arg=audit-log-maxage=30",
    "--kube-apiserver-arg=audit-log-maxbackup=5",
    "--kube-apiserver-arg=audit-log-maxsize=100",
    "--kube-apiserver-arg=authentication-config=/var/lib/rancher/k3s/server/authentication-config.yaml",
    # H2.8a: native etcd snapshots to S3.
    "--etcd-s3",
    "--etcd-s3-config-secret=k3s-etcd-snapshot-s3-config",
    "--etcd-snapshot-name=offbox",
    "--etcd-snapshot-compress",
    "--etcd-snapshot-schedule-cron=0 */12 * * *",
    "--etcd-snapshot-retention=28",
    # Image GC below Longhorn's replica scheduling floor.
    "--kubelet-arg=image-gc-high-threshold=65",
    "--kubelet-arg=image-gc-low-threshold=55",
    # Memory pods use without requesting it.
    "--kubelet-arg=system-reserved=memory=7Gi",
    # The runtime's default seccomp profile for every container whose pod sets none.
    "--kubelet-arg=seccomp-default=true",
    # Leader-election timings long enough to ride out a stalled control plane.
    *[
        f"--{component}-arg=leader-elect-{name}"
        for component in (
            "kube-controller-manager",
            "kube-scheduler",
            "kube-cloud-controller-manager",
        )
        for name in ("lease-duration=60s", "renew-deadline=45s", "retry-period=5s")
    ],
]

LEADER_ELECTING_COMPONENTS = (
    "kube-controller-manager-arg",
    "kube-scheduler-arg",
    "kube-cloud-controller-manager-arg",
)
# Seconds: the longest gap in /var/log/k3s.log measured before a `leaderelection lost` exit.
LONGEST_MEASURED_STALL_S = 42


def cli_flags(argv: list[str]) -> list[tuple[str, str]]:
    """`(flag, value)` pairs from a `k3s server` or `k3s agent` command line.

    A bare flag reads `true`.
    """
    assert argv[0] in ("server", "agent")
    pairs, rest = [], argv[1:]
    while rest:
        token, rest = rest[0], rest[1:]
        name, sep, value = token.removeprefix("--").partition("=")
        if not sep:
            if rest and not rest[0].startswith("--"):
                value, rest = rest[0], rest[1:]
            else:
                value = "true"
        pairs.append((name, value))
    return sorted(pairs)


def config_flags(config: dict) -> list[tuple[str, str]]:
    """The flags k3s derives from a config file.

    `pkg/configfilearg/parser.go` `readConfigFile` at the pinned tag turns each list item into
    its own `--key=item` and any other value into `--key=value`, a YAML true reading `true`.
    """
    pairs = []
    for key, value in config.items():
        for item in value if isinstance(value, list) else [value]:
            pairs.append(
                (key, str(item).lower() if isinstance(item, bool) else str(item))
            )
    return sorted(pairs)


@functools.cache
def rendered_config() -> dict:
    package = host_mode.render_host("daniel-box")
    return yaml_fast.safe_load(package.files[CONFIG][0].decode())


def test_the_packaged_flags_equal_the_merge_base_command_line():
    assert config_flags(rendered_config()) == cli_flags(MERGE_BASE_ARGS)


def test_a_flag_lost_in_the_move_is_detected():
    config = dict(rendered_config())
    config["disable"] = [item for item in config["disable"] if item != "servicelb"]
    assert config_flags(config) != cli_flags(MERGE_BASE_ARGS)


def reserved_memory_problems(config: dict) -> list[str]:
    """The kubelet must reserve memory, so allocatable excludes what unrequested pods use.

    Longhorn's instance-manager has no memory request, so without a reserve the
    scheduler counts its working set as free.
    """
    for item in config.get("kubelet-arg", []):
        name, _, value = item.partition("=")
        if name in ("system-reserved", "kube-reserved") and "memory=" in value:
            return []
    return ["kubelet-arg reserves no memory"]


def test_the_server_kubelet_reserves_memory():
    assert reserved_memory_problems(rendered_config()) == []


def test_the_server_kubelet_defaults_every_container_to_the_runtime_seccomp_profile():
    assert "seccomp-default=true" in rendered_config()["kubelet-arg"]


def test_a_kubelet_without_reserved_memory_is_flagged():
    config = dict(rendered_config())
    config["kubelet-arg"] = [
        item for item in config["kubelet-arg"] if not item.startswith("system-reserved")
    ]
    assert reserved_memory_problems(config) == ["kubelet-arg reserves no memory"]


def image_gc_problems(config: dict) -> list[str]:
    """The kubelet's image-GC thresholds must sit below Longhorn's replica scheduling floor.

    The image filesystem is the root volume Longhorn's disk shares, and
    k3s-host-checks' `k3s_disk_max_pct` is that floor. GC that starts above it leaves every
    superseded image on disk until Longhorn stops scheduling replicas. The kubelet
    refuses to start when the low threshold is not below the high one.
    """
    match = re.search(
        r"^k3s_disk_max_pct:\s*(\d+)\s*$", K3S_HOST_CHECKS_DEFAULTS.read_text(), re.M
    )
    assert match, "k3s-host-checks no longer declares k3s_disk_max_pct as a plain value"
    floor = int(match.group(1))
    args = dict(item.partition("=")[::2] for item in config.get("kubelet-arg", []))
    high = args.get("image-gc-high-threshold")
    low = args.get("image-gc-low-threshold")
    if high is None or low is None:
        return ["kubelet-arg lacks an image-gc-high/low-threshold pair"]
    problems = []
    if not int(high) < floor:
        problems.append(f"image-gc-high-threshold {high} is not below {floor}")
    if not int(low) < int(high):
        problems.append(f"image-gc-low-threshold {low} is not below {high}")
    return problems


def test_the_server_starts_image_gc_below_the_longhorn_floor():
    assert image_gc_problems(rendered_config()) == []


def leader_election_problems(config: dict) -> list[str]:
    """Each leader-electing component must outlast a stalled control plane, or k3s exits 1.

    The component refuses to start unless lease > renew > 1.2 x retry, and a stall longer
    than the renew deadline costs it the lease.
    """
    problems = []
    for key in LEADER_ELECTING_COMPONENTS:
        args = dict(item.partition("=")[::2] for item in config.get(key, []))
        try:
            lease, renew, retry = (
                int(args[f"leader-elect-{name}"].removesuffix("s"))
                for name in ("lease-duration", "renew-deadline", "retry-period")
            )
        except KeyError, ValueError:
            problems.append(f"{key} lacks whole-second leader-elect timings")
            continue
        if not lease > renew > 1.2 * retry:
            problems.append(
                f"{key}: lease {lease}s > renew {renew}s > 1.2 x retry {retry}s fails"
            )
        if renew <= LONGEST_MEASURED_STALL_S:
            problems.append(
                f"{key}: renew deadline {renew}s does not outlast a {LONGEST_MEASURED_STALL_S}s stall"
            )
    return problems


def test_every_leader_electing_component_outlasts_the_measured_stall():
    assert leader_election_problems(rendered_config()) == []


def test_timings_the_component_refuses_are_flagged():
    config = dict(rendered_config())
    config["kube-scheduler-arg"] = [
        "leader-elect-lease-duration=45s",
        "leader-elect-renew-deadline=60s",
        "leader-elect-retry-period=5s",
    ]
    assert leader_election_problems(config) == [
        "kube-scheduler-arg: lease 45s > renew 60s > 1.2 x retry 5s fails"
    ]
