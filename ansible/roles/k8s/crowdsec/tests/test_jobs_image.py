"""The crowdsec-jobs sidecar's modules and cscli, run inside the pinned images with `docker run`.

The other tests run the modules under the repo's interpreter. These run them where the pod
does: in `crowdsec_k8s_jobs_image`, with the rendered ConfigMap mounted read-only at `/jobs`,
the root filesystem read-only, and every capability dropped, as `templates/deployment.yaml.j2`
runs the sidecar. The AppSec verify and the home allowlist run end to end against stub `cscli`
and `kubectl` binaries, and the home-ips discovery answers `--help` under the same confinement
its CronJob gives it. The real `cscli`,
copied out of `crowdsec_k8s_image` as the `jobs-cscli` initContainer copies it, must run in the
python image too, which is the linkage the operator's ruling on #5655 asked to check.

The `lapi-register` initContainer's script, taken from the rendered Deployment, runs in
`crowdsec_k8s_image` twice with no network, so no LAPI can answer it. Its database directory
holds the entrypoint's links into the image's read-only `/staging` and no database yet. That is D5's check that `cscli machines add` and `cscli allowlists create` work with
no LAPI running (#5741).

No leg reaches a LAPI. What the sidecar's `cscli` reads from the live engine's
`/etc/crowdsec` and database is checked by the post-landing `--run` in the role's CLAUDE.md.

The legs skip when `docker` is missing and `CI` is unset, and fail when `CI` is set, so CI
always runs them. Each is in `_LIVE_API_TESTS` in ansible/tests/leakguard.py, which hands
them the real PATH. Every other test gets leakguard's `docker` shim, which exits 127.

Run: uv run pytest ansible/roles/k8s/crowdsec/tests/test_jobs_image.py
"""

import os
import shutil
import sqlite3
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


# The ConfigMap the stub kubectl returns. The leg cannot set the container's clock, so it
# passes a stale-after far past the age of this fixed stamp.
_STUB_KUBECTL = """#!/bin/sh
echo '{"data": {"looked_up_at": "2026-10-18T13:05:00Z", "ipv4": "98.42.7.9", "ipv6_prefix": "2601:600:a:b::/64"}}'
"""
_STUB_ALLOWLIST_CSCLI = """#!/bin/sh
case "$2" in
  inspect) echo '{"items": [{"value": "98.42.7.9"}, {"value": "2601:600:a:b::/64"}]}' ;;
  *) exit 2 ;;
esac
"""


def _run_in_jobs_image(
    docker, jobs, tools, *command, env=()
) -> subprocess.CompletedProcess:
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
            *(arg for pair in env for arg in ("-e", pair)),
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
    for script in (
        "crowdsec_jobs.py",
        "remote_allowlist.py",
        "bouncer_prune.py",
        "home_allowlist.py",
        "home_ips_discover.py",
    ):
        proc = _run_in_jobs_image(
            docker, jobs, tools, "python3", f"/jobs/{script}", "--help"
        )
        assert proc.returncode == 0, (script, proc.stderr)


def test_the_home_allowlist_runs_in_the_jobs_image_against_stub_tools(
    docker, jobs, tmp_path
):
    tools = tmp_path / "tools"
    tools.mkdir()
    write_exec(tools / "kubectl", _STUB_KUBECTL)
    write_exec(tools / "cscli", _STUB_ALLOWLIST_CSCLI)
    proc = _run_in_jobs_image(
        docker,
        jobs,
        tools,
        "python3",
        "/jobs/home_allowlist.py",
        env=(
            "HOME_IPS_CONFIGMAP=crowdsec-home-ips",
            "HOME_IPS_STALE_AFTER_S=999999999",
        ),
    )
    assert (proc.returncode, proc.stdout) == (0, "home IPs unchanged\n"), proc.stderr


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


def _lapi_register_command() -> list[str]:
    [deployment] = yaml_fast.safe_load_all(
        render_role_template("crowdsec", "deployment.yaml.j2")
    )
    [init] = [
        c
        for c in deployment["spec"]["template"]["spec"]["initContainers"]
        if c["name"] == "lapi-register"
    ]
    return init["command"]


def _lapi_machines() -> set[str]:
    group_vars = yaml_fast.safe_load(ALL_VARS.read_text())
    defaults = yaml_fast.safe_load(
        (ANSIBLE / "roles/k8s/crowdsec/defaults/main.yml").read_text()
    )
    return {
        *group_vars["crowdsec_k8s_sidecar_agents"].values(),
        *defaults["crowdsec_k8s_node_agent_machines"],
        "crowdsec-verify",
    }


# The image entrypoint's own loop over the staged data files (docker_start.sh).
_ENTRYPOINT_LINKS = """
for target in /staging/var/lib/crowdsec/data/*; do
  case "${target##*/}" in crowdsec.db*) continue ;; esac
  ln -s "$target" "/var/lib/crowdsec/data/${target##*/}"
done
"""


def _run_in_engine_image(docker, data, etc, *command) -> subprocess.CompletedProcess:
    """`command` in the engine's image, mounted and confined as the initContainer runs it."""
    entrypoint, *args = command
    return subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network=none",
            "--tmpfs=/tmp",
            "-e",
            "AGENT_PASSWORD=a-test-password-0",
            "-v",
            f"{data}:/var/lib/crowdsec/data",
            "-v",
            f"{etc}:/etc/crowdsec:ro",
            f"--entrypoint={entrypoint}",
            CROWDSEC_IMAGE,
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_S,
    )


def test_the_lapi_register_init_runs_against_a_fresh_database_with_no_lapi(
    docker, tmp_path
):
    # fact: ansible/roles/k8s/crowdsec/CLAUDE.md#The `lapi-register` initContainer holds the LAPI down if it fails
    # The pod's /etc/crowdsec holds no config.yaml when initContainers run.
    etc = tmp_path / "etc"
    etc.mkdir()
    # Root with every capability dropped writes only where the mode lets `other` write.
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o777)
    # The live claim's shape: the entrypoint has linked every staged data entry but the database
    # into it, so cscli reaches `trace` through a link into the read-only root filesystem.
    linked = _run_in_engine_image(docker, data, etc, "/bin/sh", "-c", _ENTRYPOINT_LINKS)
    assert linked.returncode == 0, linked.stderr
    command = _lapi_register_command()
    first = _run_in_engine_image(docker, data, etc, *command)
    print(first.stdout, first.stderr)
    assert first.returncode == 0, first.stderr
    again = _run_in_engine_image(docker, data, etc, *command)
    print(again.stdout, again.stderr)
    assert (again.returncode, again.stdout) == (0, ""), again.stderr
    listed = _run_in_engine_image(
        docker,
        data,
        etc,
        *("cscli", "-c", "/staging/etc/crowdsec/config.yaml"),
        *("machines", "list", "-o", "raw"),
    )
    assert listed.returncode == 0, listed.stderr
    machines = {line.split(",")[0] for line in listed.stdout.splitlines()[1:]}
    assert machines == _lapi_machines()
    # cscli lists allowlists only through the LAPI, so the database is read here instead. Its
    # file is root's at 0640, which a non-root runner cannot open until it is opened up.
    subprocess.run(
        [docker, "run", "--rm", "-v", f"{data}:/data", "--entrypoint=chmod"]
        + [CROWDSEC_IMAGE, "-R", "a+rX", "/data"],
        capture_output=True,
        timeout=_DOCKER_TIMEOUT_S,
        check=True,
    )
    with sqlite3.connect(data / "crowdsec.db") as db:
        allowlists = {name for (name,) in db.execute("select name from allow_lists")}
    assert allowlists == {"home-ips", "remote-ips"}
