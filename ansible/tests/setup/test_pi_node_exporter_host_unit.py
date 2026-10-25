"""Guard: the Pi's node_exporter host unit keeps the container's collector set and version.

node-exporter runs on daniel-pi as a systemd unit that `homelab-daniel-pi` ships (the
`pi-node-exporter` component, H7.25). Three things must not drift silently: the collector flags,
which monitor-bridge's Pi verdicts read series from (`node_hwmon_*`, `node_filesystem_*`,
`node_memory_*`); the version, which the cluster's DaemonSet pins and the two should share; and
the textfile directory, which homelab-updater writes `homelab_host.prom` into and never creates.
The version's oracle is in the tree: `roles/k8s/node-exporter/defaults/main.yml`. The flags'
oracle is `CONTAINER_FLAGS` below, a record of what the retired container ran, with the textfile
collector turned on (H3.6).

Run: uv run pytest ansible/tests/setup/test_pi_node_exporter_host_unit.py
"""

import re

from lib import yaml_fast
from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from _helpers import K8S_ROLES

UNIT = "/usr/lib/systemd/system/node_exporter.service"
TMPFILES = "/usr/lib/tmpfiles.d/homelab-node-exporter.conf"
TOOLS = REPO / "hosts/packages/homelab-tools/package.yml"
# The container's collector flags, minus its `--path.*` remaps, as `collector_flags` parsed them
# from the retired compose template. #2385 deleted that template; read it with
# `git show 2460d0675fd748e70fcbcde87185371ffd62402b:ansible/roles/containers/archive/node-exporter/templates/docker-compose.yml.j2`.
CONTAINER_FLAGS = frozenset(
    {
        "--collector.filesystem.mount-points-exclude=^/(dev|proc|sys|run|var/lib/docker/.+)($|/)",
        "--no-collector.arp",
        "--no-collector.netclass",
        "--no-collector.textfile",
        "--no-collector.wifi",
    }
)
# H3.6 turns the textfile collector on, over the directory M3's contract describes.
TEXTFILE_DIR = "/var/lib/node-exporter-textfile"
UNIT_FLAGS = (CONTAINER_FLAGS - {"--no-collector.textfile"}) | {
    f"--collector.textfile.directory={TEXTFILE_DIR}"
}
CLUSTER_DEFAULTS = K8S_ROLES / "node-exporter" / "defaults" / "main.yml"

# Flags that only make sense on one side: the container remapped the host's trees, the
# unit picks its own listen address.
CONTAINER_ONLY = re.compile(r"^--path\.")
UNIT_ONLY = re.compile(r"^--web\.listen-address=")


def collector_flags(text: str, drop: re.Pattern[str]) -> frozenset[str]:
    """Every `--flag[=value]` in the text, minus the side-specific ones, `$$` unescaped."""
    # ExecStartPre= runs systemd-tmpfiles, whose flags are not node_exporter's.
    code = "\n".join(
        ln
        for ln in text.splitlines()
        if not ln.lstrip().startswith(("#", "ExecStartPre="))
    )
    flags = re.findall(r"--[a-z][\w.-]*(?:=\S+)?", code)
    return frozenset(
        f.rstrip("'\"\\").replace("$$", "$") for f in flags if not drop.search(f)
    )


def _shipped(path: str) -> str:
    return host_mode.render_host("daniel-pi").files[path][0].decode()


def test_the_unit_runs_the_collector_set_the_container_ran() -> None:
    # The RENDERED unit: the listen address interpolates the Pi's LAN IP, so a source read
    # already had to be told to drop a `--web.listen-address={{ ... }}` rather than an
    # address, and any collector flag that moves into a variable goes the same way (#3202).
    unit_flags = collector_flags(_shipped(UNIT), UNIT_ONLY)
    assert unit_flags == UNIT_FLAGS, (
        f"unit-only: {sorted(unit_flags - UNIT_FLAGS)}; "
        f"expected but missing: {sorted(UNIT_FLAGS - unit_flags)}"
    )


def test_a_dropped_collector_flag_is_noticed() -> None:
    assert collector_flags("ExecStart=/x --a --no-collector.wifi", UNIT_ONLY) != (
        collector_flags("- '--a'\n", CONTAINER_ONLY)
    )


def test_the_host_binary_pins_the_version_the_cluster_pins() -> None:
    tool = yaml_fast.safe_load(TOOLS.read_text())["tools"]["node-exporter"]
    versions = {arch: str(pin["version"]) for arch, pin in tool.items()}
    image = yaml_fast.safe_load(CLUSTER_DEFAULTS.read_text())["node_exporter_k8s_image"]
    # The cluster pin may carry a `@sha256:` digest after the tag; the host binary has no digest.
    cluster = image.rsplit(":v", 1)[1].split("@", 1)[0]
    assert set(versions.values()) == {cluster}, (
        f"homelab-tools pins node_exporter {versions}, the cluster DaemonSet {cluster}; "
        "bump both, and the digests beside the host versions"
    )


def test_the_unit_runs_the_binary_homelab_tools_installs() -> None:
    assert re.search(
        r"^ExecStart=/usr/lib/homelab/bin/node-exporter ", _shipped(UNIT), re.M
    )


def textfile_dir_problems(tmpfiles: str, unit: str, sys_user: str) -> list[str]:
    """Why the package does not create the textfile directory as M3's contract says, if it doesn't."""
    lines = [
        ln.split()
        for ln in tmpfiles.splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    ours = [ln for ln in lines if ln[1:2] == [TEXTFILE_DIR]]
    if len(ours) != 1:
        return [f"{len(ours)} tmpfiles lines create {TEXTFILE_DIR}, not one"]
    kind, _, mode, owner, group = ours[0][:5]
    problems = [
        f"{what} is {got!r}, not {want!r}"
        for what, got, want in (
            ("type", kind, "d"),
            ("mode", mode, "2775"),
            ("owner", owner, "root"),
            ("group", group, sys_user),
        )
        if got != want
    ]
    if f"ExecStartPre=+/usr/bin/systemd-tmpfiles --create {TMPFILES}" not in unit:
        problems.append("the unit does not create the directory before it starts")
    return problems


def test_the_package_creates_the_textfile_directory_with_m3s_owner_and_mode() -> None:
    sys_user = render_context(None, "daniel-pi", inventory=REPO_INVENTORY)["sys_user"]
    assert textfile_dir_problems(_shipped(TMPFILES), _shipped(UNIT), sys_user) == []


def test_a_textfile_directory_without_the_group_write_bit_is_flagged() -> None:
    line = f"d {TEXTFILE_DIR} 0755 root ubuntu -\n"
    unit = f"ExecStartPre=+/usr/bin/systemd-tmpfiles --create {TMPFILES}\n"
    assert textfile_dir_problems(line, unit, "ubuntu") == ["mode is '0755', not '2775'"]
    assert textfile_dir_problems(line.replace("0755", "2775"), "", "ubuntu") == [
        "the unit does not create the directory before it starts"
    ]
