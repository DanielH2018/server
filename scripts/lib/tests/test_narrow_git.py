"""The primitives both narrowing derivations read through, and the one class they share."""

import pytest

from lib.narrow_git import (
    CannotNarrow,
    changed_mapping_keys,
    mapping_at,
    same_parsed_yaml,
)


def test_a_comment_or_layout_edit_parses_the_same():
    assert same_parsed_yaml("a: [1, 2]\n", "# Why.\na:\n  - 1\n  - 2\n")


@pytest.mark.parametrize(
    "after",
    [
        "serial: true\n",
        "serial: 1.0\n",
        "serial: '1'\n",
        "serial: {\n",
        "d: 2026-13-45\n",
    ],
    ids=["bool", "float", "string", "unparseable", "bad-timestamp"],
)
def test_a_value_python_calls_equal_or_an_unloadable_text_differs(after: str):
    assert not same_parsed_yaml("serial: 1\n", after)


def test_an_empty_file_is_an_empty_mapping():
    """A vars file emptied by a range defines no key, which is an answer rather than a doubt."""
    assert mapping_at("", "defaults/main.yml") == {}


def test_a_file_that_does_not_parse_refuses():
    with pytest.raises(CannotNarrow, match="does not parse"):
        mapping_at("---\nbad: [unclosed\n", "defaults/main.yml")


def test_a_timestamp_shaped_value_that_is_no_date_refuses():
    with pytest.raises(CannotNarrow, match="does not parse"):
        mapping_at("d: 2026-13-45\n", "defaults/main.yml")


def test_a_file_that_is_not_a_mapping_refuses():
    with pytest.raises(CannotNarrow, match="is not a mapping of keys"):
        mapping_at("---\n- a\n- list\n", "defaults/main.yml")


@pytest.mark.parametrize("after", [True, 1.0], ids=["bool", "float"])
def test_a_value_python_calls_equal_counts_as_changed(after):
    assert changed_mapping_keys({"a": 1}, {"a": after}, "main.yml") == {"a"}


def test_a_removed_key_counts_as_changed():
    """The tasks reading it change behaviour, so dropping it is not "nothing changed"."""
    assert changed_mapping_keys({"a": 1, "b": 2}, {"a": 1}, "main.yml") == {"b"}


def test_a_recursive_alias_refuses_rather_than_blowing_the_stack():
    text = "k: &l [1, *l]\n"
    with pytest.raises(CannotNarrow, match="recursive alias"):
        changed_mapping_keys(
            mapping_at(text, "main.yml"),
            mapping_at(text + "m: 1\n", "main.yml"),
            "main.yml",
        )


def test_both_narrowing_modules_raise_the_same_class():
    """The drift guarded: two `CannotNarrow` classes no single `except` could catch.

    `shared_role_reach` imports the setup half and `probe_lib/releases` catches the broad
    half, so a raise crossing that boundary had to match whichever class the catch site
    happened to import.
    """
    from deploy_tools.narrow_lib import broad as narrow_broad
    import narrow_setup

    assert narrow_broad.CannotNarrow is CannotNarrow
    assert narrow_setup.CannotNarrow is CannotNarrow
