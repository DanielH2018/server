"""The `.skip` marker appears only after k3s has created CoreDNS (H1.4).

`tasks/coredns.yml` keeps two tasks: a bounded read of Deployment `coredns`, then the task that
writes the marker. On a new cluster Flux resolves GHCR through cluster DNS, so a marker written
before k3s first applies its bundled CoreDNS would leave no CoreDNS at all. Each case runs the
real task file in a real `ansible-playbook` against a stub `k3s`, with the marker's path
(`k3s_coredns_skip_marker`) pointed at a temporary directory. The plan is
docs/target-architecture/host/h1-cluster-config-to-flux.md, *Verification*, *CoreDNS*.

The file module's `owner: root` and `group: root` are stripped from the copy that runs, because
the stand-in for `become` runs as the current user and a chown to root would fail for a reason
that says nothing about the order.

Run: uv run pytest ansible/tests/setup/test_k3s_coredns_marker_follows_the_read.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from lib.git_testing import scrubbed_env
from lib.proc_testing import fake_bin, path_with

from _helpers import leaf_tasks, load_tasks
from lib import yaml_fast
from lib.repo_paths import K3S_DEFAULTS, K3S_ROLE

_TASKS = K3S_ROLE / "tasks" / "coredns.yml"

# `get deployment coredns` succeeds only when the stub is told the Deployment exists.
_K3S = """#!/bin/sh
here=$(dirname "$0")
echo "$@" >> "$here/calls"
if [ -f "$here/deployment" ]; then
  echo "coredns   1/1   1   1   1m"
  exit 0
fi
echo 'Error from server (NotFound): deployments.apps "coredns" not found' >&2
exit 1
"""

# A `sudo` stand-in: it runs the command `become` hands it, as this user.
_BECOME = (
    "#!/bin/sh\n"
    'last=""\n'
    'prev=""\n'
    'for a in "$@"; do prev="$last"; last="$a"; done\n'
    'if [ "$prev" = "-c" ]; then exec /bin/sh -c "$last"; fi\n'
    'exec "$@"\n'
)

pytestmark = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)


def _run(
    tmp_path: Path, *, deployment: bool, retries: int = 0
) -> tuple[int, Path, str]:
    """Run coredns.yml once. Returns the exit code, the marker's path and the output."""
    bin_dir = fake_bin(tmp_path / "bin", k3s=_K3S, fake_become=_BECOME)
    if deployment:
        (bin_dir / "deployment").write_text("")
    marker = tmp_path / "manifests" / "coredns.yaml.skip"
    marker.parent.mkdir()
    tasks = load_tasks(_TASKS)
    for task in leaf_tasks(tasks):
        for module in ("ansible.builtin.file",):
            if module in task:
                task[module].pop("owner", None)
                task[module].pop("group", None)
    (tmp_path / "coredns.yml").write_text(yaml.safe_dump(tasks, sort_keys=False))
    (tmp_path / "play.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "vars": {"ansible_become_exe": str(bin_dir / "fake_become")},
                    "tasks": [{"ansible.builtin.import_tasks": "coredns.yml"}],
                }
            ]
        )
    )
    defaults = yaml_fast.safe_load(K3S_DEFAULTS.read_text())
    extra = {
        "k3s_coredns_read_retries": retries,
        "k3s_coredns_skip_marker": str(marker),
        "k3s_coredns_read_delay": 0,
    }
    assert "k3s_coredns_skip_marker" in defaults, "the marker path is a defaults key"
    env = scrubbed_env()
    env["PATH"] = path_with(bin_dir, env=env)
    env["ANSIBLE_PYTHON_INTERPRETER"] = sys.executable
    env["ANSIBLE_CONFIG"] = str(tmp_path / "none.cfg")
    env["ANSIBLE_NOCOLOR"] = "1"
    result = subprocess.run(
        ["ansible-playbook", "play.yml", "-i", "localhost,", "-e", json.dumps(extra)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result.returncode, marker, result.stdout + result.stderr


def test_a_cluster_with_no_coredns_fails_before_the_marker_exists(tmp_path):
    code, marker, output = _run(tmp_path, deployment=False)
    assert code != 0, output
    assert not marker.exists(), "the marker was written before k3s created CoreDNS"


def test_a_cluster_with_coredns_gets_the_marker(tmp_path):
    code, marker, output = _run(tmp_path, deployment=True)
    assert code == 0, output
    assert marker.exists()


def test_the_read_precedes_the_marker_and_survives_check_mode():
    tasks = list(leaf_tasks(load_tasks(_TASKS)))
    names = [t.get("name") for t in tasks]
    assert names == [
        "Wait for k3s to create its CoreDNS Deployment",
        "Stop k3s reconciling its bundled CoreDNS addon",
    ]
    read = tasks[0]
    assert read["check_mode"] is False
    assert "retries" in read and "until" in read
