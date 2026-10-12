"""A probe that cannot read its source exits TEMP_FAIL (75), never a verdict code (#4444).

Exit 1 means the probe looked and saw something bad. A curl that failed, an ssh that never
reached daniel-pi or a Prometheus query that returned no series is not that: nothing was
observed, so a caller has to be able to read "retry" off the exit code. Each test stubs only the
transport (`subprocess.run`, which also carries the SOPS domain read) and drives the real code above it.

Run: uv run pytest scripts/diagnostics/tests/test_probe_unreadable_source.py
"""

import json
import os
import subprocess
import sys

import pytest

# `scripts/diagnostics` is deliberately absent from `pythonpath` in pyproject.toml, so this
# module puts its own parent directory on `sys.path` — the insert every sibling here carries.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import probe
from diagnostics.probe_lib import core, health_docker, readonly_rbac, vip_placement
from lib.exit_codes import TEMP_FAIL, contract

PROBE = "scripts/diagnostics/probe.py"
CURL_ERROR = "curl: (6) Could not resolve host: prometheus.local.example.test"
SSH_ERROR = "ssh: connect to host daniel-pi port 22: Connection refused"
# A service daniel-pi's inventory really declares, so the absent-container branch is the
# MISSING one rather than "not a declared service".
PI_SERVICE = "wg-easy"


def _exit_code(call):
    """The code `call` exits with, whether it returns it or raises SystemExit carrying it."""
    try:
        return call()
    except SystemExit as exc:
        return exc.code


def _failing_transport(stderr, returncode):
    """A `subprocess.run` whose curl or ssh fails; the SOPS domain read still answers."""

    def run(argv, *args, **kwargs):
        if argv[0] == "sops":
            return subprocess.CompletedProcess(argv, 0, "example.test\n", "")
        return subprocess.CompletedProcess(argv, returncode, "", stderr)

    return run


def test_a_failed_fetch_exits_temp_fail_with_the_failure_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", _failing_transport(CURL_ERROR, 6))
    rc = _exit_code(lambda: probe.main(["monitors"]))
    captured = capsys.readouterr()
    assert rc == TEMP_FAIL
    assert "Could not resolve host" in captured.err
    assert "Could not resolve host" not in captured.out


def test_a_failed_config_get_exits_temp_fail_with_the_failure_on_stderr(
    monkeypatch, capsys
):
    monkeypatch.setattr(subprocess, "run", _failing_transport(CURL_ERROR, 6))
    rc = _exit_code(
        lambda: core.config_get(
            "https://ha.local.example.test/api/states", 'header = "X: y"'
        )
    )
    captured = capsys.readouterr()
    assert rc == TEMP_FAIL
    assert "Could not resolve host" in captured.err


def test_an_empty_monitor_status_result_is_unreadable_not_monitors_down(
    monkeypatch, capsys
):
    def run(argv, *args, **kwargs):
        if argv[0] == "sops":
            return subprocess.CompletedProcess(argv, 0, "example.test\n", "")
        body = json.dumps({"status": "success", "data": {"result": []}})
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(subprocess, "run", run)
    rc = _exit_code(lambda: probe.main(["monitors"]))
    assert rc == TEMP_FAIL
    assert "monitor_status" in capsys.readouterr().err


def test_an_ssh_failure_under_health_docker_is_not_reported_missing(
    monkeypatch, capsys
):
    assert health_docker.declared_on_pi(PI_SERVICE)
    monkeypatch.setattr(subprocess, "run", _failing_transport(SSH_ERROR, 255))
    rc = _exit_code(lambda: probe.main(["health", PI_SERVICE, "--docker"]))
    captured = capsys.readouterr()
    assert "MISSING" not in captured.out
    assert rc == TEMP_FAIL
    assert "Connection refused" in captured.err


def test_unparseable_inspect_output_under_health_docker_is_not_reported_missing(
    monkeypatch, capsys
):
    assert health_docker.declared_on_pi(PI_SERVICE)

    def run(argv, *args, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "not json at all", "")

    monkeypatch.setattr(subprocess, "run", run)
    rc = _exit_code(lambda: probe.main(["health", PI_SERVICE, "--docker"]))
    assert "MISSING" not in capsys.readouterr().out
    assert rc == TEMP_FAIL


# `readonly-rbac` is the model the issue names, apart from its exit code: refused control verbs
# mean the probe could not look, and so does a placement read that came back empty.
def test_readonly_rbac_with_its_control_verbs_refused_is_unreadable():
    allowed = dict.fromkeys(readonly_rbac.READONLY_ALLOWED, False)
    denied = dict.fromkeys(readonly_rbac.READONLY_DENIED, False)
    _, code = readonly_rbac.format_readonly_rbac(denied, allowed)
    assert code == TEMP_FAIL


def test_vip_placement_with_nothing_read_is_unreadable():
    _, code = vip_placement.format_vip_placement([], [], set())
    assert code == TEMP_FAIL


def test_the_probe_contract_declares_the_unreadable_code():
    values = {code.value for code in contract(PROBE)}
    assert TEMP_FAIL in values


def test_probe_help_documents_every_code_in_its_contract(capsys):
    codes = contract(PROBE)
    assert codes, f"{PROBE} declares no exit contract in lib/exit_codes.py"
    with pytest.raises(SystemExit):
        probe.main(["--help"])
    text = capsys.readouterr().out
    for code in codes:
        assert str(code.value) in text, f"--help does not document exit {code.value}"


# The k8s half of `health`: a kubectl read that failed is not a workload that is absent.
def _k8s_tools(stderr):
    """A `lib.kubectl.Tools` that serves the prod node list and fails every other read."""
    nodes = {
        "items": [{"metadata": {"name": n}} for n in ("daniel-box", "daniel-server")]
    }

    def run(argv, timeout):
        if argv[-4:] == ["get", "nodes", "-o", "json"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(nodes), "")
        return subprocess.CompletedProcess(argv, 1, "", stderr)

    return kubectl.Tools(
        run=run,
        find_tool=lambda name: "/usr/local/bin/kubectl",
        find_kubeconfig=lambda: Path("/tmp/kubeconfig"),
    )


def test_a_failed_kubectl_read_under_health_is_unreadable_not_missing(capsys):
    tools = _k8s_tools("Unable to connect to the server: dial tcp: i/o timeout")
    rc = _exit_code(lambda: health.run_health("jellyfin", served="prod", tools=tools))
    captured = capsys.readouterr()
    assert rc == TEMP_FAIL
    assert "MISSING" not in captured.out
    assert "Unable to connect to the server" in captured.err


def test_a_notfound_kubectl_read_under_health_is_still_missing(capsys):
    tools = _k8s_tools(
        'Error from server (NotFound): deployments.apps "jellyfin" not found'
    )
    rc = _exit_code(lambda: health.run_health("jellyfin", served="prod", tools=tools))
    assert rc == 1
    assert "MISSING" in capsys.readouterr().out


def test_probe_help_renders_each_contract_row_as_value_and_meaning(capsys):
    """A bare `"0" in text` matches any digit in the help, so check each rendered row whole."""
    with pytest.raises(SystemExit):
        probe.main(["--help"])
    # argparse wraps the epilog, so compare with the line breaks collapsed.
    text = " ".join(capsys.readouterr().out.split())
    for code in contract(PROBE):
        row = f"{code.value} = {code.meaning.rstrip('.')}"
        assert row in text, f"--help does not render {row!r}"


# Appended rather than placed at the top, because nothing above may change.
from pathlib import Path  # noqa: E402

from diagnostics.probe_lib import health  # noqa: E402
from lib import kubectl  # noqa: E402
