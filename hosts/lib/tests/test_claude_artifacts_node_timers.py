"""The artifacts node's peer sync and OTLP-door heartbeat ship in daniel-box's package (H7.40).

They were the claude_code role's two crons until H7.40's fourth slice. The package ships each as
a `homelab-*` timer on `claude_code_artifacts_node` alone, retires the Ansible-era cron file and
script, and creates the directories the artifacts pod mounts.

The scripts read values the k8s roles also read: the node the artifacts pod pins, the trees it
mounts, and the OTLP door the collector binds. A shared key in group_vars/all.yml would route as
a deploy-plane change only, so each value is a key pair, and this module holds each pair equal,
resolved the way a deploy resolves it.

Run: uv run pytest hosts/lib/tests/test_claude_artifacts_node_timers.py
"""

import re
from pathlib import Path

import pytest
import yaml

from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from _setup_render import CLAUDE_CODE

ROLES = REPO / "ansible" / "roles"
ARTIFACTS = ROLES / "k8s" / "artifacts"
OBSERVABILITY = ROLES / "k8s" / "observability"
COMPONENT = REPO / "hosts" / "components" / "claude-code" / "component.yml"
UNITS = "/usr/lib/systemd/system"
TMPFILES = "/usr/lib/tmpfiles.d/homelab-artifacts.conf"

# claude_code key -> (k8s role, how that role spells the same value). A callable reads a value
# the k8s role composes from two keys.
TWINS = {
    "claude_code_artifacts_node": (ARTIFACTS, "artifacts_k8s_node"),
    "claude_code_artifacts_peer_sources": (ARTIFACTS, "artifacts_peer_sources"),
    "claude_code_artifacts_host_dir": (ARTIFACTS, "artifacts_host_dir"),
    "claude_code_artifacts_peer_dir": (ARTIFACTS, "artifacts_peer_dir"),
    "claude_code_artifacts_stars_file": (
        ARTIFACTS,
        lambda ctx: f"{ctx['artifacts_stars_dir']}/{ctx['artifacts_stars_name']}",
    ),
    "claude_code_otlp_host_ip": (OBSERVABILITY, "observability_otlp_host_ip"),
    "claude_code_otlp_port": (OBSERVABILITY, "observability_otlp_port"),
}
# Each timer, the script its service runs, the syslog tag Loki keeps its lines under, and the
# minute default it fires on.
TIMERS = {
    "homelab-sync-artifacts": (
        "/usr/lib/homelab/sync-artifacts",
        "sync-artifacts",
        "claude_code_artifacts_sync_minute",
    ),
    "homelab-claude-otel-health": (
        "/usr/lib/homelab/claude-otel-health",
        "claude-otel-health",
        "claude_code_telemetry_health_minute",
    ),
}
# M6 batch B6a's Cutover-2: the journal line and the textfile are the only verdict a timer sends.
# A script that sources the Kuma library, calls kuma_push or builds a push URL has its push back.
KUMA_PUSH_MARKERS = ("kuma-push-lib", "kuma_push", "/api/push/")
# What the role installed, which the component's legacy list retires.
ANSIBLE_ERA = [
    {"cron_file": "sync-artifacts"},
    {"cron_file": "claude-otel-health"},
    {"path": "/usr/local/bin/sync-artifacts.sh"},
    {"path": "/usr/local/bin/claude-otel-health.sh"},
    {"path": "/var/lib/homelab/setup-render-manifest.d/claude-code-node-crons"},
]


def kuma_push_markers(body: str) -> list[str]:
    """The Kuma push markers a shipped script still carries, or nothing."""
    return [marker for marker in KUMA_PUSH_MARKERS if marker in body]


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
    return render_context(CLAUDE_CODE), {
        r: render_context(r) for r in (ARTIFACTS, OBSERVABILITY)
    }


@pytest.fixture(scope="module")
def box() -> dict[str, tuple[bytes, int]]:
    return host_mode.render_host("daniel-box").files


def _text(files: dict, path: str) -> str:
    return files[path][0].decode()


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
        ARTIFACTS: {**theirs[ARTIFACTS], "artifacts_host_dir": "/srv/elsewhere"},
    }
    assert twin_mismatches(ours, moved) == [
        "claude_code_artifacts_host_dir='/home/ubuntu/.claude/artifacts' "
        "but artifacts's twin is '/srv/elsewhere'"
    ]
    dropped = {k: v for k, v in ours.items() if k != "claude_code_otlp_port"}
    assert len(twin_mismatches(dropped, theirs)) == 1


@pytest.mark.parametrize("name", sorted(TIMERS))
def test_each_timer_runs_its_script_as_the_sys_user_with_its_tag(box, name):
    script, tag, _ = TIMERS[name]
    unit = _text(box, f"{UNITS}/{name}.service")
    assert script in box
    assert f"ExecStart={script}\n" in unit
    assert "User=ubuntu\n" in unit
    assert f"SyslogIdentifier={tag}\n" in unit
    # Nothing is left to push to, so the unit loads no credential.
    assert "LoadCredential" not in unit


@pytest.mark.parametrize("name", sorted(TIMERS))
def test_neither_producer_pushes_to_kuma(box, name):
    # fact: hosts/components/claude-code/CLAUDE.md#The artifacts node's two timers
    script, _, _ = TIMERS[name]
    body = _text(box, script)
    # Non-vacuity: the shipped bytes are the real script, which still records its verdict.
    assert "write_check_textfile" in body
    assert kuma_push_markers(body) == []


def test_a_script_that_pushes_to_kuma_is_flagged():
    pushing = (
        "source /usr/lib/homelab/kuma-push-lib.sh\n"
        'kuma_push "$1" "$2" "https://kuma/api/push/x"\n'
    )
    assert kuma_push_markers(pushing) == list(KUMA_PUSH_MARKERS)


@pytest.mark.parametrize("name", sorted(TIMERS))
def test_each_timer_fires_on_the_period_its_script_derives_its_deadline_from(box, name):
    minute = render_context(CLAUDE_CODE)[TIMERS[name][2]]
    every_n = re.fullmatch(r"\*/(\d+)", minute)
    assert every_n, minute
    timer = _text(box, f"{UNITS}/{name}.timer")
    assert f"OnCalendar=*-*-* *:00/{every_n.group(1)}:00\n" in timer


def test_the_package_creates_every_directory_the_artifacts_pod_mounts(box):
    ctx = render_context(ARTIFACTS)
    lines = {
        tuple(line.split()[1:5])
        for line in _text(box, TMPFILES).splitlines()
        if line.startswith("d ")
    }
    expected = {
        (ctx["artifacts_host_dir"], "0750", "ubuntu", "ubuntu"),
        (ctx["artifacts_stars_dir"], "0755", "ubuntu", "ubuntu"),
        ("/var/lib/node-exporter-textfile", "2775", "root", "ubuntu"),
    } | {
        (f"{ctx['artifacts_peer_dir']}/{peer['name']}", "0750", "ubuntu", "ubuntu")
        for peer in ctx["artifacts_peer_sources"]
    }
    assert expected <= lines, lines
    sync = _text(box, f"{UNITS}/homelab-sync-artifacts.service")
    assert f"ExecStartPre=+/usr/bin/systemd-tmpfiles --create {TMPFILES}\n" in sync


def test_only_the_artifacts_node_ships_them():
    server = host_mode.render_host("daniel-server").files
    shipped = [
        path for path in server if "sync-artifacts" in path or "otel-health" in path
    ]
    assert shipped == []
    assert TMPFILES not in server


def test_the_ansible_era_crons_and_scripts_are_retired():
    legacy = yaml.safe_load(COMPONENT.read_text())["legacy"]
    missing = [entry for entry in ANSIBLE_ERA if entry not in legacy]
    assert missing == []
