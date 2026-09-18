"""Guard: the Pi's node_exporter host unit keeps the container's collector set and version.

node-exporter left Docker on daniel-pi for a systemd unit (#2005). Two things must not
drift silently: the collector flags, which monitor-bridge's Pi verdicts read series from
(`node_hwmon_*`, `node_filesystem_*`, `node_memory_*`), and the version, which the cluster's
DaemonSet pins and the two should share. Both have a real oracle in the tree -- the archived
compose template for the flags, `roles/k8s/node-exporter/defaults/main.yml` for the version.

Run: uv run pytest ansible/tests/setup/test_pi_node_exporter_host_unit.py
"""

import re

from lib import yaml_fast
from _helpers import CONTAINER_ROLES, K8S_ROLES, SETUP_ROLES

UNIT = SETUP_ROLES / "optimize_pi" / "templates" / "node_exporter.service.j2"
DEFAULTS = SETUP_ROLES / "optimize_pi" / "defaults" / "main.yml"
ARCHIVED_COMPOSE = (
    CONTAINER_ROLES
    / "archive"
    / "node-exporter"
    / "templates"
    / "docker-compose.yml.j2"
)
CLUSTER_DEFAULTS = K8S_ROLES / "node-exporter" / "defaults" / "main.yml"

# Flags that only make sense on one side: the container remapped the host's trees, the
# unit picks its own listen address.
CONTAINER_ONLY = re.compile(r"^--path\.")
UNIT_ONLY = re.compile(r"^--web\.listen-address=")


def collector_flags(text: str, drop: re.Pattern[str]) -> frozenset[str]:
    """Every `--flag[=value]` in the text, minus the side-specific ones, `$$` unescaped."""
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    flags = re.findall(r"--[a-z][\w.-]*(?:=\S+)?", code)
    return frozenset(
        f.rstrip("'\"\\").replace("$$", "$") for f in flags if not drop.search(f)
    )


def test_the_unit_runs_the_collector_set_the_container_ran() -> None:
    unit_flags = collector_flags(UNIT.read_text(), UNIT_ONLY)
    container_flags = collector_flags(ARCHIVED_COMPOSE.read_text(), CONTAINER_ONLY)
    assert unit_flags == container_flags, (
        f"unit-only: {sorted(unit_flags - container_flags)}; "
        f"container-only: {sorted(container_flags - unit_flags)}"
    )


def test_a_dropped_collector_flag_is_noticed() -> None:
    assert collector_flags("ExecStart=/x --a --no-collector.wifi", UNIT_ONLY) != (
        collector_flags("- '--a'\n", CONTAINER_ONLY)
    )


def test_the_host_unit_pins_the_version_the_cluster_pins() -> None:
    host = str(
        yaml_fast.safe_load(DEFAULTS.read_text())["optimize_pi_node_exporter_version"]
    )
    image = yaml_fast.safe_load(CLUSTER_DEFAULTS.read_text())["node_exporter_k8s_image"]
    cluster = image.rsplit(":v", 1)[1]
    assert host == cluster, (
        f"optimize_pi pins node_exporter {host}, the cluster DaemonSet {cluster}; "
        "bump both, and the sha256 beside the host version"
    )
