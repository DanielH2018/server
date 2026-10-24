"""A `deploy.sh` playbook that ran and failed writes a `result=fail` deploy annotation (#6164).

The success marker stays behind `status == 0` (`ansible/tests/deploy/test_deploy_annotations.py`),
and a failure gets its own call, `playbook.annotate_failure`, so the dashboards show it too. A
run that matched no host deployed nothing and writes neither.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_annotates_failures.py
"""

import subprocess
from pathlib import Path

import pytest

from _deploy_sh_fakes import (
    FAKE_RECAP,
    FLOCK_STUB,
    UV_WRAPPER_ARMS,
    deploy_sh_env,
    make_snapshot_repo,
    stub_bin,
)
from deploy_tools.deploy_lib import detach_run as deploy_detach
from deploy_tools.deploy_lib import under_locks as deploy_under_locks
from lib.exit_codes import DEPLOY_NO_HOSTS
from lib.repo_paths import REPO

UV_STUB = f"""#!/bin/bash
case "$*" in
  *ansible-playbook*) {FAKE_RECAP}; exit $PLAYBOOK_EXIT ;;
{UV_WRAPPER_ARMS}
  *) exit 0 ;;
esac
"""
LOGGER_STUB = '#!/bin/bash\nprintf "%s\\n" "$*" >> "$LOGGER_LOG"\n'


@pytest.mark.parametrize(
    ("status", "marks"), [(0, ["ok"]), (20, ["fail"]), (DEPLOY_NO_HOSTS, [])]
)
def test_a_detached_deploy_annotates_its_result(tmp_path, status, marks):
    """`--detach` runs its playbook in `deploy_detach.deploy_and_gate`, not in `run`."""
    seen = []
    steps = deploy_detach.ChildSteps(
        run_playbook=lambda _run: status,
        annotate=lambda _run: seen.append("ok"),
        annotate_failure=lambda _run, _duration: seen.append("fail"),
        discharge=lambda _root: None,
        notify=lambda *_a: status,
    )
    run = deploy_under_locks.Run(
        repo_root=tmp_path, tags=["sonarr"], at_sha="", args=[]
    )
    deploy_detach.deploy_and_gate(run, tmp_path / "log", "notifier.py", steps)
    assert seen == marks


def _annotations(tmp_path: Path, playbook_exit: int) -> list[str]:
    """The `deploy-annotation` lines the real `deploy.sh` hands `logger`."""
    repo = make_snapshot_repo(tmp_path / "repo")
    bin_dir = stub_bin(
        tmp_path, {"flock": FLOCK_STUB, "uv": UV_STUB, "logger": LOGGER_STUB}
    )
    log = tmp_path / "logger.log"
    env = deploy_sh_env(
        tmp_path, bin_dir, PLAYBOOK_EXIT=str(playbook_exit), LOGGER_LOG=str(log)
    )
    result = subprocess.run(
        [
            str(REPO / "scripts" / "deploy.sh"),
            "--tags",
            "sonarr",
            "--skip-staleness-check",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == (0 if playbook_exit == 0 else 20), result.stderr
    lines = log.read_text().splitlines() if log.exists() else []
    return [line for line in lines if line.startswith("-t deploy-annotation ")]


def test_a_failed_deploy_sh_annotates_a_failure_with_its_duration(tmp_path):
    [line] = _annotations(tmp_path, 2)
    assert " services=sonarr " in line
    assert " result=fail duration_s=" in line


def test_a_successful_deploy_sh_annotates_the_unchanged_success_line(tmp_path):
    [line] = _annotations(tmp_path, 0)
    assert line.endswith(" result=ok") and "duration_s" not in line
