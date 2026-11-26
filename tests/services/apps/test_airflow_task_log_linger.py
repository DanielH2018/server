"""The task pod's command runs the task through the image's entrypoint, then lingers.

The command is ConfigMap text, so these tests run its shell script with a stub `/entrypoint`
and a stub `sleep` rather than the image.
"""

import subprocess

from _k8s_render import rendered_docs

from lib import yaml_fast
from lib.proc_testing import fake_bin, run, write_exec

ALLOY_FILE_MATCH_SYNC_SECONDS = 10
TASK_ARGS = [
    "python",
    "-m",
    "airflow.sdk.execution_time.execute_workload",
    "--json-string",
    "{}",
]


def _base_command() -> list[str]:
    template = next(
        doc
        for role, _, doc in rendered_docs()
        if role == "airflow"
        and doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "airflow-pod-template"
    )
    pod = yaml_fast.safe_load(template["data"]["pod-template.yaml"])
    (base,) = pod["spec"]["containers"]
    return base["command"]


def _run_task(tmp_path, task_status: int) -> subprocess.CompletedProcess[str]:
    init, separator, shell, flag, script, name = _base_command()
    assert [init, separator, shell, flag] == [
        "/usr/bin/dumb-init",
        "--",
        "/bin/bash",
        "-c",
    ]
    assert "/entrypoint " in script
    entrypoint = write_exec(
        tmp_path / "entrypoint",
        f'printf "%s\\n" "$@" > {tmp_path}/args\nexit {task_status}\n',
    )
    stubs = fake_bin(tmp_path / "bin", sleep=f'echo "$1" > {tmp_path}/slept\n')
    script = script.replace("/entrypoint ", f"{entrypoint} ")
    return run([shell, flag, script, name, *TASK_ARGS], stub_bin=stubs)


def test_the_executors_args_reach_the_entrypoint_and_its_status_is_the_pods(tmp_path):
    # fact: k8s/roles/airflow/CLAUDE.md#Operating it
    result = _run_task(tmp_path, task_status=3)

    assert (tmp_path / "args").read_text().splitlines() == TASK_ARGS
    assert result.returncode == 3


def test_the_task_pod_outlives_alloys_file_sync_after_the_task_exits(tmp_path):
    # fact: k8s/roles/airflow/CLAUDE.md#Operating it
    _run_task(tmp_path, task_status=0)

    slept = int((tmp_path / "slept").read_text())
    assert slept > ALLOY_FILE_MATCH_SYNC_SECONDS
