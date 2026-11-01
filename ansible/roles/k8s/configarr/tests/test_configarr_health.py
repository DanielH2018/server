"""The configarr-health CronJob records its verdict as a log line, and its pod's layout imports the reader.

`files/configarr_health.py` runs as the configarr-health CronJob since H2.5b. M6 batch B6b's Cutover-2 removed its Kuma
push, so the first tests drive its `main` with a stubbed kubectl and read what it logs and
patches. The last two build the pod's `/config` directory from the rendered ConfigMap, exactly
as the kubelet mounts it, and run the reader from it, so a module the ConfigMap stops carrying
fails here rather than in the next scheduled pod.

Run: uv run pytest ansible/roles/k8s/configarr/tests/test_configarr_health.py
"""

import json
import importlib.util
import inspect
import re
import socket
import subprocess
import sys
from pathlib import Path

from _k8s_render import render_role_template
from lib import yaml_fast

import configarr_health

ROLE = Path(__file__).resolve().parents[1]

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


# monitor-bridge's own parser, loaded by path: the reader's line must parse there, and a copy of
# its regex here would go on passing after the two drifted apart. It imports only `re`.
_PARSER_SPEC = importlib.util.spec_from_file_location(
    "_monitor_bridge_verdict_logs",
    Path(__file__).resolve().parents[2]
    / "monitor-bridge"
    / "files"
    / "verdicts"
    / "logs.py",
)
assert _PARSER_SPEC and _PARSER_SPEC.loader
_PARSER = importlib.util.module_from_spec(_PARSER_SPEC)
_PARSER_SPEC.loader.exec_module(_PARSER)

# monitor-bridge's Swallowed Push Verdicts check parses `<ts> <host> <tag>: ` and nothing
# shorter, so a line without the two leading tokens is read by its LogQL and never counted.
_SYSLOG_UP = r"\S+ \S+ configarr-health: status=up "


def _main(kubectl, **kwargs) -> int:
    return configarr_health.main(kubectl=kubectl, now=lambda: _NOW, **kwargs)


def _replies(replies: dict[str, tuple[int, str]]):
    return lambda *argv: replies[argv[0]]


def test_a_clean_run_logs_status_up_in_the_syslog_shape(capsys):
    kubectl = _replies({"get": (0, json.dumps(_JOBS)), "logs": (0, "Done\n")})
    assert _main(kubectl) == 0
    out = capsys.readouterr().out
    assert re.match(_SYSLOG_UP, out), "not in the syslog shape"
    assert "configarr sync ok: Done (1.0h ago)" in out
    # monitor-bridge reads the line as a landed run, never as a failed push.
    assert _PARSER.parse_push_line(out.strip())[:4] == (
        "configarr-health",
        socket.gethostname(),
        "run",
        "up",
    )


def test_an_unreadable_job_list_logs_status_down(capsys):
    assert _main(_replies({"get": (1, "Forbidden")})) == 0
    assert (
        "status=down cannot read Jobs in homelab: Forbidden" in capsys.readouterr().out
    )


def test_a_reader_that_raises_logs_down_naming_the_exception(capsys):
    def broken(*argv):
        raise RuntimeError("no kubectl")

    assert _main(broken) == 0
    assert (
        "status=down configarr health reader failed: RuntimeError: no kubectl"
        in capsys.readouterr().out
    )


def test_the_verdict_is_patched_onto_the_verdict_configmap():
    calls = []

    def kubectl(*argv):
        calls.append(argv)
        return (
            (0, "")
            if argv[0] == "patch"
            else {"get": (0, json.dumps(_JOBS)), "logs": (0, "Done\n")}[argv[0]]
        )

    assert _main(kubectl, verdict_configmap="configarr-health-verdict") == 0
    [patch] = [c for c in calls if c[0] == "patch"]
    assert patch[:3] == ("patch", "configmap", "configarr-health-verdict")
    annotations = json.loads(patch[-1].removeprefix("--patch="))["metadata"][
        "annotations"
    ]
    assert annotations["homelab/check-verdict"] == "up"
    assert annotations["homelab/check-last-run"].endswith("Z")


def test_the_verdict_patch_names_a_field_manager_flux_does_not_strip():
    calls = []

    def kubectl(*argv):
        calls.append(argv)
        return (
            (0, "")
            if argv[0] == "patch"
            else {"get": (0, json.dumps(_JOBS)), "logs": (0, "Done\n")}[argv[0]]
        )

    _main(kubectl, verdict_configmap="configarr-health-verdict")
    [patch] = [c for c in calls if c[0] == "patch"]
    managers = [
        a.removeprefix("--field-manager=")
        for a in patch
        if a.startswith("--field-manager=")
    ]
    # Without the flag kubectl records `kubectl-patch`, which Flux's apply strips (#6746).
    assert managers == ["configarr-health"]


def test_a_failed_export_exits_one_after_the_log_line(capsys):
    def kubectl(*argv):
        return (
            (1, "Forbidden")
            if argv[0] == "patch"
            else {"get": (0, json.dumps(_JOBS)), "logs": (0, "Done\n")}[argv[0]]
        )

    assert _main(kubectl, verdict_configmap="configarr-health-verdict") == 1
    assert "status=up" in capsys.readouterr().out


def test_the_pod_carries_no_kuma_push_url():
    """M6 batch B6b's Cutover-2: no env var, Secret or `push` parameter is left."""
    [cronjob] = yaml_fast.safe_load_all(
        render_role_template("configarr", "health-cronjob.yaml.j2")
    )
    containers = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"][
        "containers"
    ]
    names = {e["name"] for c in containers for e in c.get("env", [])}
    assert "KUMA_PUSH_URL" not in names
    assert "push" not in inspect.signature(configarr_health.main).parameters
    assert not (ROLE / "templates" / "health-secret.yaml.j2").exists()


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
