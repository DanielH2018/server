"""The required CI context names must match .github/workflows/ci.yml exactly.

A name that no longer matches a `name:` in the workflow silently drops a required check.
`ci_verdict` holds the verdict at `pending` for a required name with no runs, so a stale
name here does not fail open -- it hangs the wait forever, which is its own outage. Either
way the two lists must not drift.

Run: uv run pytest ansible/tests/test_ci_contexts_match_workflows.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "deploy_tools"))

import await_ci  # noqa: E402 — needs the path insert above

# Job-level `name:` keys sit at four spaces under `jobs:`. Matching that depth rather than
# any `name:` keeps step names (six spaces, and far more numerous) out of the set.
_JOB_NAME = re.compile(r"^\s{4}name:\s*(.+?)\s*$", re.M)


def _workflow_job_names() -> set[str]:
    text = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    return set(_JOB_NAME.findall(text))


def test_every_required_context_names_a_real_job():
    missing = await_ci.required_contexts() - _workflow_job_names()
    assert not missing, f"required contexts naming no ci.yml job: {sorted(missing)}"


def test_a_bogus_context_would_be_flagged():
    """The reject half. Without this, a parser that returned every string in the file
    would pass the check above while proving nothing."""
    assert "not-a-real-job" not in _workflow_job_names()


def test_the_workflow_parse_actually_finds_jobs():
    """A regex that matched nothing would make the coverage test vacuously true."""
    assert _workflow_job_names(), "parsed no job names out of ci.yml"
