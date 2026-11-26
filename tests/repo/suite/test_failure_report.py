"""A failed run ends on its failure, and `--github-report` names each failed test to CI.

Each run is a real pytest subprocess under `-n 2` over a scratch tree, with a bare `pytest.ini`
so the repo's own `addopts` stay out, and `phase_timing` loaded beside the plugin so a second
summary-printing plugin is in the run. `GITHUB_STEP_SUMMARY` points into the scratch tree, so
a CI run of these tests leaves the real job summary alone.
The last two tests read the repo's own `pyproject.toml` and `ci.yml`, which wire both halves in.

Run: uv run pytest tests/repo/suite/test_failure_report.py
"""

import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

from _helpers import REPO
from lib import yaml_fast

_MODULE = """
def test_passes():
    pass


def test_compares():
    assert 1 == 2, "one is not two, nor three"


def test_fails_too():
    raise RuntimeError("boom")
"""


def _run(tmp_path: Path, *args: str) -> tuple[str, Path]:
    (tmp_path / "test_scratch.py").write_text(_MODULE)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    summary = tmp_path / "summary.md"
    summary.write_text("### written by a test\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "tests"), str(REPO / "scripts")])
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "2",
            "-p",
            "no:cacheprovider",
            "-p",
            "phase_timing",
            "-p",
            "failure_report",
            "--durations=0",
            *args,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    return done.stdout, summary


def test_failures_print_after_the_durations_and_before_the_short_summary(tmp_path):
    out, _ = _run(tmp_path)
    failures = out.index("= FAILURES =")
    assert out.index("slowest durations") < failures, out
    assert out.index("phase timing") < failures, out
    assert failures < out.index("short test summary info"), out
    assert out.count("= FAILURES =") == 1, out


def test_github_report_annotates_each_failed_test_at_its_crash_line(tmp_path):
    out, _ = _run(tmp_path, "--github-report")
    annotations = [line for line in out.splitlines() if line.startswith("::error")]
    assert sorted(annotations) == sorted(
        [
            "::error file=test_scratch.py,line=7,title=test_scratch.py%3A%3Atest_compares"
            "::AssertionError: one is not two, nor three",
            "::error file=test_scratch.py,line=11,title=test_scratch.py%3A%3Atest_fails_too"
            "::RuntimeError: boom",
        ]
    ), out


def test_github_report_leads_the_job_summary_with_a_count_line(tmp_path):
    _, summary = _run(tmp_path, "--github-report")
    lines = summary.read_text().splitlines()
    assert lines[0] == "**pytest: 2 failed, 1 passed**", lines
    assert (
        "- `test_scratch.py::test_compares`: AssertionError: one is not two, nor three"
        in lines
    )
    assert lines[-1] == "### written by a test", lines


def test_without_github_report_nothing_is_annotated_or_summarised(tmp_path):
    out, summary = _run(tmp_path)
    assert "::error" not in out, out
    assert summary.read_text() == "### written by a test\n"


def test_the_repos_addopts_load_the_plugin():
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    args = shlex.split(config["tool"]["pytest"]["ini_options"]["addopts"])
    assert any(args[i : i + 2] == ["-p", "failure_report"] for i in range(len(args))), (
        args
    )


def test_ci_runs_the_suite_with_github_report_and_no_colour():
    workflow = yaml_fast.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    [run] = [
        step["run"]
        for step in workflow["jobs"]["pytest"]["steps"]
        if step.get("name") == "Run the test suite"
    ]
    assert "--github-report" in run, run
    assert "--color" not in run, run
