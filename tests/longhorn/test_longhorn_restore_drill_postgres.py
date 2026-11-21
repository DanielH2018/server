"""The restore drill's Postgres check, in two halves.

The drill half runs the rendered script through the stubbed `kubectl`, with the probe pod's log
set by `K3S_STUB_PROBE`, and asserts what the run stamps and the pod it creates. The probe half
lifts the shell program the probe pod's `postgres` container runs out of the rendered script and
runs it inside the Airflow role's pinned Postgres image against a data directory `initdb` made
there, because only that image carries the `postgres` binary the program starts.

Run: uv run pytest tests/longhorn/test_longhorn_restore_drill_postgres.py
Image half: uv run pytest -m image tests/longhorn/test_longhorn_restore_drill_postgres.py
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from _helpers import load_yaml
from _restore_drill import DRILL_TEMPLATE, harness, image_leg_available
from _shell_render import rendered_shell_text
from lib.proc_testing import run as launch
from lib.repo_paths import K8S_ROLES

PVC = "airflow-postgres-data"
POSTGRES_IMAGE = load_yaml(K8S_ROLES / "airflow" / "defaults" / "main.yml")[
    "airflow_k8s_postgres_image"
]


def _probe_log(*lines: str, checked: int, bad: int) -> str:
    return "\n".join(
        [
            "files=1200 bytes=349000000",
            "sqlite_checked=0 sqlite_bad=0",
            *lines,
            f"postgres_checked={checked} postgres_bad={bad}",
        ]
    )


def test_a_postgres_directory_that_does_not_start_fails_the_drill(
    tmp_path: Path,
) -> None:
    """FLAGGED half: a data directory with no catalog answer is a drill failure."""
    # fact: k8s/roles/longhorn-restore-drill/CLAUDE.md#What the drill is and what it writes (the autonomous-role contract)
    run = harness(tmp_path, [PVC])
    log = _probe_log(
        "postgres bad pgdata: FATAL: incorrect checksum in control file",
        checked=1,
        bad=1,
    )
    proc = run(restore="pass", env={"K3S_STUB_PROBE": log})
    assert proc.returncode == 1
    assert "postgres bad pgdata: FATAL" in proc.stderr, proc.stderr
    assert "last-success" not in run.cm
    assert f"success.{PVC}" not in run.cm


def test_a_started_postgres_directory_passes_and_is_reported(tmp_path: Path) -> None:
    run = harness(tmp_path, [PVC])
    log = _probe_log("postgres ok pgdata: pg_class=415", checked=1, bad=0)
    proc = run(restore="pass", env={"K3S_STUB_PROBE": log})
    assert proc.returncode == 0, proc.stderr
    assert "postgres_ok=1" in proc.stdout, proc.stdout
    assert f"success.{PVC}" in run.cm


def test_a_probe_log_without_the_postgres_summary_fails(tmp_path: Path) -> None:
    """A container that did not finish leaves the data directories unknown, not clean."""
    run = harness(tmp_path, [PVC])
    log = "files=3 bytes=4096\nsqlite_checked=0 sqlite_bad=0"
    proc = run(restore="pass", env={"K3S_STUB_PROBE": log})
    assert proc.returncode == 1
    assert "Postgres check" in proc.stderr and "reported no result" in proc.stderr
    assert f"success.{PVC}" not in run.cm


def _postgres_program() -> str:
    match = re.search(
        r"<<'PG'\n(.*?)\nPG\n", rendered_shell_text(*DRILL_TEMPLATE), re.DOTALL
    )
    assert match, "the drill's Postgres program moved; update _postgres_program"
    return match.group(1)


def test_the_postgres_container_runs_the_airflow_image_over_the_read_only_restore(
    tmp_path: Path,
) -> None:
    """The second container carries Airflow's pin, and the drill reads both containers' logs."""
    run = harness(tmp_path, [PVC])
    proc = run(restore="pass")
    assert proc.returncode == 0, proc.stderr
    (created,) = [c for c in run.calls if " run restore-drill-" in c]
    spec = json.loads(created.split("--overrides=", 1)[1])["spec"]
    (container,) = [c for c in spec["containers"] if c["name"] == "postgres"]
    assert container["image"] == POSTGRES_IMAGE
    assert container["command"] == [
        "sh",
        "-c",
        _postgres_program(),
        "pg-check",
        "/drill",
        "/scratch",
    ]
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    assert mounts["/drill"]["readOnly"] is True
    assert mounts["/scratch"]["name"] == "scratch"
    (logs,) = [c for c in run.calls if " logs restore-drill-" in c]
    assert "--all-containers=true" in logs


@pytest.fixture
def docker_available() -> None:
    if image_leg_available():
        return
    if os.environ.get("CI"):
        pytest.fail("CI must run the image leg, and no Docker daemon answered")
    pytest.skip("no Docker daemon here; CI runs the image leg")


# Builds a data directory under /drill as initdb leaves it, applies $MUTATE as root, then runs
# the lifted program the way the probe pod does.
_IN_IMAGE = """
set -e
mkdir -p /drill/pgdata /scratch
chown postgres:postgres /drill/pgdata
gosu postgres initdb -D /drill/pgdata >/dev/null
sh -c "$MUTATE"
sh -c "$PG_CHECK" pg-check /drill /scratch
"""


def _run_in_image(mutate: str) -> subprocess.CompletedProcess:
    return launch(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            f"MUTATE={mutate}",
            "-e",
            f"PG_CHECK={_postgres_program()}",
            POSTGRES_IMAGE,
            "sh",
            "-c",
            _IN_IMAGE,
        ],
        env=dict(os.environ),
        timeout=300,
    )


@pytest.mark.image
@pytest.mark.usefixtures("docker_available")
def test_a_snapshot_with_a_stale_pid_file_starts_and_answers_the_catalog() -> None:
    """A snapshot of a running server holds `postmaster.pid`; the copy must start anyway."""
    proc = _run_in_image("printf '1\\n/drill/pgdata\\n' >/drill/pgdata/postmaster.pid")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert re.fullmatch(r"postgres ok pgdata: pg_class=\d+", lines[0]), lines
    assert lines[1:] == ["postgres_checked=1 postgres_bad=0"]


@pytest.mark.image
@pytest.mark.usefixtures("docker_available")
def test_a_truncated_pg_control_fails_the_drill(tmp_path: Path) -> None:
    """The probe reports the directory bad, and the drill fed that log stamps no success."""
    # fact: k8s/roles/longhorn-restore-drill/CLAUDE.md#What the drill is and what it writes (the autonomous-role contract)
    control = "/drill/pgdata/global/pg_control"
    probe = _run_in_image(f"head -c 16 {control} >/tmp/c && cat /tmp/c >{control}")
    assert probe.returncode == 0, probe.stderr
    lines = probe.stdout.splitlines()
    assert lines[0].startswith("postgres bad pgdata: "), lines
    assert lines[1:] == ["postgres_checked=1 postgres_bad=1"]

    run = harness(tmp_path, [PVC])
    log = "files=1000 bytes=40000000\nsqlite_checked=0 sqlite_bad=0\n" + probe.stdout
    proc = run(restore="pass", env={"K3S_STUB_PROBE": log})
    assert proc.returncode == 1
    assert "postgres bad pgdata: " in proc.stderr, proc.stderr
    assert f"success.{PVC}" not in run.cm
