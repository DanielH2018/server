"""docs-refresh refreshes the shard-weights table and the test-dependency table, unattended.

CI's measured check (`pytest_shard.py --check-durations`) only annotates a wrong weight, and a
module the table lacks is packed at its directory's median, so nothing but this cron writes the
table: `--refresh-runner` reads it off CI's own logs, and the cron already publishes through a
PR.

Two things have to hold together, and each has a way of failing that reads green:

- The refresh runs BEFORE `git diff --cached --quiet`. That gate exits the script when the
  generated trees did not move, which is most runs, so a refresh after it never publishes.
- The table is in the `git add`. Written and left unstaged, a table never publishes, and the
  cron reports the stray write as a degraded run.

The cron also records what each test module depends on (`affected_tests.py record`),
into a second table that a pull request's CI run selects its test modules from. The same
two things hold for it, and it needs `strace`, which the component has to install.

Every path here is derived from `pytest_shard` rather than spelled, so renaming the table or
the script breaks this test instead of silently leaving the cron pointing at nothing.

Run: uv run pytest hosts/tests/test_docs_refresh_records_shard_weights.py
"""

import re

import pytest
from dev import pytest_shard
from dev.affected_lib import record as affected_record
from dev.affected_lib import table as affected_table

from _helpers import REPO
from lib import yaml_fast
from _shell_render import rendered_shell_text, shell_template_path
from lib.proc_testing import fake_bin, run

TEMPLATE = shell_template_path("components", "docs-refresh", "docs-refresh.sh.j2")
SCRIPT = rendered_shell_text("components", "docs-refresh", "docs-refresh.sh.j2")
RUNNER_WEIGHTS = pytest_shard.RUNNER_WEIGHTS_PATH.relative_to(REPO).as_posix()
AFFECTED = affected_table.TABLE_PATH.relative_to(REPO).as_posix()
AFFECTED_SCRIPT = "scripts/dev/affected_tests.py"
PUBLISH_GATE = "if git diff --cached --quiet; then"


def _code_lines(text: str) -> list[str]:
    """The template's executable lines — comments cite these paths too, and do not run."""
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def _record_before_gate(text: str, flag: str = "--refresh-runner") -> str | None:
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


@pytest.mark.parametrize("flag", ["--refresh-runner", "--slices"])
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
            "uv run python scripts/dev/pytest_shard.py --refresh-runner 3",
        ]
    )
    assert "after the publish gate" in (_record_before_gate(reordered) or "")


@pytest.mark.parametrize(
    "table",
    [
        pytest.param(RUNNER_WEIGHTS, id="runner"),
        pytest.param(AFFECTED, id="affected"),
    ],
)
def test_the_weights_table_is_staged_with_the_doc_trees(table):
    """The `git add` that names docs/reference names the table; one left out never publishes."""
    marker = "git add "
    lines = _code_lines(SCRIPT)
    hits = [i for i, line in enumerate(lines) if marker in line]
    assert hits, f"{TEMPLATE.name} no longer contains a {marker!r} line"
    # A continued line carries its remaining paths onto the next two.
    for start in hits:
        span = " ".join(lines[start : start + 3])
        assert "docs/reference" in span, f"{marker!r} at line {start} names no doc tree"
        assert table in span, (
            f"{TEMPLATE.name}: the {marker!r} covering docs/reference does not name "
            f"{table}, so the table the cron writes never publishes"
        )


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("RUNNER_MSG", id="refresh-runner"),
        pytest.param("AFFECTED_MSG", id="record-dependencies"),
    ],
)
def test_a_successful_weight_step_reaches_the_crons_output(tmp_path, message):
    """A successful step's report is the only record of what it moved.

    Runs the rendered step with a stub `uv` that exits 0. The step ends at its closing
    unindented `fi`, and `alert` and `logger` count as output.
    """
    step = re.search(rf"^{message}=.*?^fi$", SCRIPT, re.M | re.S)
    assert step, f"{TEMPLATE.name} lost the {message} step"
    moved = "scripts/x/tests/test_moved.py"
    stubs = fake_bin(
        tmp_path / "bin",
        uv=f"echo 'wrote 1 row: {moved}'\n",
        logger='echo "$@"\n',
    )

    result = run(
        [
            "bash",
            "-c",
            f'UV=uv\nGENERATORS_OK=1\nalert() {{ echo "$*"; }}\n{step.group(0)}',
        ],
        cwd=tmp_path,
        stub_bin=stubs,
    )

    assert moved in result.stdout + result.stderr


def test_the_cron_records_test_dependencies_inside_a_budget_its_timeout_outlasts():
    """The step holds the git-tree lock, so it must end by itself before `timeout` kills it.

    A killed run writes no table. The budget stops new modules, and one module already
    running may take `MODULE_TIMEOUT_SECONDS` more.
    """
    step = re.search(r"^AFFECTED_MSG=.*?^AFFECTED_RC=", SCRIPT, re.M | re.S)
    assert step, f"{TEMPLATE.name} lost the AFFECTED_MSG step"
    command = " ".join(step.group(0).replace("\\\n", " ").split())
    assert f"{AFFECTED_SCRIPT} record" in command and "--slices" in command, command
    limits = re.search(r"timeout (\d+) .* --budget (\d+)", command)
    assert limits, command
    timeout, budget = map(int, limits.groups())
    assert timeout >= budget + affected_record.MODULE_TIMEOUT_SECONDS


def test_the_component_installs_the_tracer_the_recorder_runs():
    component = yaml_fast.safe_load((TEMPLATE.parents[1] / "component.yml").read_text())

    assert "strace" in component["depends"]
