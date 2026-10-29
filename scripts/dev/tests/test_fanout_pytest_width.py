"""Every review-pipeline phase runs pytest under its host's worker ceiling, not the user's 4.

Run: uv run pytest scripts/dev/tests/test_fanout_pytest_width.py
"""

import json
import os
import shlex

import pytest
from _review_fakes import PR, _finding, _pipeline, _report
from fanout_lib.launch import systemd_run_command
from fanout_lib.review.held_hooks import PYTEST_WORKERS_ENV, pytest_workers
from fanout_lib.target import Target


@pytest.mark.parametrize(
    ("cores", "workers"), [(4, 2), (8, 2), (12, 3), (16, 4), (32, 4), (None, 4)]
)
def test_the_ceiling_is_a_quarter_of_the_cores_between_two_and_four(cores, workers):
    assert pytest_workers(cores) == workers


def _env(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--settings") + 1]).get("env", {})


def test_every_phase_of_a_server_batch_on_an_8_core_host_runs_two_workers(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(os, "process_cpu_count", lambda: 8)
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [_finding("off by one")]}),
        _report(f"Fixed it. {PR}"),
        _report(structured={"summary": "resolved", "findings": []}),
    ]
    pipeline, run = _pipeline(tmp_path, reports)
    pipeline.run_all()
    phases = [phase for _, _, phase in run.claude]
    assert phases[:2] == ["implement", "review"] and len(phases) == 4
    for argv, _, phase in run.claude:
        # `_claude` adds the OTEL labels to this env too (#5684); the worker pin is the subject.
        assert _env(argv)[PYTEST_WORKERS_ENV] == "2", phase


def test_a_16_core_host_keeps_four_and_another_repos_phase_gets_it_too(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(os, "process_cpu_count", lambda: 16)
    dotfiles = Target("DanielH2018/dotfiles", str(tmp_path), "origin/main")
    pipeline, _ = _pipeline(tmp_path, [], target=dotfiles)
    pipeline.session = "sid-1"
    assert _env(pipeline._resume()) == {PYTEST_WORKERS_ENV: "4"}


def test_the_placement_time_unit_carries_no_worker_count():
    """`systemd_run_command` runs on the placing host, whose core count is the wrong one."""
    dotfiles = Target("DanielH2018/dotfiles", "/srv/dotfiles", "origin/main")
    argv = shlex.split(systemd_run_command("763", dotfiles))
    assert "env" not in json.loads(argv[argv.index("--settings") + 1])
