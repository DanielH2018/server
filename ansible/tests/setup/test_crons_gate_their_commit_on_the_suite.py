"""The two crons that publish a commit run the suite themselves, before the commit.

No prek hook runs the suite, so `git commit` and `git push` gate nothing and each cron runs it
on its own line instead. Losing that silently is the failure this module exists to catch —
both crons would keep publishing generated pages or a sweep, and only CI would say the suite
was red, after the pages were already live.

Run: uv run pytest ansible/tests/setup/test_crons_gate_their_commit_on_the_suite.py
"""

import pytest
from _helpers import REPO

TEMPLATES = REPO / "ansible/roles/setup/initial_setup/templates"
# The crons that must gate their own commit on the suite.
GATED_CRONS = ("docs-refresh.sh.j2", "eval-run.sh.j2")
# Every cron that reaches publish_pr.py.
PUBLISHING_CRONS = (*GATED_CRONS, "secret-rotate.sh.j2")

SUITE_RUN = "python -m pytest"


def _lines(name: str) -> list[str]:
    return (TEMPLATES / name).read_text().splitlines()


@pytest.mark.parametrize("name", GATED_CRONS)
def test_the_cron_runs_the_suite_before_its_commit(name):
    lines = _lines(name)
    suite = [i for i, line in enumerate(lines) if SUITE_RUN in line and "#" not in line]
    commit = [i for i, line in enumerate(lines) if line.startswith("git commit ")]
    assert suite, (
        f"{name} never runs the suite, and no prek hook runs it for it (#2827)"
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


def test_no_cron_still_carries_the_retired_deselect():
    """The reject half. A leftover `PYTEST_ADDOPTS` that deselects the retired weights ratchet
    names a node id pytest cannot find."""
    stale = [
        name
        for name in PUBLISHING_CRONS
        if "PYTEST_ADDOPTS" in (TEMPLATES / name).read_text()
    ]
    assert not stale, f"{stale} still set PYTEST_ADDOPTS; the ratchet went with #2830"
