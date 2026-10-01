"""The two crons that publish a commit run the suite themselves, and skip it again at push.

The prek `pytest` hook runs at the pre-push stage, so `git commit` gates nothing and each
cron runs the suite on its own line instead. Losing that silently is the failure this
module exists to catch — both crons would keep publishing generated pages or a sweep, and
only CI would say the suite was red, after the pages were already live.

The second half is the push. The publisher (`publish_pr.py`) pushes a branch, which is exactly
where the pre-push hook fires, so without `SKIP=pytest` each cron would pay the suite twice and
move its verdict to a point with no undo — the commit is already made and only a human can land
or drop it. secret-rotate carries the same `SKIP=pytest` for a stronger reason: its rotation is
already live when it publishes, which is why its commit is `--no-verify` too.

Run: uv run pytest ansible/tests/setup/test_crons_gate_their_commit_on_the_suite.py
"""

import pytest
from _helpers import REPO

TEMPLATES = REPO / "ansible/roles/setup/initial_setup/templates"
# The crons whose commit the prek hook does not gate.
GATED_CRONS = ("docs-refresh.sh.j2", "eval-run.sh.j2")
# Every cron that reaches publish_pr.py, so every cron whose push meets the pre-push hook.
PUBLISHING_CRONS = (*GATED_CRONS, "secret-rotate.sh.j2")

SUITE_RUN = "python -m pytest"
SKIP_PYTEST = "SKIP=pytest"


def _lines(name: str) -> list[str]:
    return (TEMPLATES / name).read_text().splitlines()


@pytest.mark.parametrize("name", GATED_CRONS)
def test_the_cron_runs_the_suite_before_its_commit(name):
    lines = _lines(name)
    suite = [i for i, line in enumerate(lines) if SUITE_RUN in line and "#" not in line]
    commit = [i for i, line in enumerate(lines) if line.startswith("git commit ")]
    assert suite, (
        f"{name} never runs the suite; the prek hook stopped doing it for it (#2827)"
    )
    assert commit, f"{name} no longer commits the way this guard reads"
    assert min(suite) < min(commit), (
        f"{name} runs the suite at line {min(suite) + 1}, after its commit at "
        f"{min(commit) + 1}: a failure there cannot stop the publish"
    )


@pytest.mark.parametrize("name", GATED_CRONS)
def test_the_suite_failure_stops_the_cron(name):
    """A gate that only logs is not a gate. The run's exit code has to end the script."""
    text = (TEMPLATES / name).read_text()
    block = text[text.index(SUITE_RUN) : text.index(SUITE_RUN) + 1200]
    assert "SUITE_RC=$?" in block, f"{name} does not capture the suite's exit code"
    assert 'if [ "$SUITE_RC" -ne 0 ]; then' in block, f"{name} does not branch on it"
    assert "exit 1" in block, f"{name} does not exit on a failed suite"


@pytest.mark.parametrize("name", PUBLISHING_CRONS)
def test_the_publisher_skips_the_pre_push_hook(name):
    text = (TEMPLATES / name).read_text()
    publish = [line for line in text.splitlines() if "publish_pr.py publish" in line]
    assert len(publish) == 1, f"{name} has {len(publish)} publish_pr.py lines"
    assert SKIP_PYTEST in publish[0], (
        f"{name} publishes without {SKIP_PYTEST}, so prek's pre-push `pytest` hook runs the "
        "whole suite again on the push (#2827)"
    )


def test_no_cron_still_carries_the_retired_deselect():
    """The reject half. A leftover `PYTEST_ADDOPTS` that deselects the retired weights ratchet
    names a node id pytest cannot find."""
    stale = [
        name
        for name in PUBLISHING_CRONS
        if "PYTEST_ADDOPTS" in (TEMPLATES / name).read_text()
    ]
    assert not stale, f"{stale} still set PYTEST_ADDOPTS; the ratchet went with #2830"
