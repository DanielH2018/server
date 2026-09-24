"""A dispatched CI run is a FULL sweep, and it cannot evict a master push.

WHY THIS IS A TEST. `workflow_dispatch` is on ci.yml for one job: sweeping every hook against a
branch whose own diff scopes them all out. A runner-image bump is the case — #2259 moves every
`runs-on` to `ubuntu-26.04`, a workflow-only diff matches no hook's `files` regex, and the PR run
is green without having run `validate-unit-templates` or gitleaks on that image at all (#2366).

The trigger buys that only while every job reads a dispatch as a push. Both scoping conditions
key on `pull_request` today, so a dispatch falls through to the full-sweep arm. Rewriting one as
`github.event_name == 'push'` would leave a dispatch fast, green and checking nothing, and
nothing in the file would look wrong — which is why the property is asserted here rather than
only described in the trigger's comment.

Sibling to `test_ci_master_runs_are_not_evictable.py`, which owns the push arm of the same
concurrency group. This module owns the dispatch arm.

Run: uv run pytest ansible/tests/repo/test_ci_dispatch_sweeps_everything.py
"""

import re

from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# The census names its members. A step rename would otherwise drop the step out of the check
# without failing anything, and `all()` over an empty set passes.
SWEEP_STEPS = frozenset({"Run hooks (full sweep)", "Lint the whole tree"})
SCOPED_STEPS = frozenset(
    {
        "Collect this PR's changed files",
        "Run hooks (PR — scoped to changed files)",
        "Collect this PR's changed ansible files",
        "Lint the changed ansible files",
    }
)
KNOWN_JOBS = frozenset({"hooks", "ansible_lint", "pytest", "prek", "renovate-config"})

_EVENT_TERM = re.compile(r"github\.event_name\s*(==|!=)\s*'([a-z_]+)'")


def _workflow() -> dict:
    """ci.yml as a mapping. Its `on:` key parses as the boolean `True` under YAML 1.1."""
    return yaml_fast.safe_load(CI_WORKFLOW.read_text())


def truthy(condition: str, event: str) -> bool:
    """Evaluate the `github.event_name` subset of a GitHub expression for `event`.

    ci.yml's conditions are `github.event_name <op> '<literal>'` terms joined by `||` and `&&`,
    sometimes beside a step-output term. Any other term counts as false, which is what a step
    output actually is on a dispatch: the step that writes `steps.changed.outputs.mode` runs only
    on a pull request, so the output is unset and every comparison against it fails.
    """

    def term(text: str) -> bool:
        match = _EVENT_TERM.search(text)
        if not match:
            return False
        operator, literal = match.groups()
        return event == literal if operator == "==" else event != literal

    return any(
        all(term(part) for part in arm.split("&&")) for arm in condition.split("||")
    )


def _step_conditions() -> dict[str, str]:
    """Every named step in ci.yml that carries an `if:`, keyed by step name."""
    conditions = {}
    for job in _workflow()["jobs"].values():
        for step in job.get("steps", []):
            if "name" in step and "if" in step:
                conditions[step["name"]] = str(step["if"])
    return conditions


def _job_runs_on_dispatch(condition: str | None) -> bool:
    """Whether a job with this job-level `if:` runs on a dispatch.

    `always()` is the `prek` gate: it runs whatever its dependencies did, by design.
    """
    if condition is None:
        return True
    if "always()" in condition:
        return True
    return truthy(condition, "workflow_dispatch")


def test_the_workflow_declares_the_dispatch_trigger():
    triggers = _workflow()[True]
    assert triggers, "parsed no triggers out of ci.yml's `on:` mapping"
    assert "workflow_dispatch" in triggers, (
        "ci.yml carries no workflow_dispatch trigger, so the only full sweep on a change the "
        "hooks' `files` regexes cannot see is the push run after the merge (#2366)"
    )


def test_a_dispatch_runs_every_job():
    jobs = _workflow()["jobs"]
    assert KNOWN_JOBS <= jobs.keys(), (
        f"job census lost {sorted(KNOWN_JOBS - jobs.keys())}; the parse no longer reaches them, "
        f"so nothing below is checking them"
    )
    skipped = [
        name for name, job in jobs.items() if not _job_runs_on_dispatch(job.get("if"))
    ]
    assert not skipped, (
        f"jobs {skipped} do not run on a workflow_dispatch — a dispatched sweep that skips a job "
        f"reports green for the half it ran"
    )


def test_a_dispatch_takes_the_full_sweep_and_not_the_scoped_path():
    conditions = _step_conditions()
    census = SWEEP_STEPS | SCOPED_STEPS
    assert census <= conditions.keys(), (
        f"step census lost {sorted(census - conditions.keys())}; a renamed step is unchecked "
        f"here, so rename the entry above with it"
    )
    not_sweeping = [
        n for n in sorted(SWEEP_STEPS) if not truthy(conditions[n], "workflow_dispatch")
    ]
    assert not not_sweeping, (
        f"steps {not_sweeping} do not fire on a workflow_dispatch — the dispatch exists to run "
        f"them, and a run that skips them is a green that checked nothing (#2366)"
    )
    scoped = [
        n for n in sorted(SCOPED_STEPS) if truthy(conditions[n], "workflow_dispatch")
    ]
    assert not scoped, (
        f"steps {scoped} fire on a workflow_dispatch — the scoped path diffs against "
        f"`github.base_ref`, which is empty off a pull request"
    )


def test_the_evaluator_reads_a_pull_request_the_other_way():
    """Non-vacuity for the assertion above.

    An evaluator that returned false for everything would pass the "no scoped step fires"
    half while proving nothing. On a pull request those same conditions must hold.
    """
    conditions = _step_conditions()
    assert truthy(conditions["Collect this PR's changed files"], "pull_request")
    assert truthy(conditions["Collect this PR's changed ansible files"], "pull_request")


def test_a_sweep_condition_keyed_on_push_is_flagged():
    """The reject half: the shape that would silently break the dispatch."""
    assert truthy("github.event_name != 'pull_request'", "workflow_dispatch")
    assert not truthy("github.event_name == 'push'", "workflow_dispatch")
    assert not truthy(
        "github.event_name == 'push' || steps.changed.outputs.mode == 'full'",
        "workflow_dispatch",
    )


def dispatch_keyed_uniquely(group: str) -> bool:
    """Whether a concurrency group puts a dispatch in a group of its own."""
    return "workflow_dispatch" in group and "github.run_id" in group


def test_a_dispatch_cannot_join_a_push_concurrency_group():
    group = str(_workflow()["concurrency"]["group"])
    assert dispatch_keyed_uniquely(group), (
        f"concurrency.group is {group!r}, so a dispatch falls through to the `github.sha` arm and "
        f"joins the push group of the SHA it ran on. GitHub keeps one pending run per group, so "
        f"the dispatch can evict that SHA's queued push run and leave it with no verdict."
    )


def test_a_group_without_a_dispatch_arm_is_flagged():
    """The reject half — the value this arm replaced on 2026-09-24."""
    assert not dispatch_keyed_uniquely(
        "${{ github.workflow }}-${{ github.event_name == 'pull_request' "
        "&& github.ref || github.sha }}"
    )
