"""The two Claude crons H2.7 moved into setup/claude_code keep their identity and their node.

`Sync peer Claude artifacts` and `Claude Code telemetry health` lived in the k8s artifacts and
observability roles until H2.7 (docs/target-architecture/host/h2-shrink-host-plane.md). The
move is safe on daniel-box only if `claude_code` writes the same /etc/cron.d files with the same
lines and the same script paths. A changed cron `name:` or `cron_file:` leaves the old entry
behind beside the new one, and a changed script path leaves the old script behind.

The crons read values the k8s roles also read: the node the artifacts pod pins, the peer list
it mounts, and the OTLP door the collector binds. A role default does not cross a role
boundary, and a shared key in group_vars/all.yml would route as a deploy-plane change that
never re-applies `claude_code`. So each value is a key pair, and this module holds each pair
equal, resolved the way a deploy resolves it.
"""

import re
from pathlib import Path

from lib import yaml_fast
from lib.render_context import render_context

from _helpers import ROLES, load_tasks, walk_tasks
from _role_census import task_files
from _shell_render import rendered_shell_text

CLAUDE_CODE = ROLES / "setup" / "claude_code"
NODE_CRONS = CLAUDE_CODE / "tasks" / "node_crons.yml"
ARTIFACTS = ROLES / "k8s" / "artifacts"
OBSERVABILITY = ROLES / "k8s" / "observability"

# claude_code key -> (k8s role, how that role spells the same value). A callable reads a value
# the k8s role composes from two keys.
TWINS = {
    "claude_code_artifacts_node": (ARTIFACTS, "artifacts_k8s_node"),
    "claude_code_artifacts_peer_sources": (ARTIFACTS, "artifacts_peer_sources"),
    "claude_code_artifacts_peer_dir": (ARTIFACTS, "artifacts_peer_dir"),
    "claude_code_artifacts_stars_file": (
        ARTIFACTS,
        lambda ctx: f"{ctx['artifacts_stars_dir']}/{ctx['artifacts_stars_name']}",
    ),
    "claude_code_otlp_host_ip": (OBSERVABILITY, "observability_otlp_host_ip"),
    "claude_code_otlp_port": (OBSERVABILITY, "observability_otlp_port"),
}

# What the k8s roles installed before H2.7, copied from their tasks at the move. The cron module
# keys an entry on `name` inside `cron_file`, so these two fields are the identity; the job is
# the script path, plus the journal pipe both jobs gained in #6162.
CRONS_BEFORE_H2_7 = {
    "Sync peer Claude artifacts": {
        "cron_file": "sync-artifacts",
        "job": "/usr/local/bin/sync-artifacts.sh 2>&1 | logger -t sync-artifacts",
        "minute": "*/5",
    },
    "Claude Code telemetry health": {
        "cron_file": "claude-otel-health",
        "job": "/usr/local/bin/claude-otel-health.sh 2>&1 | logger -t claude-otel-health",
        "minute": "*/10",
    },
}
SCRIPTS_BEFORE_H2_7 = {
    "sync-artifacts.sh.j2": "/usr/local/bin/sync-artifacts.sh",
    "telemetry-health.sh.j2": "/usr/local/bin/claude-otel-health.sh",
}


def _context(role: Path) -> dict:
    return render_context(role)


def twin_mismatches(claude_code: dict, k8s: dict[Path, dict]) -> list[str]:
    """One line per twin whose two resolved values differ, or that either side lacks."""
    problems = []
    for key, (role, twin) in TWINS.items():
        ctx = k8s[role]
        theirs = twin(ctx) if callable(twin) else ctx.get(twin)
        ours = claude_code.get(key)
        if ours is None or theirs is None or ours != theirs:
            problems.append(f"{key}={ours!r} but {role.name}'s twin is {theirs!r}")
    return problems


def _live_contexts() -> tuple[dict, dict[Path, dict]]:
    return _context(CLAUDE_CODE), {r: _context(r) for r in (ARTIFACTS, OBSERVABILITY)}


def test_every_twin_resolves_equal_to_its_k8s_key_is_clean():
    ours, theirs = _live_contexts()
    assert twin_mismatches(ours, theirs) == []
    # Non-vacuity: a key that resolved to nothing on both sides would compare equal.
    assert ours["claude_code_artifacts_node"] == "daniel-box"
    assert ours["claude_code_artifacts_peer_sources"][0]["name"] == "daniel-server"
    assert ours["claude_code_otlp_port"] == 4317


def test_a_twin_that_drifted_is_flagged():
    ours, theirs = _live_contexts()
    moved = {
        **theirs,
        ARTIFACTS: {**theirs[ARTIFACTS], "artifacts_k8s_node": "daniel-server"},
    }
    assert twin_mismatches(ours, moved) == [
        "claude_code_artifacts_node='daniel-box' but artifacts's twin is 'daniel-server'"
    ]
    dropped = {k: v for k, v in ours.items() if k != "claude_code_otlp_port"}
    assert len(twin_mismatches(dropped, theirs)) == 1


def _node_cron_tasks() -> list[dict]:
    return list(walk_tasks(load_tasks(NODE_CRONS)))


def test_the_moved_crons_keep_their_names_files_jobs_and_minutes():
    ctx = _context(CLAUDE_CODE)
    found = {}
    for task in _node_cron_tasks():
        cron = task.get("ansible.builtin.cron")
        if isinstance(cron, dict):
            minute = cron["minute"]
            for key, value in ctx.items():
                minute = minute.replace("{{ %s }}" % key, str(value))
            found[cron["name"]] = {
                "cron_file": cron["cron_file"],
                "job": cron["job"],
                "minute": minute,
            }
            assert cron["user"] == "{{ sys_user }}", cron
    assert found == CRONS_BEFORE_H2_7


def test_the_moved_scripts_keep_their_paths():
    found = {
        task["ansible.builtin.template"]["src"]: task["ansible.builtin.template"][
            "dest"
        ]
        for task in _node_cron_tasks()
        if isinstance(task.get("ansible.builtin.template"), dict)
    }
    assert found == SCRIPTS_BEFORE_H2_7


def test_no_other_role_installs_the_moved_cron_files():
    """Two roles writing one cron file would fight over its lines on every apply."""
    owners = {}
    for tasks_file in task_files():
        for task in walk_tasks(yaml_fast.safe_load(tasks_file.read_text()) or []):
            cron = task.get("ansible.builtin.cron")
            if isinstance(cron, dict) and cron.get("name") in CRONS_BEFORE_H2_7:
                owners.setdefault(cron["name"], []).append(
                    tasks_file.relative_to(ROLES)
                )
    assert owners == {
        name: [NODE_CRONS.relative_to(ROLES)] for name in CRONS_BEFORE_H2_7
    }


def test_both_crons_are_gated_on_the_artifacts_node():
    """daniel-server runs claude_code too, and must install neither cron (H2-R1-17)."""
    imports = [
        task
        for task in load_tasks(CLAUDE_CODE / "tasks" / "main.yml")
        if task.get("ansible.builtin.import_tasks") == NODE_CRONS.name
    ]
    assert len(imports) == 1, imports
    assert imports[0]["when"] == "inventory_hostname == claude_code_artifacts_node"


TEXTFILE = re.compile(r"write_check_textfile (\S+) ")


def node_cron_paths() -> set[str]:
    """Every path node_crons.yml leaves on the artifacts node: scripts, cron files, check
    files and the render stamp.
    """
    paths = set()
    for task in _node_cron_tasks():
        if isinstance(tpl := task.get("ansible.builtin.template"), dict):
            paths.add(tpl["dest"])
            script = rendered_shell_text("setup", "claude_code", tpl["src"])
            for check in TEXTFILE.findall(script):
                paths.add(f"/var/lib/node-exporter-textfile/{check}.prom")
        if isinstance(cron := task.get("ansible.builtin.cron"), dict):
            paths.add(f"/etc/cron.d/{cron['cron_file']}")
        if name := (task.get("vars") or {}).get("stamp_render_name"):
            paths.add(f"/var/lib/homelab/setup-render-manifest.d/{name}")
    return paths


def _absent_arm() -> dict:
    arms = [
        task
        for task in load_tasks(CLAUDE_CODE / "tasks" / "main.yml")
        if task.get("when") == "inventory_hostname != claude_code_artifacts_node"
    ]
    assert len(arms) == 1, arms
    return arms[0]


def test_every_node_cron_path_has_an_absent_arm_off_the_node_is_clean():
    """A host the artifacts node leaves keeps nothing that pages as stale (#6355)."""
    arm = _absent_arm()
    assert arm["ansible.builtin.file"] == {"path": "{{ item }}", "state": "absent"}
    paths = node_cron_paths()
    # Non-vacuity: the check file the stale-page incident named, and the cron behind it.
    assert {"/var/lib/node-exporter-textfile/sync-artifacts.prom", "/etc/cron.d/sync-artifacts"} <= paths  # fmt: skip
    assert len(paths) == 7
    assert paths - set(arm["loop"]) == set()


def test_a_node_cron_path_the_absent_arm_misses_is_flagged():
    loop = [
        p for p in _absent_arm()["loop"] if not p.endswith("claude-otel-health.prom")
    ]
    assert node_cron_paths() - set(loop) == {
        "/var/lib/node-exporter-textfile/claude-otel-health.prom"
    }
