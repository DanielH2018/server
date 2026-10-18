"""The crowdsec-jobs sidecar's modules and cscli, run inside the pinned images with `docker run`.

The other tests run the modules under the repo's interpreter. These run them where the pod
does: in `crowdsec_k8s_jobs_image`, with the rendered ConfigMap mounted read-only at `/jobs`,
the root filesystem read-only, and every capability dropped, as `templates/deployment.yaml.j2`
runs the sidecar. The AppSec verify runs end to end against a stub `cscli`. The real `cscli`,
copied out of `crowdsec_k8s_image` as the `jobs-cscli` initContainer copies it, must run in the
python image too, which is the linkage the operator's ruling on #5655 asked to check.

Neither leg reaches a LAPI. What the sidecar's `cscli` reads from the live engine's
`/etc/crowdsec` and database is checked by the post-landing `--run` in the role's CLAUDE.md.

The legs skip when `docker` is missing and `CI` is unset, and fail when `CI` is set, so CI
always runs them. Both are in `_LIVE_API_TESTS` in ansible/tests/leakguard.py, which hands
them the real PATH. Every other test gets leakguard's `docker` shim, which exits 127.

Run: uv run pytest ansible/roles/k8s/crowdsec/tests/test_jobs_image.py
"""

import os
import shutil
import subprocess

import pytest
from _helpers import ANSIBLE
from lib.proc_testing import write_exec
from lib.repo_paths import ALL_VARS
from _k8s_render import render_role_template
from lib import yaml_fast

_DOCKER_TIMEOUT_S = 300
_STUB_CSCLI = """#!/bin/sh
case "$1" in
  lapi) exit 0 ;;
  appsec-configs) echo '{"appsec-configs": [{"name": "c", "status": "enabled"}]}' ;;
  appsec-rules) echo '{"appsec-rules": [{"name": "r", "status": "enabled"}, {"name": "s", "status": "enabled"}]}' ;;
  *) exit 2 ;;
esac
"""


@pytest.fixture(scope="module")
def docker() -> str:
    found = shutil.which("docker")
    if found is None:
        if os.environ.get("CI"):
            pytest.fail("docker is missing under CI, so the image legs cannot run")
        pytest.skip("docker is missing and CI is unset")
    return found


def _image(key: str, path) -> str:
    return yaml_fast.safe_load(path.read_text())[key]


JOBS_IMAGE = _image(
    "crowdsec_k8s_jobs_image", ANSIBLE / "roles/k8s/crowdsec/defaults/main.yml"
)
CROWDSEC_IMAGE = _image("crowdsec_k8s_image", ALL_VARS)


def _run_in_jobs_image(docker, jobs, tools, *command) -> subprocess.CompletedProcess:
    """`command` in the sidecar's image, mounted and confined as the pod runs it."""
    return subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network=none",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-v",
            f"{jobs}:/jobs:ro",
            "-v",
            f"{tools}:/tools:ro",
            JOBS_IMAGE,
            *command,
        ],
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_S,
    )


@pytest.fixture
def jobs(tmp_path):
    """The `/jobs` directory as the kubelet writes it from the rendered ConfigMap."""
    [configmap] = yaml_fast.safe_load_all(
        render_role_template("crowdsec", "jobs-configmap.yaml.j2")
    )
    root = tmp_path / "jobs"
    root.mkdir()
    for key, text in configmap["data"].items():
        (root / key).write_text(text)
    return root


def test_the_appsec_verify_runs_in_the_jobs_image_against_a_stub_cscli(
    docker, jobs, tmp_path
):
    tools = tmp_path / "tools"
    tools.mkdir()
    write_exec(tools / "cscli", _STUB_CSCLI)
    proc = _run_in_jobs_image(docker, jobs, tools, "python3", "/jobs/appsec_verify.py")
    assert (proc.returncode, proc.stdout) == (
        0,
        "1 appsec-configs enabled, 2 enabled rules loaded\n",
    ), proc.stderr
    for script in ("crowdsec_jobs.py", "remote_allowlist.py", "bouncer_prune.py"):
        proc = _run_in_jobs_image(
            docker, jobs, tools, "python3", f"/jobs/{script}", "--help"
        )
        assert proc.returncode == 0, (script, proc.stderr)


def test_the_engines_cscli_runs_in_the_jobs_image(docker, jobs, tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    created = subprocess.run(
        [docker, "create", CROWDSEC_IMAGE],
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_S,
        check=True,
    ).stdout.strip()
    try:
        subprocess.run(
            [docker, "cp", f"{created}:/usr/local/bin/cscli", str(tools / "cscli")],
            capture_output=True,
            timeout=_DOCKER_TIMEOUT_S,
            check=True,
        )
    finally:
        subprocess.run([docker, "rm", created], capture_output=True, timeout=60)
    proc = _run_in_jobs_image(docker, jobs, tools, "/tools/cscli", "version")
    assert proc.returncode == 0, proc.stderr
    # cscli prints `version: v1.8.1-909b5157` at the pinned digest: lower case, then its tag.
    assert "version: v" in (proc.stdout + proc.stderr).lower()
