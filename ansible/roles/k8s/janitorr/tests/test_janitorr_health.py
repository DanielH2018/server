"""The janitorr-health CronJob pushes its verdict, and its pod's layout imports the reader.

`files/janitorr_health.py` runs as the janitorr-health CronJob since H2.5c, reading its push
token from the janitorr-health Secret. The first tests drive its `main` with a stubbed kubectl
and a stubbed push. The last one builds the pod's `/config` directory from the rendered
ConfigMap, exactly as the kubelet mounts it, and runs the reader from it, so a module the
ConfigMap stops carrying fails here rather than in the next scheduled pod.

Run: uv run pytest ansible/roles/k8s/janitorr/tests/test_janitorr_health.py
"""

import importlib.util
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from _k8s_render import render_role_template
from lib import yaml_fast

import janitorr_health

# 2026-10-18T05:30:26Z, so a container started at the stamp below is one hour old.
_NOW = 1792301426.0
_STARTED = "2026-10-18T04:30:26Z"
_ERROR = "ERROR Unexpected error occurred in scheduled task\n"


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
_SYSLOG_UP = r"\S+ \S+ janitorr-health: status=up "


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

    return janitorr_health.main(kubectl=kubectl, push=push, now=lambda: _NOW)


def _replies(replies: dict[str, tuple[int, str]]):
    return lambda *argv: replies[argv[0]]


def test_a_clean_log_is_pushed_up_to_the_url_from_the_environment(pushes, capsys):
    kubectl = _replies({"get": (0, _STARTED), "logs": (0, "INFO cleanup done\n")})
    assert _main(pushes, kubectl) == 0
    [(status, msg, base, token)] = pushes
    assert (status, base, token) == (
        "up",
        "http://uptime-kuma.homelab.svc.cluster.local:3001",
        "tok",
    )
    assert msg == "no janitorr errors (up 1.0h)"
    assert re.match(_SYSLOG_UP, capsys.readouterr().out), "not in the syslog shape"


def test_a_push_kuma_rejects_parses_as_a_rejection_in_monitor_bridge(pushes, capsys):
    def push(status, msg, base, token, *, log):
        log("push failed (http=404 by=kuma) (status=%s: %s)" % (status, msg))
        return False

    janitorr_health.main(
        kubectl=_replies({"get": (0, _STARTED), "logs": (0, "INFO cleanup done\n")}),
        push=push,
        now=lambda: _NOW,
    )
    *_, last = capsys.readouterr().out.splitlines()
    assert _PARSER.parse_push_line(last)[:4] == (
        "janitorr-health",
        socket.gethostname(),
        "rejected",
        "up",
    ), last


def test_a_cleanup_error_past_the_grace_is_pushed_down(pushes):
    kubectl = _replies({"get": (0, _STARTED), "logs": (0, _ERROR)})
    assert _main(pushes, kubectl) == 0
    [(status, msg, _, _)] = pushes
    assert status == "down"
    assert msg.startswith("1 janitorr scheduled-task error(s)")


def test_a_reader_that_raises_pushes_down_naming_the_exception(pushes):
    def broken(*argv):
        raise RuntimeError("no kubectl")

    assert _main(pushes, broken) == 0
    [(status, msg, _, _)] = pushes
    assert status == "down"
    assert msg == "janitorr health reader failed: RuntimeError: no kubectl"


def test_with_no_token_it_prints_the_verdict_and_pushes_nothing(
    monkeypatch, pushes, capsys
):
    monkeypatch.setenv("KUMA_PUSH_URL", "")
    assert _main(pushes, _replies({"get": (1, "Forbidden")})) == 0
    assert pushes == []
    assert (
        capsys.readouterr().out
        == "down\tcannot read the janitorr pod in homelab: Forbidden\n"
    )


def test_the_reader_imports_from_the_pods_mounted_layout(tmp_path):
    [configmap] = yaml_fast.safe_load_all(
        render_role_template("janitorr", "health-configmap.yaml.j2")
    )
    [cronjob] = yaml_fast.safe_load_all(
        render_role_template("janitorr", "health-cronjob.yaml.j2")
    )
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    [volume] = [v for v in pod["volumes"] if "configMap" in v]
    assert volume["configMap"]["name"] == configmap["metadata"]["name"]
    # No `items:`, so the kubelet writes every key as a file of the same name.
    assert "items" not in volume["configMap"]
    for key, text in configmap["data"].items():
        (tmp_path / key).write_text(text)
    # `--help` returns after every module-level import has run, so exit 0 proves the import
    # closure resolves from the mounted files alone. `-S` drops the venv's site-packages: the
    # pod's bare python image has no wheels. `-P` keeps the working directory off sys.path.
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-P",
            str(tmp_path / "janitorr_health.py"),
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
