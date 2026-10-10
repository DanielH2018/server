"""The `deploy` source cc-wait reads: `deploy_probe.py` and the `.claude/wait-sources/deploy` shim.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_probe.py

The probe's terminal states must satisfy cc-wait's contract (dotfiles
docs/specs/2026-10-04-cc-wait-design.md), which `test_land_probe.py` restates for the `land`
source: no reserved code (2 or 75) and at least one failure state.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from deploy_tools import deploy_detach, deploy_probe
from deploy_tools import deploy_under_locks as locked
from lib.detach_fork import recorded_code

_SHIM = Path(__file__).resolve().parents[3] / ".claude" / "wait-sources" / "deploy"
_SETTLED = "deploy --detach settled (ansible exit 0)"


def deploy(
    tmp_path: Path,
    text: str,
    rc: int | None = None,
    pid: int | None = None,
    name: str = "deploy-sonarr-20261010-120000-4242.log",
) -> Path:
    log = tmp_path / name
    log.write_text(text)
    if rc is not None:
        log.with_suffix(".rc").write_text(f"{rc}\n")
    if pid is not None:
        log.with_suffix(".pid").write_text(f"{pid}\n")
    return log


@pytest.fixture
def dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_a_settled_deploy_ends_the_wait_with_its_headline(tmp_path):
    log = deploy(tmp_path, f"PLAY RECAP\n{_SETTLED}\n  sonarr: ok\n", rc=0)
    assert deploy_probe.read(log) == {"state": "settled", "detail": _SETTLED}


def test_a_failed_gate_ends_the_wait_as_failed(tmp_path):
    headline = "deploy --detach FAILED (ansible exit 2)"
    log = deploy(tmp_path, f"{headline}\n", rc=1)
    assert deploy_probe.read(log) == {
        "state": "failed",
        "detail": f"exit code 1: {headline}",
    }


def test_a_crashed_child_reports_its_exception(tmp_path):
    log = deploy(
        tmp_path,
        'Traceback (most recent call last):\n  File "x", line 1\nOSError: disk full\n',
        rc=1,
    )
    assert (
        deploy_probe.read(log)["detail"]
        == f"exit code 1: OSError: disk full; read {log}"
    )


def test_a_running_deploy_reports_its_newest_task(tmp_path):
    log = deploy(
        tmp_path,
        "PLAY [k8s] ****\nTASK [k8s/sonarr : apply manifests] ****\nok: [daniel-box]\n",
        pid=os.getpid(),
    )
    assert deploy_probe.read(log) == {
        "state": "running",
        "detail": "k8s/sonarr : apply manifests",
    }


def test_a_deploy_whose_pid_died_without_a_code_died(tmp_path, dead_pid):
    log = deploy(tmp_path, "TASK [x] ****\n", pid=dead_pid)
    assert deploy_probe.read(log)["state"] == "died"


def test_the_newest_log_for_exactly_these_tags_is_the_deploy(tmp_path):
    deploy(tmp_path, "old\n", name="deploy-sonarr-20261010-090000-99999.log")
    newest = deploy(tmp_path, "new\n", name="deploy-sonarr-20261010-120000-100.log")
    # A sibling tag's log sorts after `sonarr-2...` as text, and must not be picked.
    deploy(tmp_path, "other\n", name="deploy-sonarr-exporter-20261010-130000-1.log")
    assert deploy_probe.find_log("sonarr", "", str(tmp_path)) == newest


def test_no_log_for_the_tags_is_refused(tmp_path):
    deploy(tmp_path, "other\n", name="deploy-sonarr-exporter-20261010-130000-1.log")
    with pytest.raises(deploy_probe.NoDeploy):
        deploy_probe.find_log("sonarr", "", str(tmp_path))


@pytest.mark.parametrize("tags", ["sonarr", "sonarr,radarr", "a b,c/d", ""])
def test_the_label_is_the_one_the_deploy_names_its_log_with(tags):
    run = locked.Run(
        repo_root=Path("/x"), tags=[t for t in tags.split(",") if t], at_sha="", args=[]
    )
    assert deploy_probe.label(tags) == run.label


def test_the_probe_reads_the_directory_the_deploy_writes():
    assert deploy_probe.LOG_DIR == deploy_detach.LOG_DIR


@pytest.mark.parametrize("verdict", [0, 1])
def test_the_child_records_the_notifier_s_code(tmp_path, verdict):
    """`child` itself writes the gate's code as `.rc`, not a constant 0 (#3934).

    Runs `child` in a forked process, because it ends in `os._exit`. The cgroup move is
    stubbed so the test process never leaves the unit it runs in.
    """
    run = locked.Run(repo_root=tmp_path, tags=["sonarr"], at_sha="", args=[])
    tools = deploy_detach.DetachTools(
        leave_unit_cgroup=lambda _prefix: None,
        deploy_and_gate=lambda *_args: verdict,
    )
    log = tmp_path / "deploy-sonarr-20261010-120000-1.log"
    pid = os.fork()
    if pid == 0:
        deploy_detach.child(run, log, "notifier.py", tools)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == verdict
    assert recorded_code(log) == verdict


def test_run_records_the_log_and_pid_before_returning(tmp_path, capsys):
    """A `cc-wait deploy` chained on the return finds this run's log, pid and command."""
    logs_at_fork = []

    def fork(_body) -> int:
        logs_at_fork.extend(tmp_path.glob("deploy-sonarr-*.log"))
        return 4242

    tools = deploy_detach.DetachTools(
        log_dir=tmp_path,
        take_tree_lock=lambda: os.open(os.devnull, os.O_RDONLY),
        reap_dead_snapshots=lambda _root: None,
        make_snapshot=lambda _state: None,
        take_service_locks=lambda _state, _tags: None,
        fork_detached=fork,
    )
    assert deploy_detach.run(tmp_path, ["sonarr"], "", [], "notifier.py", tools) == 0
    (log,) = tmp_path.glob("deploy-sonarr-*.log")
    assert logs_at_fork == [log], "the log must exist before the fork"
    assert deploy_probe.find_log("sonarr", "", str(tmp_path)) == log
    assert log.with_suffix(".pid").read_text() == "4242\n"
    assert f"  wait: cc-wait deploy sonarr --log {log}" in capsys.readouterr().out


def test_the_declared_states_keep_cc_wait_s_contract():
    codes = deploy_probe.TERMINAL.values()
    assert not {2, 75} & set(codes)
    assert any(code != 0 for code in codes)


def test_the_shim_runs_the_probe_and_prints_one_json_state(tmp_path):
    """The transport cc-wait actually uses: an executable, its argv, one JSON line out."""
    deploy(tmp_path, f"{_SETTLED}\n", rc=0)
    out = subprocess.run(
        [str(_SHIM), "sonarr", "--log-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert json.loads(out.stdout) == {"state": "settled", "detail": _SETTLED}
