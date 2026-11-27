"""The configarr-health CronJob records its verdict as a log line, and its pod's layout imports the reader.

`files/configarr_health.py` runs as the configarr-health CronJob since H2.5b. M6 batch B6b's Cutover-2 removed its Kuma
push, so the first tests drive its `main` with a stubbed kubectl and read what it logs and
patches. The mounted-layout test builds the pod's `/config` directory from the rendered ConfigMap, exactly
as the kubelet mounts it, and runs the reader from it, so a module the ConfigMap stops carrying
fails here rather than in the next scheduled pod. The last test holds the condition parser's
volume key to the key-name shape that promote's bundle scan admits.

Run: uv run pytest k8s/roles/configarr/tests/test_configarr_health.py
"""

import json
import re
import socket
import subprocess
import sys
import tomllib

import pytest

from _k8s_render import render_role_template
from lib import yaml_fast
from lib.repo_paths import REPO

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


# The `<ts> <host> <tag>: ` shape rsyslog gave the host script's `logger -t` lines.
_SYSLOG_UP = r"\S+ \S+ configarr-health: status=up "


def _main(kubectl, **kwargs) -> int:
    return configarr_health.main(kubectl=kubectl, now=lambda: _NOW, **kwargs)


def _replies(replies: dict[str, tuple[int, str]]):
    return lambda *argv: replies[argv[0]]


def test_a_clean_run_logs_status_up_in_the_syslog_shape(capsys):
    # fact: k8s/roles/configarr/CLAUDE.md#Editing
    kubectl = _replies({"get": (0, json.dumps(_JOBS)), "logs": (0, "Done\n")})
    assert _main(kubectl) == 0
    out = capsys.readouterr().out
    assert re.match(_SYSLOG_UP, out), "not in the syslog shape"
    assert "configarr sync ok: Done (1.0h ago)" in out
    assert out.split()[1] == socket.gethostname()


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
    # Without the flag kubectl records `kubectl-patch`, which Flux's apply strips.
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


def test_the_reader_imports_from_the_pods_mounted_layout(tmp_path):
    # fact: k8s/roles/configarr/CLAUDE.md#Editing
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


def _bundle_key_name_allowlist() -> re.Pattern[str]:
    config = tomllib.loads((REPO / ".gitleaks.toml").read_text())
    [allowlist] = [
        a
        for a in config["allowlists"]
        if a.get("paths") == [r"^out/cluster/[^/]+/[^/]+\.yaml$"]
        and a.get("regexTarget") == "line"
    ]
    [regex] = allowlist["regexes"]
    return re.compile(regex)


def test_each_lib_key_is_a_name_promotes_gitleaks_scan_admits():
    # `generic-api-key` reads a lib module's file name as a credential when it is the key, and the
    # renderer drops any `# gitleaks:allow` on the line, so only the allowlist's shape passes.
    [cronjob] = yaml_fast.safe_load_all(
        render_role_template("configarr", "health-cronjob.yaml.j2")
    )
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    [volume] = [v for v in pod["volumes"] if "configMap" in v]
    items = [i for i in volume["configMap"]["items"] if i["path"].startswith("lib/")]
    assert {i["path"] for i in items} == {
        "lib/k8s_conditions.py",
        "lib/check_verdict.py",
    }
    for item in items:
        assert _bundle_key_name_allowlist().match(f"          - key: {item['key']}")


def test_age_seconds_refuses_a_stamp_with_no_offset():
    with pytest.raises(ValueError, match="no UTC offset"):
        configarr_health.age_seconds("2026-10-18", _NOW)
    assert configarr_health.age_seconds("2026-10-18T04:30:26Z", _NOW) == 3600.0
