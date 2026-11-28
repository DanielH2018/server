"""Properties of the `ci_verdict` readers over generated GitHub JSON.

On any JSON value, built from the field names each reader reads, a reader returns or raises
`ValueError`, and every timestamp it returns is timezone-aware, so a reader may compare it with
another. The property tests run under the profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest hosts/lib/tests/host_lib/test_ci_verdict_parse_properties.py
"""

from collections.abc import Callable

from hypothesis import example, given
from hypothesis import strategies as st

from ci_verdict import CheckRun, CheckSuite, CommitStatus, WorkflowRun, parse_rollup

_KEYS = [
    "app",
    "attempt",
    "check_suite",
    "conclusion",
    "context",
    "createdAt",
    "created_at",
    "creator",
    "databaseId",
    "description",
    "detailsUrl",
    "full_name",
    "headBranch",
    "headSha",
    "head_branch",
    "head_commit",
    "head_sha",
    "html_url",
    "id",
    "latest_check_runs_count",
    "login",
    "name",
    "repository",
    "run_attempt",
    "run_started_at",
    "slug",
    "startedAt",
    "state",
    "status",
    "targetUrl",
    "target_url",
    "tree_id",
    "updated_at",
    "url",
]

_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda children: (
        st.lists(children) | st.dictionaries(st.sampled_from(_KEYS), children)
    ),
)

_PARSERS: list[Callable[[object], object]] = [
    CheckRun.parse,
    CheckRun.from_rollup,
    parse_rollup,
    CheckSuite.parse,
    CommitStatus.parse,
    WorkflowRun.parse,
]


@given(st.sampled_from(_PARSERS), _json)
def test_a_reader_given_any_json_returns_or_raises_value_error(parse, raw):
    try:
        parse(raw)
    except ValueError:
        pass


_stamp = (
    st.none()
    | st.text()
    | st.sampled_from(["2026-11-26T10:00:00Z", "2026-11-26T10:00:00", "2026-11-26"])
)


@given(_stamp, _stamp)
@example("2026-11-26T10:00:00", "2026-11-26")
def test_every_timestamp_a_reader_returns_is_timezone_aware(created, started):
    run = WorkflowRun.parse({"created_at": created, "run_started_at": started})
    status = CommitStatus.parse({"updated_at": created})

    stamps = [run.created_at, run.started_at, status.updated_at]
    assert all(stamp.tzinfo is not None for stamp in stamps)
