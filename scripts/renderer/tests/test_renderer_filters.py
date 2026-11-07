"""Each reimplemented filter against ansible-core's, the parity oracle until H7 removes it.

The inputs are those on which a naive implementation parts from ansible-core's: `bool` over
the string `false`, a stable `to_uuid`, `hash` over a non-string, `from_yaml` over YAML 1.1
scalars. After H7, ADR-0028's literals in `_dialect_cases.py` hold the cluster subset.

Run: uv run pytest scripts/renderer/tests/test_renderer_filters.py
"""

import datetime
import hashlib
import json

import pytest
from ansible.plugins.filter.core import FilterModule as CoreFilters
from ansible.plugins.filter.core import (
    b64encode,
    combine,
    comment,
    from_json,
    from_yaml,
    get_hash,
    mandatory,
    regex_replace,
    to_bool,
    to_uuid,
)
from ansible.plugins.test.core import search

from lib.ansible_jinja_env import ansible_bool, make_ansible_env
from renderer.renderer_lib import jinja_filters as filters

CORE = CoreFilters().filters()

# Each case: (our filter, ansible-core's, args, kwargs).
PARITY = [
    *(
        (filters.b64encode, b64encode, args, kwargs)
        for args, kwargs in (
            (("héllo",), {}),
            ((b"raw",), {}),
            ((12,), {}),
            (("ÿ?>",), {"urlsafe": True}),
            (("héllo",), {"encoding": "utf-16"}),
        )
    ),
    *(
        (fn, core, (path,), {})
        for path in ("/a/b.txt", "/a/b/", "b", "", "/")
        for fn, core in (
            (filters.basename, CORE["basename"]),
            (filters.dirname, CORE["dirname"]),
        )
    ),
    *(
        (filters.combine, combine, args, {})
        for args in (
            (),
            ({"a": 1},),
            ({"a": 1, "b": 1}, {"b": 2, "c": 2}),
            ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
            ({"a": 1}, {"b": 1}, {"a": 2}),
            ([{"a": 1}, {"a": 2}],),
            ([{"a": 1}], {"a": 2}),
            ({"a": 1}, None, "null", "None"),
            ({"a": {"x": 1}}, {"a": {"y": 2}}),
        )
    ),
    *(
        (filters.comment, comment, (text,), {})
        for text in ("one", "two\nlines", "gap\n\nline", "trailing\n", "", "\n")
    ),
    *(
        (filters.from_json, from_json, (text,), {})
        for text in ('{"a": [1, null, true]}', "1", '"s"', b'{"a": 1}')
    ),
    *(
        (filters.from_yaml, from_yaml, (text,), {})
        for text in (
            "a: [1, 2]",
            "a: yes\nb: on\nc: off\nd: 'yes'",
            "a: 010\nb: 0o10\nc: 1e3\nd: 1.5",
            "a: 2026-11-07\nb: 2026-11-07T01:02:03Z",
            "a: ~\nb: null",
            "a: 1\na: 2",
            "base: &b {x: 1}\nmerged:\n  <<: *b\n  y: 2",
            "",
            "- one\n- two",
            None,
            {"already": "parsed"},
            7,
        )
    ),
    *(
        (filters.hash_text, get_hash, args, {})
        for args in (
            ("text",),
            ("text", "sha256"),
            ("héllo", "sha256"),
            (b"raw", "sha1"),
            (12, "sha256"),
            ("text", "md5"),
        )
    ),
    *(
        (filters.mandatory, mandatory, args, {})
        for args in (("set",), (0,), (None,), ("", "the message"))
    ),
    *(
        (filters.regex_replace, regex_replace, args, kwargs)
        for args, kwargs in (
            (("*/15", r"^[*]/([0-9]+)$", r"\1"), {}),
            (("5min", r"^([0-9]+)min$", r"\1"), {}),
            ((123, "2", "x"), {}),
            (("aAa", "a", "x"), {"ignorecase": True}),
            (("a\nb", "^b", "x"), {}),
            (("a\nb", "^b", "x"), {"multiline": True}),
            (("aaa", "a", "x"), {"count": 2}),
            (("aaa", "a", "x"), {"mandatory_count": 3}),
        )
    ),
    *(
        (filters.search, search, args, kwargs)
        for args, kwargs in (
            (("2400:cb00::/32", ":"), {}),
            (("172.64.0.0/13", ":"), {}),
            (("Abc", "abc"), {}),
            (("Abc", "abc"), {"ignorecase": True}),
            (("a\nb", "^b"), {}),
            (("a\nb", "^b"), {"multiline": True}),
            ((12, "2"), {}),
        )
    ),
    *(
        (filters.to_uuid, to_uuid, args, {})
        for args in (
            ("name",),
            (12,),
            ("name", "6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        )
    ),
]


def _case_id(case) -> str:
    fn, _, args, kwargs = case
    return f"{fn.__name__}-{args!r}-{kwargs!r}"


@pytest.mark.parametrize("case", PARITY, ids=_case_id)
def test_a_filter_agrees_with_ansible_core(case):
    ours, theirs, args, kwargs = case
    expected, got = theirs(*args, **kwargs), ours(*args, **kwargs)
    assert got == expected
    # dict equality ignores order, and `to_json` writes a mapping in its own order.
    assert json.dumps(got, default=str) == json.dumps(expected, default=str)


@pytest.mark.parametrize(
    "value",
    ["false", "False", "0", "off", "no", "true", "yes", "maybe", True, False, 1, 0, 2],
)
def test_bool_agrees_with_ansible_core(value):
    assert ansible_bool(value) is to_bool(value)


def test_bool_parity_reads_red_for_python_bool():
    """The inputs tell the filter from Python's `bool`, which reads `false` as True."""
    assert to_bool("false") is not bool("false")


def test_to_uuid_is_stable_and_uses_the_fixed_namespace():
    assert filters.to_uuid("name") == filters.to_uuid("name")
    assert filters.to_uuid("name") != filters.to_uuid("other")
    assert filters.to_uuid("name") == to_uuid("name")


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
    env = make_ansible_env()
    undefined = env.undefined(name="absent")
    assert filters.mandatory(undefined, "the message") is undefined
    assert mandatory(undefined, "the message") is undefined


def test_every_name_the_registration_names_is_reimplemented():
    """No registered filter or test resolves into ansible-core."""
    env = make_ansible_env()
    from_ansible = {
        name: fn.__module__
        for name, fn in [*env.filters.items(), *env.tests.items()]
        if getattr(fn, "__module__", "").startswith("ansible.")
    }
    assert not from_ansible
    assert env.filters["hash"] is filters.hash_text
    assert env.tests["search"] is filters.search
