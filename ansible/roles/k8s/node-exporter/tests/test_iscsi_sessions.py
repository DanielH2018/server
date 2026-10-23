"""iscsi-sessions.sh reports a stale iSCSI session, and the DaemonSet wires its file in (#5826).

The script runs as the node-exporter DaemonSet's `iscsi-sessions` sidecar and writes a textfile
check file the exporter reads from a second `--collector.textfile.directory`. These tests run
it once against a fixture sysfs tree under `sh`, the POSIX shell the busybox image also
provides, and read the rendered DaemonSet for the wiring.

Run: uv run pytest ansible/roles/k8s/node-exporter/tests/test_iscsi_sessions.py
"""

import re
from pathlib import Path

import pytest
from _helpers import K8S_ROLES
from _k8s_render import rendered_k8s_text
from lib import yaml_fast
from lib.proc_testing import run

SCRIPT = K8S_ROLES / "node-exporter" / "files" / "iscsi-sessions.sh"
RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"


def _session(sysfs: Path, n: int, state: str, connection: str | None) -> None:
    d = sysfs / "class" / "iscsi_session" / f"session{n}"
    d.mkdir(parents=True)
    (d / "state").write_text(f"{state}\n")
    (d / "targetname").write_text(f"iqn.2019-10.io.longhorn:pvc-{n}\n")
    if connection is not None:
        c = sysfs / "class" / "iscsi_connection" / f"connection{n}:0"
        c.mkdir(parents=True)
        (c / "state").write_text(f"{connection}\n")


def _write_once(sysfs: Path, out: Path) -> dict[str, str]:
    """Run the script once and return its file as {series: value}."""
    env = {
        "SYSFS": str(sysfs),
        "TEXTFILE_DIR": str(out),
        "ONCE": "1",
        "PATH": "/usr/bin:/bin",
    }
    result = run(["sh", str(SCRIPT)], env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert [p.name for p in out.iterdir()] == ["iscsi-sessions.prom"]
    text = (out / "iscsi-sessions.prom").read_text()
    return dict(
        line.rsplit(" ", 1) for line in text.splitlines() if not line.startswith("#")
    )


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    sysfs, out = tmp_path / "sys", tmp_path / "out"
    (sysfs / "class" / "iscsi_session").mkdir(parents=True)
    out.mkdir()
    return sysfs, out


def test_a_free_session_turns_the_check_red_and_names_its_target(dirs):
    sysfs, out = dirs
    _session(sysfs, 2, "FREE", "failed")
    _session(sysfs, 3, "LOGGED_IN", "up")
    series = _write_once(sysfs, out)
    assert (
        series[
            'homelab_iscsi_session_info{session="session2",state="FREE",'
            'target="iqn.2019-10.io.longhorn:pvc-2",connection_state="failed"}'
        ]
        == "1"
    )
    assert (
        series[
            'homelab_iscsi_session_info{session="session3",state="LOGGED_IN",'
            'target="iqn.2019-10.io.longhorn:pvc-3",connection_state="up"}'
        ]
        == "1"
    )
    assert series["homelab_iscsi_sessions_not_logged_in"] == "1"
    assert series['homelab_check_success{check="iscsi-sessions"}'] == "0"
    assert (out / "iscsi-sessions.prom").stat().st_mode & 0o777 == 0o644


def test_logged_in_sessions_and_a_missing_connection_keep_the_check_green(dirs):
    sysfs, out = dirs
    _session(sysfs, 4, "LOGGED_IN", None)
    series = _write_once(sysfs, out)
    assert (
        series[
            'homelab_iscsi_session_info{session="session4",state="LOGGED_IN",'
            'target="iqn.2019-10.io.longhorn:pvc-4",connection_state="unknown"}'
        ]
        == "1"
    )
    assert series['homelab_check_success{check="iscsi-sessions"}'] == "1"


def test_both_not_reporting_rules_carry_the_scripts_deadline(dirs):
    # fact: ansible/roles/k8s/node-exporter/CLAUDE.md#The textfile collector directory
    sysfs, out = dirs
    deadline = _write_once(sysfs, out)[
        'homelab_check_deadline_seconds{check="iscsi-sessions"}'
    ]
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    exprs = [
        r["expr"]
        for r in group["rules"]
        if r.get("alert") == "IscsiSessionsNotReporting"
    ]
    assert len(exprs) == 2
    for expr in exprs:
        assert re.findall(r"> (\d+)", expr) == [deadline]
        assert re.findall(r"\[(\d+)s\]", expr) == [deadline]


def test_the_daemonset_runs_the_script_and_the_exporter_reads_its_directory():
    [ds] = [
        d
        for d in yaml_fast.safe_load_all(
            rendered_k8s_text("node-exporter", "daemonset.yaml.j2")
        )
        if d
    ]
    containers = {c["name"]: c for c in ds["spec"]["template"]["spec"]["containers"]}
    sidecar, exporter = containers["iscsi-sessions"], containers["node-exporter"]
    assert sidecar["command"][-1].strip() == SCRIPT.read_text().strip()
    [written] = [
        m for m in sidecar["volumeMounts"] if m["mountPath"] == "/iscsi-textfile"
    ]
    [read] = [m for m in exporter["volumeMounts"] if m["name"] == written["name"]]
    assert f"--collector.textfile.directory={read['mountPath']}" in exporter["args"]


# node_exporter drops a family whose HELP text differs from the one an earlier textfile gave
# it, and sets node_textfile_scrape_error. Every check-file producer shares the
# homelab_check_* families, so each must write the same HELP string for each of them.
# The text ends at the quote, the `\n` escape or the line end that closes it, never at an
# apostrophe, which disk-health.sh's text carries.
HELP_LINE = re.compile(r'# HELP (homelab_check_\w+) ([^"\\\n]+)')
PRODUCERS = frozenset(
    {
        "ansible/roles/k8s/node-exporter/files/iscsi-sessions.sh",
        "ansible/roles/setup/initial_setup/files/homelab-check-lib.sh",
        "ansible/roles/setup/k3s/files/live_drift_check.py",
        "ansible/roles/setup/k3s/templates/disk-health.sh.j2",
    }
)


def help_conflicts(texts: dict[str, str]) -> list[str]:
    """Each homelab_check_* family that two producers describe with different HELP text."""
    seen: dict[str, dict[str, str]] = {}
    for path, text in sorted(texts.items()):
        for family, help_text in HELP_LINE.findall(text):
            seen.setdefault(family, {}).setdefault(help_text, path)
    return sorted(
        f"{family}: {sorted(variants.values())}"
        for family, variants in seen.items()
        if len(variants) > 1
    )


def _producer_texts() -> dict[str, str]:
    paths = run(
        ["git", "grep", "-l", "# HELP homelab_check_", "--", ":!*tests/*", ":!*.md"]
    )
    root = SCRIPT.parents[5]
    return {p: (root / p).read_text() for p in paths.stdout.split()}


def test_every_check_file_producer_writes_the_same_help_text():
    # fact: ansible/roles/k8s/node-exporter/CLAUDE.md#The textfile collector directory
    texts = _producer_texts()
    assert PRODUCERS <= texts.keys()
    assert help_conflicts(texts) == []


def test_a_producer_with_a_different_help_text_is_flagged():
    texts = _producer_texts()
    path = "ansible/roles/k8s/node-exporter/files/iscsi-sessions.sh"
    texts[path] = texts[path].replace("last verdict was up", "verdict is up")
    # The first producer, in path order, to carry each text names its variant: the iSCSI
    # script's edited one and the shared library's, which disk-health.sh's text matches.
    lib = "ansible/roles/setup/initial_setup/files/homelab-check-lib.sh"
    assert help_conflicts(texts) == [f"homelab_check_success: {sorted([path, lib])}"]
