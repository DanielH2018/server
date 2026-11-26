"""The determinism modules' digest lines reach the digest file of an xdist run.

CI runs every shard under xdist, where a test runs in a worker and only the controller sees
the whole session. The test runs a pytest session of its own under `-n 2` with this directory's real
`conftest.py`. Its inner test records a fixture tree's lines through `report_digests`, the
function `check_part` ends with, so the lines take the same path out of a worker as in CI.

Run: uv run pytest scripts/renderer/tests/bundle/test_renderer_digest_log.py
"""

import os
import shutil
import sys
from pathlib import Path

from lib import yaml_fast
from lib.proc_testing import run
from lib.repo_paths import REPO, SCRIPTS
from renderer.renderer_lib.shadow import digest_line, directory_digests

HERE = Path(__file__).resolve().parents[1]

INNER_TESTS = """
from _render_audit import report_digests


def test_a_part_records_its_lines(tmp_path, record_property):
    (tmp_path / "cluster/littlelink").mkdir(parents=True)
    (tmp_path / "cluster/littlelink/service.yaml").write_text("port: 80\\n")
    report_digests(tmp_path, "position 2 of 4", record_property)


def test_another_property_is_not_a_digest(record_property):
    record_property("unrelated", {"sha": "f00", "module": "m", "lines": ["flux sha256:cd"]})
"""


def test_a_workers_digest_lines_reach_the_file_and_the_log_prints_their_count(tmp_path):
    tree = tmp_path / "expected"
    (tree / "cluster/littlelink").mkdir(parents=True)
    (tree / "cluster/littlelink/service.yaml").write_text("port: 80\n")
    [expected] = [digest_line(n, h) for n, h in directory_digests(tree).items()]
    inner = tmp_path / "inner"
    inner.mkdir()
    saved = tmp_path / "render-digests.txt"
    shutil.copy(HERE / "conftest.py", inner / "conftest.py")
    (inner / "pytest.ini").write_text("[pytest]\n")
    (inner / "test_inner.py").write_text(INNER_TESTS)
    # Without the summary path, so a CI run of this test leaves the job summary alone.
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
    done = run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "2",
            "-p",
            "no:cacheprovider",
            "-c",
            str(inner / "pytest.ini"),
            str(inner),
        ],
        cwd=inner,
        env={
            **env,
            "GITHUB_SHA": "f00",
            "RENDER_DIGEST_FILE": str(saved),
            "PYTHONPATH": os.pathsep.join([str(HERE), str(SCRIPTS)]),
        },
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "2 workers" in done.stdout, done.stdout
    # Once, though the teardown report carries the same property as the call report.
    assert saved.read_text() == f"render digest at f00 (position 2 of 4): {expected}\n"
    assert "render digest at" not in done.stdout, done.stdout
    assert f"render digests: 1 saved to {saved}" in done.stdout, done.stdout


def test_ci_uploads_the_file_its_test_step_saves_the_lines_to():
    workflow = yaml_fast.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    steps = {s.get("name"): s for s in workflow["jobs"]["pytest"]["steps"]}
    saved = steps["Run the test suite"]["env"]["RENDER_DIGEST_FILE"]
    assert steps["Upload the render digest lines"]["with"]["path"] == saved
