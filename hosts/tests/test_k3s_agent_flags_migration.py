"""The agent's flags survive the move from the installer's unit to config.yaml.d (H6.5b).

Before H6.5b, `k3s_agent_args` in setup/k3s reached k3s-agent as the installer's `ExecStart`.
H6.5b deletes it and renders `k3s_agent_config` into
/etc/rancher/k3s/config.yaml.d/50-homelab.yaml instead, and the agent's next start runs a bare
`k3s agent`. A flag lost in the move fails silently: a dropped `node-ip` lets k3s autodetect the
address on a second NIC, and a dropped `kubelet-arg` makes the kubelet reject every pod that
sets the two sysctls.

LIVE_ARGS is daniel-server's `k3s.io/node-args` annotation, read on 2026-11-03 with
`kubectl get node daniel-server -o jsonpath='{.metadata.annotations.k3s\\.io/node-args}'`. k3s
writes it from the command line it started with, and it equals `k3s_agent_args` at the merge
base, 425dcdfd98, rendered with daniel-server's inventory. K3S_URL and K3S_TOKEN come from the installer's
env file, not the command line, so neither appears. A later deliberate flag change edits this
fixture in the same PR: the image-gc pair is #8307's, the memory reserve #8644's and
`seccomp-default` the last entry's, each live from the agent's next start.
"""

import functools

from lib import yaml_fast
from renderer.renderer_lib import host as host_mode
from test_k3s_server_flags_migration import (
    cli_flags,
    config_flags,
    image_gc_problems,
    reserved_memory_problems,
)

CONFIG = "/etc/rancher/k3s/config.yaml.d/50-homelab.yaml"

LIVE_ARGS = [
    "agent",
    "--node-ip",
    "10.0.0.161",
    "--node-label",
    "node.longhorn.io/create-default-disk=false",
    "--kubelet-arg",
    "allowed-unsafe-sysctls=net.ipv4.conf.all.src_valid_mark,net.ipv4.ip_forward",
    "--kubelet-arg",
    "image-gc-high-threshold=65",
    "--kubelet-arg",
    "image-gc-low-threshold=55",
    "--kubelet-arg",
    "system-reserved=memory=4Gi",
    "--kubelet-arg",
    "seccomp-default=true",
]


@functools.cache
def rendered_config() -> dict:
    package = host_mode.render_host("daniel-server")
    return yaml_fast.safe_load(package.files[CONFIG][0].decode())


def test_the_packaged_flags_equal_the_live_command_line():
    assert config_flags(rendered_config()) == cli_flags(LIVE_ARGS)


def test_a_flag_lost_in_the_move_is_detected():
    config = dict(rendered_config())
    del config["kubelet-arg"]
    assert config_flags(config) != cli_flags(LIVE_ARGS)


def test_the_agent_starts_image_gc_below_the_longhorn_floor():
    assert image_gc_problems(rendered_config()) == []


def test_an_image_gc_threshold_at_the_kubelet_default_is_flagged():
    config = dict(rendered_config())
    config["kubelet-arg"] = [
        "image-gc-high-threshold=85" if item.startswith("image-gc-high") else item
        for item in config["kubelet-arg"]
    ]
    assert image_gc_problems(config) == ["image-gc-high-threshold 85 is not below 75"]


def test_a_low_threshold_not_below_the_high_one_is_flagged():
    config = dict(rendered_config())
    config["kubelet-arg"] = [
        "image-gc-low-threshold=65" if item.startswith("image-gc-low") else item
        for item in config["kubelet-arg"]
    ]
    assert image_gc_problems(config) == ["image-gc-low-threshold 65 is not below 65"]


def test_the_agent_kubelet_reserves_memory():
    assert reserved_memory_problems(rendered_config()) == []


def test_the_agent_kubelet_defaults_every_container_to_the_runtime_seccomp_profile():
    assert "seccomp-default=true" in rendered_config()["kubelet-arg"]
