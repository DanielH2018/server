"""Every probe.py verdict subcommand ends with one `PROBE-VERDICT:` line (#4454).

The line comes from `lib.exit_codes.verdict_line`, so its verdict token is derived from the exit
code rather than from each subcommand's own vocabulary. The oracle for the token is the probe's
entry in `CONTRACTS`, not a string copied from the subcommand. Each run stubs only the transport
under the subcommand (`subprocess.run`) and drives `probe.main`, which is where an exit code becomes a line.

Run: uv run pytest scripts/diagnostics/tests/test_probe_verdict_line.py
"""

import argparse
import json
import os
import re
import subprocess
import sys

import pytest

# `scripts/diagnostics` is deliberately absent from `pythonpath` in pyproject.toml, so this
# module puts its own parent directory on `sys.path` — the insert every sibling here carries.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import probe
from diagnostics.probe_lib.cli_parser import _build_parser
from lib.exit_codes import FAILED, OK, TEMP_FAIL, contract, verdict_line

PROBE = "scripts/diagnostics/probe.py"
VERDICT_LINE = re.compile(r"^PROBE-VERDICT: (\S+) \((.+)\)$")
PI_SERVICE = "wg-easy"

# The verdict subcommands the issue names, plus every one whose help already states an exit
# code. `ha` reaches its verdict through the `verify-automations` subcommand.
VERDICT_SUBCOMMANDS = (
    ("monitors",),
    ("targets",),
    ("kuma-drift",),
    ("health",),
    ("pi",),
    ("vip-placement",),
    ("readonly-rbac",),
    ("alerts",),
    ("b2-longhorn",),
    ("b2-budget",),
    ("b2-deletions",),
    ("landing",),
    ("gitops-state",),
    ("ha", "verify-automations"),
    ("ha", "verify-entities"),
)


def _exit_code(call):
    try:
        return call()
    except SystemExit as exc:
        return exc.code


def _verdict_for(rc):
    """The verdict token the probe's contract gives `rc`."""
    for code in contract(PROBE):
        if code.value == rc:
            return code.verdict
    raise AssertionError(f"{PROBE}'s contract has no code {rc}")


def _assert_ends_with_verdict(out, rc):
    lines = out.rstrip("\n").splitlines()
    assert lines, "the subcommand printed nothing"
    match = VERDICT_LINE.match(lines[-1])
    assert match, f"last line is not a PROBE-VERDICT line: {lines[-1]!r}"
    assert match.group(1) == _verdict_for(rc)


def _completed(argv, stdout, returncode=0):
    return subprocess.CompletedProcess(argv, returncode, stdout, "")


def _subparser(path):
    parser = _build_parser()
    help_text = ""
    for name in path:
        action = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        help_text = next(
            (c.help or "" for c in action._choices_actions if c.dest == name), ""
        )
        parser = action.choices[name]
    return help_text, parser


# ── the renderer ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rc", [OK, FAILED, TEMP_FAIL])
def test_verdict_line_renders_a_probe_verdict_for_each_probe_code(rc):
    line = verdict_line(PROBE, rc, "detail")
    assert line is not None and VERDICT_LINE.match(line), line


def test_the_probe_verdicts_for_ok_fail_and_unreadable_are_distinct():
    tokens = {_verdict_for(rc) for rc in (OK, FAILED, TEMP_FAIL)}
    assert len(tokens) == 3 and "" not in tokens


# ── each subcommand's last line ─────────────────────────────────────────────────────────────


def _monitor_status(status):
    return json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"monitor_name": "Jellyfin"}, "value": [0, status]}
                ],
            },
        }
    )


@pytest.mark.parametrize("status, expected_rc", [("1", OK), ("0", FAILED)])
def test_monitors_ends_with_its_verdict(monkeypatch, capsys, status, expected_rc):
    def run(argv, *args, **kwargs):
        # `k8s_endpoint` reads the domain from SOPS before the curl.
        if argv[0] == "sops":
            return _completed(argv, "example.test\n")
        return _completed(argv, _monitor_status(status))

    monkeypatch.setattr(subprocess, "run", run)
    rc = _exit_code(lambda: probe.main(["monitors"]))
    assert rc == expected_rc
    _assert_ends_with_verdict(capsys.readouterr().out, rc)


def test_health_docker_ends_with_its_verdict(monkeypatch, capsys):
    inspect = [
        {"Name": f"/{PI_SERVICE}", "State": {"Status": "running"}, "RestartCount": 0}
    ]
    monkeypatch.setattr(
        subprocess, "run", lambda argv, *a, **k: _completed(argv, json.dumps(inspect))
    )
    rc = _exit_code(lambda: probe.main(["health", PI_SERVICE, "--docker"]))
    assert rc == OK
    _assert_ends_with_verdict(capsys.readouterr().out, rc)


def test_health_docker_that_cannot_reach_the_pi_ends_with_the_unreadable_verdict(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, *a, **k: subprocess.CompletedProcess(
            argv, 255, "", "ssh: connect to host daniel-pi port 22: Connection refused"
        ),
    )
    rc = _exit_code(lambda: probe.main(["health", PI_SERVICE, "--docker"]))
    assert rc == TEMP_FAIL
    _assert_ends_with_verdict(capsys.readouterr().out, rc)


def test_pi_containers_ends_with_its_verdict(monkeypatch, capsys):
    containers = [
        {
            "Name": f"/{PI_SERVICE}",
            "Config": {"Image": "ghcr.io/wg-easy/wg-easy:15"},
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
            "NetworkSettings": {"Networks": {"bridge": {}}},
        }
    ]
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, *a, **k: _completed(argv, json.dumps(containers)),
    )
    rc = _exit_code(lambda: probe.main(["pi", "containers"]))
    assert rc == OK
    _assert_ends_with_verdict(capsys.readouterr().out, rc)


# ── each subcommand's help ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", VERDICT_SUBCOMMANDS, ids=" ".join)
def test_verdict_subcommand_help_names_the_unreadable_exit(path):
    help_text, parser = _subparser(path)
    text = help_text + "\n" + parser.format_help()
    assert re.search(rf"\b{TEMP_FAIL} = ", text), (
        f"`probe.py {' '.join(path)}` help has no `{TEMP_FAIL} = ...` exit clause"
    )


def test_a_json_view_prints_no_verdict_line_after_its_document(capsys):
    """`--json` stdout is one document, and the deck mod parses `landing --json` as one.

    `gitops-state` reads the deployer's state directory and nothing over a network, so the run
    is real; whatever the directory holds, stdout must still parse as JSON.
    """
    probe.main(["gitops-state", "--json"])
    out = capsys.readouterr().out
    assert "PROBE-VERDICT" not in out
    json.loads(out)


def test_a_message_exit_from_a_verdict_subcommand_is_unreadable_with_its_line(
    monkeypatch, capsys
):
    """`SystemExit("...")` exits 1, the fail verdict, and skips the line unless main catches it."""
    from diagnostics.probe_lib import subcommands

    def refused(ns):
        raise SystemExit("HA websocket auth failed (check claude_ha_token)")

    monkeypatch.setattr(subcommands.REGISTRY.get("landing"), "func", refused)
    rc = _exit_code(lambda: probe.main(["landing"]))
    captured = capsys.readouterr()
    assert rc == TEMP_FAIL
    assert "HA websocket auth failed" in captured.err
    _assert_ends_with_verdict(captured.out, rc)
