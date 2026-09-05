"""The ci.yml pytest job must check out full history, because a ratchet test compares against it.

`test_no_allowlist_entry_rose_against_origin_master` in `test_module_length_ratchet.py` skips
when `origin/master` is unresolvable. On the runner it resolves only because the `pytest` job
checks out with `fetch-depth: 0`: at the pinned actions/checkout SHA that fetches
`+refs/heads/*:refs/remotes/origin/*`.

Change that line to `fetch-depth: 1`, or drop it, and the anti-raise half of the ratchet becomes
a skip with CI still green -- the repo's own non-vacuity failure class, one workflow file away
from the test it disarms. This guard asserts the workflow shape instead of failing inside the
ratchet on `CI`, because an env-gated failure there would hardcode an assumption about the
environment into a test that has none.

Run: uv run pytest ansible/tests/repo/test_ci_pytest_job_fetch_depth.py
"""

from __future__ import annotations

from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# The job whose checkout the ratchet depends on, and the test file that depends on it.
JOB = "pytest"
RATCHET = REPO / "ansible" / "tests" / "repo" / "test_module_length_ratchet.py"


def checkout_fetch_depths(workflow_text: str, job_id: str) -> list[object]:
    """Every actions/checkout step's `fetch-depth` in one job, `None` where it is unset."""
    steps = yaml_fast.safe_load(workflow_text)["jobs"][job_id].get("steps") or []
    return [
        (step.get("with") or {}).get("fetch-depth")
        for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]


def fetches_full_history(workflow_text: str, job_id: str) -> bool:
    depths = checkout_fetch_depths(workflow_text, job_id)
    return bool(depths) and all(depth == 0 for depth in depths)


def test_the_pytest_job_checks_out_full_history():
    assert fetches_full_history(CI_WORKFLOW.read_text(), JOB), (
        "the ci.yml `pytest` job no longer checks out with fetch-depth: 0, so "
        "test_no_allowlist_entry_rose_against_origin_master silently skips on the runner"
    )


def test_the_step_parse_finds_a_checkout_to_read():
    """Non-vacuity. A checkout step this parser missed would read as an empty list, which
    `all(...)` accepts."""
    assert checkout_fetch_depths(CI_WORKFLOW.read_text(), JOB) == [0]


def test_the_ratchet_that_depends_on_this_still_reads_origin_master():
    """The named consumer. If the ratchet stops comparing against `origin/master`, re-examine
    whether this guard still has a subject rather than leaving it pinning a workflow line nobody
    depends on."""
    assert "origin/master" in RATCHET.read_text()


_FULL = """
jobs:
  pytest:
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          fetch-depth: 0
      - run: uv run python -m pytest
"""

_SHALLOW = """
jobs:
  pytest:
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          fetch-depth: 1
      - run: uv run python -m pytest
"""

_UNSET = """
jobs:
  pytest:
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
      - run: uv run python -m pytest
"""


def test_a_full_history_checkout_is_clean():
    assert fetches_full_history(_FULL, JOB)


def test_a_shallow_checkout_is_flagged():
    assert not fetches_full_history(_SHALLOW, JOB)


def test_a_checkout_with_no_fetch_depth_is_flagged():
    """The default is a depth-1 clone, so an omitted line is the same hazard as `fetch-depth: 1`."""
    assert not fetches_full_history(_UNSET, JOB)
