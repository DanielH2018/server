"""Properties of the `pr_json` readers over generated GitHub JSON.

On arbitrary JSON built from the field names each reader reads, a reader returns or raises
`ValueError` or `KeyError`, never a `TypeError` or `AttributeError` that would escape the
landing as a traceback. A well-typed answer parses to a value that parses back to itself.
The property tests run under the profile `tests/hypothesis_profiles.py` loads.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_pr_json_properties.py
"""

import json
from collections.abc import Callable

from hypothesis import given
from hypothesis import strategies as st

from deploy_tools.land_lib.pr_json import PrView, parse_file, parse_review, parse_view

_NESTED_KEYS = ["login", "oid", "path", "changeType", "messageHeadline", "messageBody"]
_FILE_KEYS = ["filename", "previous_filename", "status", "sha"]
_REVIEW_KEYS = ["user", "state", "submitted_at", "commit_id"]

_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda children: (
        st.lists(children) | st.dictionaries(st.sampled_from(_NESTED_KEYS), children)
    ),
)


def _objects(keys: list[str]) -> st.SearchStrategy[dict]:
    return st.dictionaries(st.sampled_from(keys), _json)


def _refuses_cleanly(parse: Callable[[dict], object], raw: dict) -> None:
    try:
        parse(raw)
    except ValueError, KeyError:
        pass


# `url` is a field no reader declares, so a draw holding it exercises parse_view's KeyError.
@given(_objects([*sorted(PrView.__optional_keys__), "url"]))
def test_a_view_of_any_shape_parses_or_raises_value_or_key_error(raw):
    _refuses_cleanly(parse_view, raw)


@given(_objects(_FILE_KEYS))
def test_a_file_entry_of_any_shape_parses_or_raises_value_error(raw):
    _refuses_cleanly(parse_file, raw)


@given(_objects(_REVIEW_KEYS))
def test_a_review_of_any_shape_parses_or_raises_value_error(raw):
    _refuses_cleanly(parse_review, raw)


_text = st.none() | st.text()
_login = st.none() | st.fixed_dictionaries(
    {"login": _text}, optional={"is_bot": st.booleans()}
)
_view = st.fixed_dictionaries(
    {},
    optional={
        "author": _login,
        "baseRefName": _text,
        "body": _text,
        "changedFiles": st.integers(),
        "commits": st.lists(
            st.fixed_dictionaries(
                {"oid": st.text()},
                optional={"messageHeadline": _text, "messageBody": _text},
            )
        ),
        "files": st.lists(
            st.fixed_dictionaries(
                {"path": _text},
                optional={"changeType": _text, "additions": st.integers()},
            )
        ),
        "headRefName": _text,
        "headRefOid": _text,
        "isCrossRepository": st.booleans(),
        "isDraft": st.booleans(),
        "mergeCommit": st.none() | st.fixed_dictionaries({}, optional={"oid": _text}),
        "mergeStateStatus": _text,
        "mergeable": _text,
        "reviewDecision": _text,
        "state": _text,
        "title": _text,
    },
)
_file = st.fixed_dictionaries(
    {"filename": _text, "status": st.text(min_size=1)},
    optional={"previous_filename": _text, "sha": _text, "patch": st.text()},
)
_review = st.fixed_dictionaries(
    {},
    optional={
        "user": _login,
        "state": _text,
        "submitted_at": _text,
        "commit_id": _text,
    },
)


def _reparsed(parse: Callable[[dict], object], raw: dict) -> tuple[object, object]:
    parsed = parse(raw)
    return parsed, parse(json.loads(json.dumps(parsed)))


@given(_view)
def test_a_parsed_view_parses_back_to_itself(raw):
    parsed, again = _reparsed(parse_view, raw)
    assert again == parsed


@given(_file)
def test_a_parsed_file_entry_parses_back_to_itself(raw):
    parsed, again = _reparsed(parse_file, raw)
    assert again == parsed


@given(_review)
def test_a_parsed_review_parses_back_to_itself(raw):
    parsed, again = _reparsed(parse_review, raw)
    assert again == parsed
