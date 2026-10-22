"""`probe.py health longhorn-backup`: the BackupTarget availability verdict (H1.7b).

The role renders no workload, so the gate reads each target's `status.available`. The
`_is_clean` and `_is_flagged` pairs are the red proof the plan asks for: an armed target that is
not available fails, a disarmed one is named and skipped, and a list without `default` cannot
pass.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_backup_targets.py
"""

import json
import subprocess
from pathlib import Path

import pytest

from diagnostics.probe_lib import health, health_alerts
from diagnostics.probe_lib.health_backup_targets import (
    RECONCILE_ANNOTATION,
    format_backup_target_health,
    target_verdicts,
)
from lib.exit_codes import PROBE_UNREADABLE
from lib.kubectl import Tools

URL = "s3://bucket@us-east-005/longhorn"


def _target(name, url=URL, available=True, annotated=False, message=""):
    item = {
        "metadata": {"name": name, "annotations": {}},
        "spec": {"backupTargetURL": url},
        "status": {"available": available},
    }
    if annotated:
        item["metadata"]["annotations"][RECONCILE_ANNOTATION] = "disabled"
    if message:
        item["status"]["conditions"] = [{"message": message}]
    return item


def test_two_available_targets_is_clean():
    text, code = format_backup_target_health([_target("default"), _target("r2")])
    assert code == 0
    assert "available: default, r2" in text


def test_an_armed_target_that_is_not_available_is_flagged():
    text, code = format_backup_target_health(
        [_target("default", available=False, message="AccessDenied"), _target("r2")]
    )
    assert code == 1
    assert "backup target default is armed and not available: AccessDenied" in text


def test_a_disarmed_target_with_a_blank_url_is_clean_and_named():
    text, code = format_backup_target_health(
        [_target("default", url="", available=False), _target("r2")]
    )
    assert code == 0
    assert "disarmed on purpose: default" in text


def test_a_list_without_a_default_target_is_flagged():
    for items in ([], [_target("r2")]):
        text, code = format_backup_target_health(items)
        assert code == 1
        assert "no backup target named default" in text


def test_a_target_held_by_the_break_glass_annotation_is_flagged_even_when_blank():
    _, _, problems = target_verdicts(
        [_target("default", url="", available=False, annotated=True), _target("r2")]
    )
    assert problems == [
        f"backup target default carries {RECONCILE_ANNOTATION}=disabled: a break-glass is "
        "in force and Flux is not enforcing it"
    ]


_QUIET = health_alerts.PromTools(query=lambda promql: [], sleep=lambda s: None)


_NODES = json.dumps({"items": [{"metadata": {"name": "daniel-box"}}]})


def _tools(stdout: str, returncode: int = 0, stderr: str = "") -> Tools:
    """A kubectl boundary: the identity read sees prod, and every other call gets one answer."""

    def run(argv, timeout):
        if "nodes" in argv:
            return subprocess.CompletedProcess(argv, 0, _NODES, "")
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return Tools(
        run=run,
        find_tool=lambda name: "/usr/bin/kubectl",
        find_kubeconfig=lambda: Path("/tmp/kubeconfig"),
    )


def _run_health(tools: Tools) -> int:
    return health.run_health(
        "longhorn-backup", cluster="prod", served="prod", tools=tools, prom=_QUIET
    )


def test_run_health_reads_the_list_and_reports_it(capsys):
    items = json.dumps({"items": [_target("default"), _target("r2")]})
    assert _run_health(_tools(items)) == 0
    assert "available: default, r2" in capsys.readouterr().out


def test_run_health_fails_an_unavailable_target(capsys):
    items = json.dumps({"items": [_target("default", available=False), _target("r2")]})
    assert _run_health(_tools(items)) == 1
    assert "backup target default is armed and not available" in capsys.readouterr().out


@pytest.mark.parametrize("stdout", ["", "not json"])
def test_run_health_with_an_unreadable_list_is_unreadable_not_a_pass(capsys, stdout):
    code = _run_health(_tools(stdout, returncode=1, stderr="connection refused"))
    assert code == PROBE_UNREADABLE
    assert "longhorn-backup" in capsys.readouterr().err
