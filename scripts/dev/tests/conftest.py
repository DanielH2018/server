"""The two factories the three findings test modules share, as fixtures.

Both are factory fixtures rather than built values: a test names the issue it wants and the
boundaries it wants in the same line it asserts on. `make_tools` is spelled that way, not
`tools`, so `tools, calls = make_tools(...)` does not shadow the fixture it came from. The
factories themselves live in `_findings_fakes.py`, beside the rest of the doubles.

Run: uv run pytest scripts/dev/tests -k findings
"""

import pytest
from _findings_fakes import build_tools, make_issue


@pytest.fixture(autouse=True)
def _no_worktree_prefix(monkeypatch):
    """Run as the agent user, whose profile sets it, the fan-out tests read the operator's names."""
    monkeypatch.delenv("CLAUDE_WORKTREE_PREFIX", raising=False)


@pytest.fixture
def issue():
    """`issue(number, *, state=, labels=, fp=, comments=, created=, title=)` -> a gh issue."""
    return make_issue


@pytest.fixture
def make_tools():
    """`make_tools(Fakes(...))` -> `(FindingsTools, Calls)`; no argument means all defaults."""
    return build_tools
