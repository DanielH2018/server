"""pull-pi-peers.sh's verdict is a line monitor-bridge can read, and it asks Kuma for nothing.

The CronJob runs in a pod with no `logger`, so its verdict goes to stdout in the host crons'
syslog shape — `<ts> <host> pi-peer-backup: ...` — and Alloy lands it under the pod's labels,
which `checks/logs.py` reads through a second selector. M6 batch B7's Cutover-2 removed the Kuma
push, so the script makes no request to Kuma and logs no `push failed` line.

`report()`, `export_verdict()` and `push()` are sourced by name and run against a stubbed `curl`;
the script itself cannot be sourced whole, since it rsyncs from the Pi at the top level. Each
emitted line goes through the reader's own `parse_push_line`, so the oracle is the consumer.
"""

import re
import shlex
from pathlib import Path

from _helpers import ROLES
from verdicts.logs import parse_push_line
from lib.proc_testing import run

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
    tmp_path: Path, status: str, gate_run: int = 0, hc_ping_url: str = ""
) -> tuple[list[str], list[str], list[str]]:
    """Run push() once with a recording `curl` stub.

    Returns (stdout lines, stderr lines, the curl calls). The export is off (no
    VERDICT_CONFIGMAP), so a curl call is the Healthchecks ping or a Kuma request.
    """
    calls = tmp_path / "curl-calls"
    script = f"""
set -uo pipefail
HC_PING_URL={shlex.quote(hc_ping_url)}
HOSTNAME={POD}
GATE_RUN={gate_run}
curl() {{ printf '%s\\n' "$*" >> {shlex.quote(str(calls))}; return 0; }}
{_function("report")}
{_function("export_verdict")}
{_function("push")}
push {status} "pulled 2 peer file(s) from daniel-pi"
"""
    result = run(["bash", "-c", script], check=True)
    recorded = calls.read_text().splitlines() if calls.exists() else []
    return result.stdout.splitlines(), result.stderr.splitlines(), recorded


def test_the_verdict_line_is_read_as_a_run(tmp_path):
    out, err, _ = _run_push(tmp_path, "up")
    assert err == []
    assert len(out) == 1, out
    assert parse_push_line(out[0]) == (
        TAG,
        POD,
        "run",
        "up",
        "pulled 2 peer file(s) from daniel-pi",
    )


def test_a_run_asks_kuma_for_nothing_and_only_the_healthchecks_ping_leaves(tmp_path):
    # ACCEPT: with a ping URL, the one request is the Healthchecks.io ping.
    _, err, calls = _run_push(
        tmp_path, "down", hc_ping_url="https://hc.example/key/slug"
    )
    assert err == []
    assert len(calls) == 1 and calls[0].endswith("https://hc.example/key/slug/fail")
    # REJECT: without one, no request leaves at all. A restored Kuma push would add a call here.
    _, _, none = _run_push(tmp_path / "bare", "up")
    assert none == []


def test_a_deploy_gate_run_emits_no_readable_line(tmp_path):
    # The gate run must not read as a nightly run either — same reason it does not report.
    out, err, calls = _run_push(tmp_path, "up", gate_run=1, hc_ping_url="https://h/x")
    assert err == [] and calls == []
    assert all(parse_push_line(line) is None for line in out), out
