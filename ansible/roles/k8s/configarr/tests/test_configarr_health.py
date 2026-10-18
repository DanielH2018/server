"""The configarr-health CronJob pushes its verdict, and its pod's layout imports the reader.

`files/configarr_health.py` runs as the configarr-health CronJob since H2.5b, reading its push
token from the configarr-health Secret. The first tests drive its `main` with a stubbed kubectl
and a stubbed push. The last one builds the pod's `/config` directory from the rendered
ConfigMap and the CronJob's `items:`, exactly as the kubelet mounts them, and runs the reader
from it, so a module the ConfigMap stops carrying fails here rather than in the next scheduled pod.

Run: uv run pytest ansible/roles/k8s/configarr/tests/test_configarr_health.py
"""

import json
import subprocess
import sys

import pytest
from _k8s_render import render_role_template
from lib import yaml_fast

import configarr_health

_JOBS = {
    "items": [
        {
            "metadata": {"name": "configarr-29000000", "labels": {"app": "configarr"}},
            "status": {"succeeded": 1, "completionTime": "2026-10-18T04:30:26Z"},
        }
    ]
}


# One hour after the Job above finished.
_NOW = 1792301426.0


@pytest.fixture
def pushes(monkeypatch) -> list[tuple]:
    """Sets the push environment; each push the stub below receives lands in the list."""
    monkeypatch.setenv(
        "KUMA_PUSH_URL",
        "http://uptime-kuma.homelab.svc.cluster.local:3001/api/push/tok",
    )
    return []


def _main(pushes: list[tuple], kubectl) -> int:
    def push(status, msg, base, token, *, log=None):
        pushes.append((status, msg, base, token))
        return True

    return configarr_health.main(kubectl=kubectl, push=push, now=lambda: _NOW)


def _replies(replies: dict[str, tuple[int, str]]):
    return lambda *argv: replies[argv[0]]


def test_a_clean_sync_is_pushed_up_to_the_url_from_the_environment(pushes, capsys):
    kubectl = _replies({"get": (0, json.dumps(_JOBS)), "logs": (0, "Done\n")})
    assert _main(pushes, kubectl) == 0
    [(status, msg, base, token)] = pushes
    assert (status, base, token) == (
        "up",
        "http://uptime-kuma.homelab.svc.cluster.local:3001",
        "tok",
    )
    assert msg == "configarr sync ok: Done (1.0h ago)"
    assert capsys.readouterr().out.startswith("configarr-health: status=up ")


def test_a_reader_that_raises_pushes_down_naming_the_exception(pushes):
    def broken(*argv):
        raise RuntimeError("no kubectl")

    assert _main(pushes, broken) == 0
    [(status, msg, _, _)] = pushes
    assert status == "down"
    assert msg == "configarr health reader failed: RuntimeError: no kubectl"


def test_with_no_token_it_prints_the_verdict_and_pushes_nothing(
    monkeypatch, pushes, capsys
):
    monkeypatch.setenv("KUMA_PUSH_URL", "")
    assert _main(pushes, _replies({"get": (1, "Forbidden")})) == 0
    assert pushes == []
    assert capsys.readouterr().out == "down\tcannot read Jobs in homelab: Forbidden\n"


def test_the_reader_imports_from_the_pods_mounted_layout(tmp_path):
    [configmap] = yaml_fast.safe_load_all(
        render_role_template("configarr", "health-configmap.yaml.j2")
    )
    [cronjob] = yaml_fast.safe_load_all(
        render_role_template("configarr", "health-cronjob.yaml.j2")
    )
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    [volume] = [v for v in pod["volumes"] if "configMap" in v]
    assert volume["configMap"]["name"] == configmap["metadata"]["name"]
    for item in volume["configMap"]["items"]:
        target = tmp_path / item["path"]
        target.parent.mkdir(exist_ok=True)
        target.write_text(configmap["data"][item["key"]])
    # `--help` returns after every module-level import has run, so exit 0 proves the import
    # closure resolves from the mounted files alone. `-S` drops the venv's site-packages,
    # which `-I` keeps: the pod's bare python image has no wheels, so a third-party import
    # must fail here too. `-P` keeps the working directory off sys.path.
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-P",
            str(tmp_path / "configarr_health.py"),
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
