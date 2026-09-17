"""The two crons that commit with hooks on deselect the ratchet only `--record` can repair — #1899.

The prek `pytest` hook is `always_run` and takes no filenames, so a docs-refresh or eval-run
commit needs the ENTIRE suite green. `test_the_recorded_weights_still_cover_most_of_the_suite`
goes red when enough test files land unweighted, and only a manual `pytest_shard.py --record`
clears it — so while it was red both crons' commits failed, the docs stopped publishing, and
docs-refresh's failure path reported `TREE STILL DIRTY`, which parks the GitOps deployer.

Both templates now set `PYTEST_ADDOPTS="--deselect <ratchet>"` on the `git commit` line and
nowhere else, so the deselect reaches the hook's `python -m pytest` and nothing else the cron
runs. CI's sharded pytest job sets no `PYTEST_ADDOPTS`, so the ratchet is still enforced there.

The node id lives once, as `pytest_shard.RATCHET_NODE_ID`. The text half here pins both
templates to it — a rename of the ratchet that forgot the templates would put the crons back
where #1899 found them, with nothing to say so. The executing half collects the ratchet's own
file under that `PYTEST_ADDOPTS` and asserts the ratchet is gone, which is the proof that
pytest honours the variable and that the node id names a test that exists; without the
variable it must be present, or the deselect is matching nothing.

Run: uv run pytest ansible/tests/setup/test_crons_deselect_the_weights_ratchet.py
"""

import os
import subprocess
import sys

import pytest
import pytest_shard

from _helpers import REPO

TEMPLATES = REPO / "ansible/roles/setup/initial_setup/templates"
CRONS = [
    pytest.param(TEMPLATES / "docs-refresh.sh.j2", id="docs-refresh"),
    pytest.param(TEMPLATES / "eval-run.sh.j2", id="eval-run"),
]
DESELECT = f'PYTEST_ADDOPTS="--deselect {pytest_shard.RATCHET_NODE_ID}"'


@pytest.mark.parametrize("path", CRONS)
def test_the_commit_line_deselects_the_ratchet_and_nothing_else_does(path):
    lines = path.read_text().splitlines()
    hits = [i for i, line in enumerate(lines) if DESELECT in line]
    assert len(hits) == 1, (
        f"{path.name} sets {DESELECT!r} {len(hits)} times; expected exactly once, on the "
        "line that continues into `git commit`"
    )
    prefix, following = lines[hits[0]], lines[hits[0] + 1]
    assert prefix.rstrip().endswith("\\"), (
        f"{path.name}: the assignment must continue into the commit"
    )
    assert following.lstrip().startswith("git commit"), (
        f"{path.name}: the deselect prefixes {following.strip()!r}, not the commit"
    )
    elsewhere = [
        line
        for i, line in enumerate(lines)
        if i != hits[0]
        and "PYTEST_ADDOPTS" in line
        and not line.lstrip().startswith("#")
    ]
    assert not elsewhere, (
        f"{path.name} names PYTEST_ADDOPTS outside the commit line: {elsewhere}"
    )


def test_the_ratchet_is_still_enforced_in_ci():
    """The variable the crons set must not reach the sharded job, which is CI's only run."""
    workflow = (REPO / ".github/workflows/ci.yml").read_text()
    assert "PYTEST_ADDOPTS" not in workflow


def _collected(addopts: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    if addopts is not None:
        env["PYTEST_ADDOPTS"] = addopts
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "-o",
            "addopts=",
            pytest_shard.RATCHET_TEST,
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_the_deselect_the_crons_set_removes_the_ratchet_from_the_hooks_run():
    """FLAGGED half: the same PYTEST_ADDOPTS reaches pytest and takes the ratchet out."""
    out = _collected(f"--deselect {pytest_shard.RATCHET_NODE_ID}")
    assert pytest_shard.RATCHET_NODE_ID not in out, out
    assert "deselected" in out, out


def test_without_the_deselect_the_ratchet_is_collected():
    """CLEAN half: the node id names a test that exists, so the deselect matches something."""
    out = _collected(None)
    assert pytest_shard.RATCHET_NODE_ID in out, out
