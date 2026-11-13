"""The red halves of promote.yml's starter gate, kept apart for the module-length cap.

`test_promote_workflow.py`'s `problems` holds the check, and its green test covers the real
file. Each case here breaks the gate one way and must be named.
"""

import copy

import pytest
from test_promote_workflow import (
    FLUX_DEFAULTS,
    REFUSE_JOB,
    WORKFLOW,
    _load,
    problems,
)


def _the_bot_on_the_starter_list(wf):
    for job in wf["jobs"].values():
        job["if"] = job["if"].replace(
            '"github-actions[bot]"]', '"github-actions[bot]", "DanielClaudeBot"]'
        )


def _refusal_for_dispatches_only(wf):
    wf["jobs"][REFUSE_JOB]["if"] = "github.event_name == 'workflow_dispatch'"


RED = {
    "the bot on the starter list": (
        _the_bot_on_the_starter_list,
        "render: its `if` lacks the starter guard",
    ),
    "a refusal that is not the guard's negation": (
        _refusal_for_dispatches_only,
        "refuse-starter: its `if` is not the negated starter guard",
    ),
}


@pytest.mark.parametrize("case", RED, ids=list(RED))
def test_each_broken_starter_gate_is_named(case):
    workflow = _load(WORKFLOW)
    broken = copy.deepcopy(workflow)
    mutate, expected = RED[case]
    mutate(broken)
    assert broken != workflow, f"{case}: the mutation left the workflow unchanged"
    found = problems(broken, _load(FLUX_DEFAULTS)["flux_version"])
    assert any(expected in line for line in found), found
