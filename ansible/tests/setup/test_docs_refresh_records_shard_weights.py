"""docs-refresh records a weight for every new test module, unattended.

CI's measured gate (`pytest_shard.py --check-durations`) rejects the PR that introduces an
unweighted module costing `RUNNER_HEAVY_FAIL_SECONDS` or more and hands the repair to a human.
One costing `RUNNER_HEAVY_SECONDS` up to that bound only annotates the run. It says nothing
about a lighter one, and an unweighted file is packed at the suite median of 0.0s
— so the split stays un-skewed rather than measured. The twice-daily cron closes that: it holds
the git-tree lock already and already publishes through a PR.

Three things have to hold together, and each has a way of failing that reads green:

- The record runs BEFORE `git diff --cached --quiet`. That gate exits the script when the
  generated trees did not move, which is most runs, so a record after it never publishes.
- Both weights tables are in the `git add`: the recorded one, and the runner-seconds one that
  `--refresh-runner` rewrites from CI's logs (#6178). Written and left unstaged, a table is a
  modified file in the primary checkout, which `deploy_git.py` reads as dirty and which parks
  every deploy.
- `restore_generated_tree`'s per-tree check names them too. That check is what decides between
  `tree restored` and `TREE STILL DIRTY` after a failed commit, and it is path-scoped: scoped
  to the two doc trees it would report restored over a weights table left modified.

The runner refresh runs before the gate for the same reason the record does.

Every path here is derived from `pytest_shard` rather than spelled, so renaming the table or
the script breaks this test instead of silently leaving the cron pointing at nothing.

Run: uv run pytest ansible/tests/setup/test_docs_refresh_records_shard_weights.py
"""

import re

import pytest
import pytest_shard

from _helpers import REPO
from _shell_render import rendered_shell_text, shell_template_path

TEMPLATE = shell_template_path("components", "docs-refresh", "docs-refresh.sh.j2")
SCRIPT = rendered_shell_text("components", "docs-refresh", "docs-refresh.sh.j2")
WEIGHTS = pytest_shard.WEIGHTS_PATH.relative_to(REPO).as_posix()
RUNNER_WEIGHTS = pytest_shard.RUNNER_WEIGHTS_PATH.relative_to(REPO).as_posix()
SHARD_SCRIPT = (REPO / "scripts/dev/pytest_shard.py").relative_to(REPO).as_posix()
PUBLISH_GATE = "if git diff --cached --quiet; then"


def _code_lines(text: str) -> list[str]:
    """The template's executable lines — comments cite these paths too, and do not run."""
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def _record_before_gate(text: str, flag: str = "--record-missing") -> str | None:
    """The complaint about where a script runs `flag` against its publish gate, or None.

    `flag` is a mode that writes a table the gate's `git add` stages.

    A function rather than inline asserts, so the rejecting half below can hand it a reordered
    script and watch the same rule go red — the ordering is the one claim here that a later
    edit could invert while every other assertion still passed.
    """
    lines = _code_lines(text)
    record = [i for i, line in enumerate(lines) if flag in line]
    gate = [i for i, line in enumerate(lines) if line.startswith(PUBLISH_GATE)]
    if len(record) != 1:
        return f"expected exactly one {flag} invocation, found {len(record)}"
    if len(gate) != 1:
        return f"expected exactly one {PUBLISH_GATE!r}, found {len(gate)}"
    if record[0] > gate[0]:
        return (
            "the weights are recorded after the publish gate, so a weights-only change never "
            "reaches a PR"
        )
    return None


def test_the_cron_records_the_weights_it_is_missing():
    lines = _code_lines(SCRIPT)
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


@pytest.mark.parametrize("flag", ["--record-missing", "--refresh-runner"])
def test_the_record_runs_before_the_publish_gate(flag):
    """CLEAN half: the real template orders each write the only way that publishes."""
    assert _record_before_gate(SCRIPT, flag) is None


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
    "table",
    [pytest.param(WEIGHTS, id="recorded"), pytest.param(RUNNER_WEIGHTS, id="runner")],
)
@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("git add ", id="staged-for-the-commit"),
        pytest.param("git status --porcelain -- ", id="checked-by-the-restore"),
    ],
)
def test_the_weights_table_is_named_wherever_the_doc_trees_are(marker, table):
    """Both places name docs/reference; a weights table missing from either parks the deployer.

    The `git add` is the issue's own first bullet. The porcelain check is one layer under it:
    the per-path restore already covers the table, and this is the check that says the restore
    worked — scoped to the two doc trees it reports `tree restored` over leftover dirt.
    """
    lines = _code_lines(SCRIPT)
    hits = [i for i, line in enumerate(lines) if marker in line]
    assert hits, f"{TEMPLATE.name} no longer contains a {marker!r} line"
    # A continued line carries its remaining paths onto the next one.
    for start in hits:
        span = " ".join(lines[start : start + 2])
        assert "docs/reference" in span, f"{marker!r} at line {start} names no doc tree"
        assert table in span, (
            f"{TEMPLATE.name}: the {marker!r} covering docs/reference does not name "
            f"{table}, so the table the cron writes is left loose in the primary checkout"
        )


# The modes that read the table or fill its gaps, and so leave a present entry as it is. The
# two runner modes write the other table, so they refresh nothing in this one either.
_NON_REFRESHING_FLAGS = frozenset(
    {
        "--record-missing",
        "--refresh-runner",
        "--record-runner",
        "--of",
        "--shard",
        "--out",
        "--summary",
        "--check-durations",
        "--warn-only",
        "--threshold",
        "--fail-above",
        "--stale-ratio",
    }
)


def _shard_script_arguments(text: str) -> list[str]:
    """What follows `python <pytest_shard.py>` in each command, continuation lines joined.

    Only the text after the script counts: `uv run --frozen` is uv's flag, and the publish
    step's PR body names the script in prose without running it.
    """
    runs = re.compile(rf"python\s+{re.escape(SHARD_SCRIPT)}(.*)")
    arguments, pending = [], ""
    for line in _code_lines(text):
        pending += line.rstrip().removesuffix("\\") + " "
        if not line.rstrip().endswith("\\"):
            arguments += [m.group(1) for m in runs.finditer(pending)]
            pending = ""
    return arguments


def _flags(text: str, arguments: str) -> set[str]:
    """The flags in `arguments`, and in every assignment to a variable it expands.

    `ci.yml` passes its mode as `"${mode[@]}"`, so a cron that picks its mode the same way
    names no flag after the script itself.
    """
    flags = set(re.findall(r"--[a-z][a-z-]*", arguments))
    for name in re.findall(r"\$\{?(\w+)", arguments):
        assigned = re.compile(rf"(?:^|[\s;&|(]){name}\+?=(.*)")
        for line in _code_lines(text):
            if match := assigned.search(line):
                flags |= set(re.findall(r"--[a-z][a-z-]*", match.group(1)))
    return flags


def test_the_cron_also_refreshes_weights_that_are_present():
    """#4888: `--record-missing` never touches a present entry, so a module that grew 1.5-2x
    past its weight keeps the old number. The cron is the one place that measures serially on
    the recording host, which is where drift that small is distinguishable from runner scale.

    Any flag outside the gap-filling and read-only set satisfies this, so it proves the cron
    runs a refreshing mode, not that the mode runs often enough or on a quiet host.
    """
    arguments = _shard_script_arguments(SCRIPT)
    assert arguments, f"{TEMPLATE.name} never runs {SHARD_SCRIPT}"
    flags = set().union(*(_flags(SCRIPT, args) for args in arguments))
    refreshing = flags - _NON_REFRESHING_FLAGS
    assert refreshing, (
        f"{TEMPLATE.name} runs {SHARD_SCRIPT} only with {sorted(flags)}, none of which "
        "re-measures an entry the table already holds. The mode must appear after the "
        "script on its command line, or in an assignment to a variable that line expands"
    )
