"""The two factories the three findings test modules share, as fixtures.

Both are factory fixtures rather than built values: a test names the issue it wants and the
boundaries it wants in the same line it asserts on. `make_tools` is spelled that way, not
`tools`, so `tools, calls = make_tools(...)` does not shadow the fixture it came from. The
factories themselves live in `_findings_fakes.py`, beside the rest of the doubles.

Run: uv run pytest scripts/dev/tests -k findings
"""

import pytest
from _findings_fakes import build_tools, make_issue


@pytest.fixture
def issue():
    """`issue(number, *, state=, labels=, fp=, comments=, created=, title=)` -> a gh issue."""
    return make_issue


@pytest.fixture
def make_tools():
    """`make_tools(Fakes(...))` -> `(FindingsTools, Calls)`; no argument means all defaults."""
    return build_tools
