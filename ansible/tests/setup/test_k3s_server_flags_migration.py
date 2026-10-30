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

from lib import yaml_fast
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
]


def cli_flags(argv: list[str]) -> list[tuple[str, str]]:
    """`(flag, value)` pairs from a `k3s server` command line, a bare flag reading `true`."""
    assert argv[0] == "server"
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
