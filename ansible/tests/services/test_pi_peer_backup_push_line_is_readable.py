"""pull-pi-peers.sh's push outcome is a line monitor-bridge can read (issue #1943).

The CronJob runs in a pod with no `logger`, so its verdict and a failed push go to stdout in
the host crons' syslog shape — `<ts> <host> pi-peer-backup: ...` — and Alloy lands them under
the pod's labels, which `checks/logs.py` reads through a second selector. Until 2026-09-18 it
wrote `kuma push failed (<status>: <msg>)` to stderr, which no reader matched, so a push Kuma
rejected — a token no live tile holds (#1803) — was invisible from this pusher.

`report()` and `push()` are sourced by name and run against a stubbed `curl`; the script
itself cannot be sourced whole, since it rsyncs from the Pi at the top level. Each emitted
line goes through the reader's own `parse_push_line`, so the oracle is the consumer.
"""

import re
import shlex
import subprocess
from pathlib import Path

from _helpers import ROLES
from verdicts.logs import parse_push_line

SCRIPT_PATH = ROLES / "k8s/pi-peer-backup/files/pull-pi-peers.sh"
TAG = "pi-peer-backup"
POD = "pi-peer-backup-29312345-x7k2q"


def _function(name: str) -> str:
    match = re.search(
        rf"^{name}\(\) \{{[^\n]*\n.*?^\}}$", SCRIPT_PATH.read_text(), re.M | re.S
    )
    assert match, f"{name}() is gone from pull-pi-peers.sh — sourced by name"
    return match.group(0)


def _run_push(
    tmp_path: Path, reply: str, curl_rc: int, status: str, gate_run: int = 0
) -> tuple[list[str], list[str]]:
    """Run push() once with curl stubbed to print `reply` for `-w` and exit `curl_rc`.

    Returns (stdout lines, stderr lines). HC_PING_URL stays unset so only the Kuma leg runs.
    """
    script = f"""
set -uo pipefail
KUMA_PUSH_URL=http://kuma.example/api/push/secret-token
HOSTNAME={POD}
GATE_RUN={gate_run}
curl() {{ printf '%s' {shlex.quote(reply)}; exit {curl_rc}; }}
{_function("report")}
{_function("push")}
push {status} "pulled 2 peer file(s) from daniel-pi"
"""
    result = subprocess.run(
        ["bash", "-c", script], check=True, capture_output=True, text=True
    )
    return result.stdout.splitlines(), result.stderr.splitlines()


def _parsed(line: str):
    parsed = parse_push_line(line)
    assert parsed is not None, line
    return parsed


def test_the_verdict_line_is_read_as_a_run(tmp_path):
    out, err = _run_push(tmp_path, "200 application/json; charset=utf-8", 0, "up")
    assert err == []
    assert len(out) == 1, out
    assert _parsed(out[0]) == (
        TAG,
        POD,
        "run",
        "up",
        "pulled 2 peer file(s) from daniel-pi",
    )


def test_a_kuma_json_404_is_read_as_rejected(tmp_path):
    # ACCEPT: Kuma refusing the token answers 404 as application/json (measured 2026-09-18);
    # the in-cluster Service URL has no Traefik in front, so this is the shape a bad token
    # takes from this pod.
    out, err = _run_push(tmp_path, "404 application/json; charset=utf-8", 0, "up")
    assert len(out) == 1 and len(err) == 1, (out, err)
    assert _parsed(err[0]) == (TAG, POD, "rejected", "up", "http=404 rc=0 by=kuma")


def test_a_transport_failure_is_read_as_swallowed_not_rejected(tmp_path):
    # REJECT (the pair): curl dying before a response prints `000 ` and a non-zero rc, and
    # no `by=kuma` — Kuma did not answer.
    _out, err = _run_push(tmp_path, "000 ", 7, "down")
    assert _parsed(err[0]) == (TAG, POD, "swallowed", "down", "http=000 rc=7")


def test_a_deploy_gate_run_emits_no_readable_line(tmp_path):
    # The gate run must not read as a nightly run either — same reason it does not push.
    out, err = _run_push(tmp_path, "200 ", 0, "up", gate_run=1)
    assert err == []
    assert all(parse_push_line(line) is None for line in out), out
