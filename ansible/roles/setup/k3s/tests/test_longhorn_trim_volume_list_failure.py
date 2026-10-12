"""A failed volume list aborts the trim rather than reading as a clean trim of zero (#4620).

`longhorn-trim-volumes.sh` reads `volumes.longhorn.io` through process substitution, so a
`kubectl get` that exits non-zero yields zero rows. Before the fix the run then logged
`trimmed 0 volume(s), 0 skipped, 0 failed` and exited 0, which check 9 reads as a clean trim.

These run the rendered script with a pods list that succeeds and a volume list that fails, the
window the earlier manager-pod lookup does not cover. `k3s`, `curl` and `hostname` are bash
functions exported into the child: the script prepends `/usr/local/bin`, where the real `k3s`
lives, so a stub on PATH would lose. `logger` is the directory's autouse PATH stub
(`conftest.py`), which records each call as `-t <tag> <message>`.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_trim_volume_list_failure.py
"""

import json
import os
from pathlib import Path

import pytest
from lib.proc_testing import run as launch
from longhorn_lib import longhorn_cron_evidence_logic as logic

from _shell_render import render_shell_script

NODE = "trim-test-node"

# The pods list answers with one ready manager on NODE; the volume list fails the way a
# transient apiserver error or a lost `list` verb does. Anything else answers empty.
K3S_STUB = r"""
case "$*" in
  *"get pods -l app=longhorn-manager"*) cat "$TRIM_STUB_PODS" ;;
  *"get volume"*)
    echo 'Error from server (Forbidden): volumes.longhorn.io is forbidden' >&2
    return 1 ;;
  *) : ;;
esac
"""

# The summary of deletion accounting that check 9 reads beside the trim, copied from
# `test_longhorn_backup_health_cron_evidence.py`, so the deletion half reports nothing.
_DELETIONS_CLEAN = "b2-deletions: charged 3, skipped 0, unpriced 0"


@pytest.fixture
def failed_list_run(tmp_path: Path, logger_calls: Path):
    """The trim run against a failing volume list: (CompletedProcess, longhorn-trim messages)."""
    pods = tmp_path / "pods.json"
    pods.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "spec": {"nodeName": NODE},
                        "status": {
                            "phase": "Running",
                            "podIP": "10.42.0.9",
                            "containerStatuses": [{"ready": True}],
                        },
                    }
                ]
            }
        )
    )
    stamps = tmp_path / "stamps"
    stamps.mkdir()
    script = tmp_path / "longhorn-trim-volumes.sh"
    script.write_text(
        render_shell_script(
            "setup",
            "k3s",
            "longhorn-trim-volumes.sh.j2",
            overrides={"k3s_cron_fired_stamp_dir": str(stamps)},
        )
    )
    proc = launch(
        ["bash", str(script)],
        env={
            **os.environ,
            "TRIM_STUB_PODS": str(pods),
            "BASH_FUNC_k3s%%": f"() {{ {K3S_STUB} }}",
            "BASH_FUNC_curl%%": "() { return 0; }",
            "BASH_FUNC_hostname%%": f"() {{ echo {NODE}; }}",
        },
    )
    prefix = "-t longhorn-trim "
    messages = [
        line[len(prefix) :]
        for line in logger_calls.read_text().splitlines()
        if line.startswith(prefix)
    ]
    return proc, messages


def test_a_failed_volume_list_logs_a_trim_abort_line(failed_list_run):
    proc, messages = failed_list_run
    assert any(logic._TRIM_ABORT_RE.match(m) for m in messages), (
        f"no longhorn-trim line matches _TRIM_ABORT_RE; logged {messages!r}, "
        f"stderr {proc.stderr!r}"
    )


def test_a_failed_volume_list_exits_1(failed_list_run):
    proc, _ = failed_list_run
    assert proc.returncode == 1, (proc.stdout, proc.stderr)


def test_a_failed_volume_list_prints_an_abort_line(failed_list_run):
    proc, _ = failed_list_run
    printed = (proc.stdout + proc.stderr).splitlines()
    assert any(line.startswith("ABORT:") for line in printed), printed


def test_check_9_flags_a_failed_volume_list(failed_list_run):
    _, messages = failed_list_run
    problems = logic.check_cron_evidence(messages, [_DELETIONS_CLEAN], 26)
    assert any("longhorn-trim aborted" in text for _, text in problems), (
        f"check 9 read {messages!r} as {problems!r}"
    )
