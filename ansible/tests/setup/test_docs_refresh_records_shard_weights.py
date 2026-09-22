"""docs-refresh records a weight for every new test module, unattended — #2274.

CI's measured gate (`pytest_shard.py --check-durations`) rejects the PR that introduces an
unweighted module costing `RUNNER_HEAVY_SECONDS` or more and hands the repair to a human. It
says nothing about a lighter one, and an unweighted file is packed at the suite median of 0.0s
— so the split stays un-skewed rather than measured. The twice-daily cron closes that: it holds
the git-tree lock already and already publishes through a PR.

Three things have to hold together, and each has a way of failing that reads green:

- The record runs BEFORE `git diff --cached --quiet`. That gate exits the script when the
  generated trees did not move, which is most runs, so a record after it never publishes.
- The weights table is in the `git add`. Written and left unstaged, it is a modified file in
  the primary checkout, which `deploy_git.py` reads as dirty and which parks every deploy.
- `restore_generated_tree`'s per-tree check names it too. That check is what decides between
  `tree restored` and `TREE STILL DIRTY` after a failed commit, and it is path-scoped: scoped
  to the two doc trees it would report restored over a weights table left modified.

Every path here is derived from `pytest_shard` rather than spelled, so renaming the table or
the script breaks this test instead of silently leaving the cron pointing at nothing.

Run: uv run pytest ansible/tests/setup/test_docs_refresh_records_shard_weights.py
"""

import pytest
import pytest_shard

from _helpers import REPO

TEMPLATE = REPO / "ansible/roles/setup/initial_setup/templates/docs-refresh.sh.j2"
WEIGHTS = pytest_shard.WEIGHTS_PATH.relative_to(REPO).as_posix()
SHARD_SCRIPT = (REPO / "scripts/dev/pytest_shard.py").relative_to(REPO).as_posix()
PUBLISH_GATE = "if git diff --cached --quiet; then"


def _code_lines(text: str) -> list[str]:
    """The template's executable lines — comments cite these paths too, and do not run."""
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def _record_before_gate(text: str) -> str | None:
    """The complaint about a script's record/publish ordering, or None when it is right.

    A function rather than inline asserts, so the rejecting half below can hand it a reordered
    script and watch the same rule go red — the ordering is the one claim here that a later
    edit could invert while every other assertion still passed.
    """
    lines = _code_lines(text)
    record = [i for i, line in enumerate(lines) if "--record-missing" in line]
    gate = [i for i, line in enumerate(lines) if line.startswith(PUBLISH_GATE)]
    if len(record) != 1:
        return f"expected exactly one --record-missing invocation, found {len(record)}"
    if len(gate) != 1:
        return f"expected exactly one {PUBLISH_GATE!r}, found {len(gate)}"
    if record[0] > gate[0]:
        return (
            "the weights are recorded after the publish gate, so a weights-only change never "
            "reaches a PR"
        )
    return None


def test_the_cron_records_the_weights_it_is_missing():
    lines = _code_lines(TEMPLATE.read_text())
    invocations = [
        line for line in lines if SHARD_SCRIPT in line or "--record-missing" in line
    ]
    assert invocations, (
        f"{TEMPLATE.name} never runs {SHARD_SCRIPT}, so a new test module is weighted only "
        "when a human runs --record-missing by hand"
    )
    joined = " ".join(invocations)
    assert "--record-missing" in joined, (
        f"{TEMPLATE.name} runs {SHARD_SCRIPT} without --record-missing; a full --record "
        "re-measures every weight and churns the table twice a day"
    )
    assert "--record " not in joined and not joined.endswith("--record"), (
        f"{TEMPLATE.name} runs a full --record: {joined}"
    )


def test_the_record_runs_before_the_publish_gate():
    """CLEAN half: the real template orders the two the only way that publishes."""
    assert _record_before_gate(TEMPLATE.read_text()) is None


def test_a_record_after_the_publish_gate_is_flagged():
    """FLAGGED half: the same rule on a script that records too late."""
    reordered = "\n".join(
        [
            "git add docs/reference",
            PUBLISH_GATE,
            "  exit 0",
            "fi",
            "uv run python scripts/dev/pytest_shard.py --record-missing",
        ]
    )
    assert "after the publish gate" in (_record_before_gate(reordered) or "")


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("git add ", id="staged-for-the-commit"),
        pytest.param("git status --porcelain -- ", id="checked-by-the-restore"),
    ],
)
def test_the_weights_table_is_named_wherever_the_doc_trees_are(marker):
    """Both places name docs/reference; a weights table missing from either parks the deployer.

    The `git add` is the issue's own first bullet. The porcelain check is one layer under it:
    the per-path restore already covers the table, and this is the check that says the restore
    worked — scoped to the two doc trees it reports `tree restored` over leftover dirt.
    """
    text = TEMPLATE.read_text()
    lines = _code_lines(text)
    hits = [i for i, line in enumerate(lines) if marker in line]
    assert hits, f"{TEMPLATE.name} no longer contains a {marker!r} line"
    # A continued line carries its remaining paths onto the next one.
    for start in hits:
        span = " ".join(lines[start : start + 2])
        assert "docs/reference" in span, f"{marker!r} at line {start} names no doc tree"
        assert WEIGHTS in span, (
            f"{TEMPLATE.name}: the {marker!r} covering docs/reference does not name "
            f"{WEIGHTS}, so the table the cron writes is left loose in the primary checkout"
        )
