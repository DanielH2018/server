"""Guard: the Pi's node_exporter host unit keeps the container's collector set and version.

node-exporter runs on daniel-pi as a systemd unit. Three things must not
drift silently: the collector flags, which monitor-bridge's Pi verdicts read series from
(`node_hwmon_*`, `node_filesystem_*`, `node_memory_*`); the version, which the cluster's
DaemonSet pins and the two should share; and the textfile directory, which homelab-updater
writes `homelab_host.prom` into and never creates. The version's oracle is in the tree:
`roles/k8s/node-exporter/defaults/main.yml`. The flags' oracle is `CONTAINER_FLAGS` below, a
record of what the retired container ran, with the textfile collector turned on (H3.6).

Run: uv run pytest ansible/tests/setup/test_pi_node_exporter_host_unit.py
"""

import re

from lib import yaml_fast
from _helpers import K8S_ROLES, SETUP_ROLES, load_tasks, walk_tasks
from _setup_render import rendered_setup_text

UNIT = "node_exporter.service.j2"
DEFAULTS = SETUP_ROLES / "optimize_pi" / "defaults" / "main.yml"
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
TASKS = SETUP_ROLES / "optimize_pi" / "tasks" / "main.yml"
# M3's owner contract for the directory.
TEXTFILE_OWNER = {"owner": "root", "group": "{{ sys_user }}", "mode": "2775"}

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
    # The RENDERED unit: the listen address interpolates the Pi's LAN IP, so a source read
    # already had to be told to drop a `--web.listen-address={{ ... }}` rather than an
    # address, and any collector flag that moves into a role default goes the same way (#3202).
    unit_flags = collector_flags(rendered_setup_text("optimize_pi", UNIT), UNIT_ONLY)
    assert unit_flags == UNIT_FLAGS, (
        f"unit-only: {sorted(unit_flags - UNIT_FLAGS)}; "
        f"expected but missing: {sorted(UNIT_FLAGS - unit_flags)}"
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
    # The cluster pin may carry a `@sha256:` digest after the tag; the host unit has no digest.
    cluster = image.rsplit(":v", 1)[1].split("@", 1)[0]
    assert host == cluster, (
        f"optimize_pi pins node_exporter {host}, the cluster DaemonSet {cluster}; "
        "bump both, and the sha256 beside the host version"
    )


def textfile_dir_problems(tasks: list[dict]) -> list[str]:
    """Why the tasks do not create the textfile directory as M3's contract says, if they don't."""
    creators = [
        task
        for task in walk_tasks(tasks)
        if (task.get("ansible.builtin.file") or {}).get("path") == TEXTFILE_DIR
    ]
    if len(creators) != 1:
        return [f"{len(creators)} tasks create {TEXTFILE_DIR}, not one"]
    (task,) = creators
    args = task["ansible.builtin.file"]
    problems = [
        f"{key} is {args.get(key)!r}, not {want!r}"
        for key, want in {"state": "directory", **TEXTFILE_OWNER}.items()
        if str(args.get(key)) != want
    ]
    if "node-exporter-host" not in task.get("tags", []):
        problems.append("the task lacks the node-exporter-host tag")
    return problems


def test_the_role_creates_the_textfile_directory_with_m3s_owner_and_mode() -> None:
    assert textfile_dir_problems(load_tasks(TASKS)) == []


def test_a_textfile_directory_without_the_group_write_bit_is_flagged() -> None:
    task = {
        "name": "Create it",
        "tags": ["node-exporter-host"],
        "ansible.builtin.file": {
            "path": TEXTFILE_DIR,
            "state": "directory",
            **TEXTFILE_OWNER,
            "mode": "0755",
        },
    }
    assert textfile_dir_problems([task]) == ["mode is '0755', not '2775'"]
