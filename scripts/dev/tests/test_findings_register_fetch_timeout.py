"""The register fetch carries its own timeout, because it outgrew `lib.gh`'s default.

`load_issues` asks gh for `body` and `comments` on every `claude` issue in the state it is
given. On 2026-09-28 `--state all` was 4.19 MB across about 900 issues and took 67.5s wall,
past `lib.gh.gh`'s 60s default — so every findings.py subcommand died with `gh failed: ...
timed out after 60.0 seconds`, `open` included. A session could neither file a finding nor
read the register, and the failure looked like a GitHub outage rather than a fetch that had
grown. `open` left this path in #2846 and searches for its one fingerprint instead
(`gh_calls.fingerprint_match`); the docs-refresh cron behind
`scripts/docs/reference/backlog.py` is what the override now covers.

The kwarg is the whole fix, so it is what these assert. `_findings_fakes` records argv only, so
these tests stub `gh_json` themselves to see the keyword.
"""

import inspect

from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.gh_calls import (
    REGISTER_FETCH_TIMEOUT,
    load_issues,
    open_pr_refs,
)
from lib.gh import gh as real_gh


def _recording_tools(answer):
    """FindingsTools whose `gh_json` records the kwargs it was handed."""
    seen: list[dict] = []

    def gh_json(*argv, **kwargs):
        seen.append(kwargs)
        return answer

    return FindingsTools(gh_json=gh_json), seen


def test_the_register_fetch_asks_for_more_than_the_gh_default():
    tools, seen = _recording_tools([])
    load_issues(tools=tools)
    assert seen == [{"timeout": REGISTER_FETCH_TIMEOUT}]


def test_the_gh_default_this_overrides_is_still_the_smaller_one():
    """Non-vacuity: if `lib.gh` ever raises its default past this, the override is dead weight."""
    default = inspect.signature(real_gh).parameters["timeout"].default
    assert default == 60.0, "lib.gh.gh's default timeout moved; recheck this override"
    assert REGISTER_FETCH_TIMEOUT > 60.0


def test_a_small_fixed_fetch_keeps_the_shared_default():
    """The rejecting half: only the register fetch gets the override, not every gh call.

    `open_pr_refs` reads 200 PR bodies, a fixed cost that has not grown. Handing every call a
    5-minute timeout would turn a genuinely hung `gh` into a 5-minute stall.
    """
    tools, seen = _recording_tools([])
    open_pr_refs(tools)
    assert seen == [{}]
