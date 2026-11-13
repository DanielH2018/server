"""Each reimplemented filter against the value ansible-core returned for the same call.

Each expected value was taken from ansible-core 2.22's own filter before H7 removed it. The
inputs are those on which a naive implementation parts from ansible-core's: `bool` over the
string `false`, a stable `to_uuid`, `hash` over a non-string, `from_yaml` over YAML 1.1
scalars. ADR-0028's literals in `_dialect_cases.py` hold the cluster subset as templates.

Run: uv run pytest scripts/renderer/tests/test_renderer_filters.py
"""

import datetime
import hashlib
import json

import pytest

from lib.ansible_jinja_env import make_env, register_ansible_filters
from renderer.renderer_lib import jinja_filters as filters
from renderer.renderer_lib.jinja_filters import (
    b64encode,
    basename,
    combine,
    comment,
    dirname,
    from_json,
    from_yaml,
    hash_text,
    mandatory,
    regex_replace,
    search,
    to_uuid,
)

# Each case: (our filter, args, kwargs, ansible-core's result).
CASES = [
    (b64encode, ("héllo",), {}, "aMOpbGxv"),
    (b64encode, (b"raw",), {}, "cmF3"),
    (b64encode, (12,), {}, "MTI="),
    (b64encode, ("ÿ?>",), {"urlsafe": True}, "w78_Pg=="),
    (b64encode, ("héllo",), {"encoding": "utf-16"}, "//5oAOkAbABsAG8A"),
    (basename, ("/a/b.txt",), {}, "b.txt"),
    (dirname, ("/a/b.txt",), {}, "/a"),
    (basename, ("/a/b/",), {}, ""),
    (dirname, ("/a/b/",), {}, "/a/b"),
    (basename, ("b",), {}, "b"),
    (dirname, ("b",), {}, ""),
    (basename, ("",), {}, ""),
    (dirname, ("",), {}, ""),
    (basename, ("/",), {}, ""),
    (dirname, ("/",), {}, "/"),
    (combine, (), {}, {}),
    (combine, ({"a": 1},), {}, {"a": 1}),
    (combine, ({"a": 1, "b": 1}, {"b": 2, "c": 2}), {}, {"a": 1, "b": 2, "c": 2}),
    (combine, ({"a": 1, "b": 2}, {"b": 2, "a": 1}), {}, {"b": 2, "a": 1}),
    (combine, ({"a": 1}, {"b": 1}, {"a": 2}), {}, {"a": 2, "b": 1}),
    (combine, ([{"a": 1}, {"a": 2}],), {}, {"a": 2}),
    (combine, ([{"a": 1}], {"a": 2}), {}, {"a": 2}),
    (combine, ({"a": 1}, None, "null", "None"), {}, {"a": 1}),
    (combine, ({"a": {"x": 1}}, {"a": {"y": 2}}), {}, {"a": {"y": 2}}),
    (comment, ("one",), {}, "#\n# one\n#"),
    (comment, ("two\nlines",), {}, "#\n# two\n# lines\n#"),
    (comment, ("gap\n\nline",), {}, "#\n# gap\n#\n# line\n#"),
    (comment, ("trailing\n",), {}, "#\n# trailing\n# \n#"),
    (comment, ("",), {}, "#\n# \n#"),
    (comment, ("\n",), {}, "#\n#\n# \n#"),
    (from_json, ('{"a": [1, null, true]}',), {}, {"a": [1, None, True]}),
    (from_json, ("1",), {}, 1),
    (from_json, ('"s"',), {}, "s"),
    (from_json, (b'{"a": 1}',), {}, {"a": 1}),
    (from_yaml, ("a: [1, 2]",), {}, {"a": [1, 2]}),
    (
        from_yaml,
        ("a: yes\nb: on\nc: off\nd: 'yes'",),
        {},
        {"a": True, "b": True, "c": False, "d": "yes"},
    ),
    (
        from_yaml,
        ("a: 010\nb: 0o10\nc: 1e3\nd: 1.5",),
        {},
        {"a": 8, "b": "0o10", "c": "1e3", "d": 1.5},
    ),
    (
        from_yaml,
        ("a: 2026-11-07\nb: 2026-11-07T01:02:03Z",),
        {},
        {
            "a": datetime.date(2026, 11, 7),
            "b": datetime.datetime(2026, 11, 7, 1, 2, 3, tzinfo=datetime.timezone.utc),
        },
    ),
    (from_yaml, ("a: ~\nb: null",), {}, {"a": None, "b": None}),
    (from_yaml, ("a: 1\na: 2",), {}, {"a": 2}),
    (
        from_yaml,
        ("base: &b {x: 1}\nmerged:\n  <<: *b\n  y: 2",),
        {},
        {"base": {"x": 1}, "merged": {"x": 1, "y": 2}},
    ),
    (from_yaml, ("",), {}, None),
    (from_yaml, ("- one\n- two",), {}, ["one", "two"]),
    (from_yaml, (None,), {}, None),
    (from_yaml, ({"already": "parsed"},), {}, {"already": "parsed"}),
    (from_yaml, (7,), {}, 7),
    (hash_text, ("text",), {}, "372ea08cab33e71c02c651dbc83a474d32c676ea"),
    (
        hash_text,
        ("text", "sha256"),
        {},
        "982d9e3eb996f559e633f4d194def3761d909f5a3b647d1a851fead67c32c9d1",
    ),
    (
        hash_text,
        ("héllo", "sha256"),
        {},
        "3c48591d8d098a4538f5e013dfcf406e948eac4d3277b10bf614e295d6068179",
    ),
    (hash_text, (b"raw", "sha1"), {}, "ce15802a8c5e8e9db0ffaf10130ef265296e9ea4"),
    (
        hash_text,
        (12, "sha256"),
        {},
        "6b51d431df5d7f141cbececcf79edf3dd861c3b4069f0b11661a3eefacbba918",
    ),
    (hash_text, ("text", "md5"), {}, "1cb251ec0d568de6a929b520c4aed8d1"),
    (mandatory, ("set",), {}, "set"),
    (mandatory, (0,), {}, 0),
    (mandatory, (None,), {}, None),
    (mandatory, ("", "the message"), {}, ""),
    (regex_replace, ("*/15", "^[*]/([0-9]+)$", "\\1"), {}, "15"),
    (regex_replace, ("5min", "^([0-9]+)min$", "\\1"), {}, "5"),
    (regex_replace, (123, "2", "x"), {}, "1x3"),
    (regex_replace, ("aAa", "a", "x"), {"ignorecase": True}, "xxx"),
    (regex_replace, ("a\nb", "^b", "x"), {}, "a\nb"),
    (regex_replace, ("a\nb", "^b", "x"), {"multiline": True}, "a\nx"),
    (regex_replace, ("aaa", "a", "x"), {"count": 2}, "xxa"),
    (regex_replace, ("aaa", "a", "x"), {"mandatory_count": 3}, "xxx"),
    (search, ("2400:cb00::/32", ":"), {}, True),
    (search, ("172.64.0.0/13", ":"), {}, False),
    (search, ("Abc", "abc"), {}, False),
    (search, ("Abc", "abc"), {"ignorecase": True}, True),
    (search, ("a\nb", "^b"), {}, False),
    (search, ("a\nb", "^b"), {"multiline": True}, True),
    (search, (12, "2"), {}, True),
    (to_uuid, ("name",), {}, "dcc154cb-0b43-56da-8cf6-d7d0d70f4e94"),
    (to_uuid, (12,), {}, "41c8891d-b370-568a-9e37-9c075fa71707"),
    (
        to_uuid,
        ("name", "6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        {},
        "9b8edca0-90f2-5031-8e5d-3f708834696c",
    ),
]


def _case_id(case) -> str:
    fn, args, kwargs, _ = case
    return f"{fn.__name__}-{args!r}-{kwargs!r}"


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_a_filter_returns_what_ansible_core_returned(case):
    fn, args, kwargs, expected = case
    got = fn(*args, **kwargs)
    assert got == expected
    # dict equality ignores order, and `to_json` writes a mapping in its own order.
    assert json.dumps(got, default=str) == json.dumps(expected, default=str)


def test_to_uuid_is_stable_and_uses_the_fixed_namespace():
    assert filters.to_uuid("name") == filters.to_uuid("name")
    assert filters.to_uuid("name") != filters.to_uuid("other")


def test_hash_matches_hashlib():
    assert filters.hash_text("text", "sha256") == hashlib.sha256(b"text").hexdigest()


def test_from_yaml_keeps_yaml_1_1_scalars():
    parsed = filters.from_yaml("a: yes\nb: 010\nc: 2026-11-07")
    assert parsed == {"a": True, "b": 8, "c": datetime.date(2026, 11, 7)}


@pytest.mark.parametrize(
    "call",
    [
        lambda: filters.from_yaml("a: [1"),
        lambda: filters.combine({"a": 1}, "text"),
        lambda: filters.combine({"a": 1}, {"a": 2}, recursive=True),
        lambda: filters.comment("text", "xml"),
        lambda: filters.regex_replace("aaa", "a", "x", mandatory_count=2),
        lambda: filters.to_json(object()),
    ],
    ids=["from_yaml", "combine-term", "combine-option", "comment", "regex", "to_json"],
)
def test_a_refusal_is_a_value_error_the_renderer_catches_is_flagged(call):
    with pytest.raises(filters.FilterRefused):
        call()


def test_to_json_of_plain_data_is_clean():
    assert filters.to_json({"b": [1, None], "a": "é"}, sort_keys=True) == (
        '{"a": "\\u00e9", "b": [1, null]}'
    )


def test_mandatory_passes_an_undefined_through_to_the_guard():
    env = register_ansible_filters(make_env())
    undefined = env.undefined(name="absent")
    assert filters.mandatory(undefined, "the message") is undefined


def test_every_name_the_registration_names_is_reimplemented():
    """No registered filter or test resolves into ansible-core."""
    env = register_ansible_filters(make_env())
    from_ansible = {
        name: fn.__module__
        for name, fn in [*env.filters.items(), *env.tests.items()]
        if getattr(fn, "__module__", "").startswith("ansible.")
    }
    assert not from_ansible
    assert env.filters["hash"] is filters.hash_text
    assert env.tests["search"] is filters.search
