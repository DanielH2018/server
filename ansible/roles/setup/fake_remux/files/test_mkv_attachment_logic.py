"""Tests for mkv_attachment_logic.

Every rule is a `..._is_clean` / `..._is_flagged` pair, per the repo's "a new check ships with a
proof it can go RED" rule: a guard that fires on everything and one that fires on nothing are
indistinguishable from the passing side alone. `verdict` gets the same treatment — the zero-scanned
DOWN is the arm that catches a sweep which stopped seeing files.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mkv_attachment_logic as mal


# --- is_safe: the exact class ffmpeg 7.1 accepts -------------------------------------------------


def test_plain_ascii_name_is_clean():
    assert mal.is_safe("NexaBold.otf")
    assert mal.plan(["NexaBold.otf"]) == []


def test_name_with_space_is_flagged():
    assert not mal.is_safe("Nexa Bold.otf")
    assert mal.plan(["Nexa Bold.otf"]) == [("Nexa Bold.otf", "Nexa_Bold.otf")]


def test_underscores_and_dashes_are_clean():
    assert mal.is_safe("Nexa-Bold_2.otf")
    assert mal.plan(["Nexa-Bold_2.otf"]) == []


def test_non_ascii_name_is_flagged():
    assert not mal.is_safe("ゴシック.ttf")
    ((old, new),) = mal.plan(["ゴシック.ttf"])
    assert old == "ゴシック.ttf"
    assert mal.is_safe(new)


def test_leading_digit_is_clean_but_leading_dot_is_flagged():
    assert mal.is_safe("2Bold.otf")
    assert not mal.is_safe(".hidden.otf")
    ((_, new),) = mal.plan([".hidden.otf"])
    assert new == "_hidden.otf"
    assert mal.is_safe(new)


def test_parens_and_hash_are_flagged():
    assert not mal.is_safe("Font (Bold)#1.ttf")
    ((_, new),) = mal.plan(["Font (Bold)#1.ttf"])
    assert new == "Font__Bold__1.ttf"


# --- sanitize: extension, collisions, degenerate stems -------------------------------------------


def test_extension_is_preserved():
    ((_, new),) = mal.plan(["My Font.OTF"])
    assert new.endswith(".OTF")


def test_name_without_extension_keeps_having_none():
    ((_, new),) = mal.plan(["My Font"])
    assert new == "My_Font"


def test_two_unsafe_names_collide_to_distinct_outputs():
    """`Nexa Bold.otf` and `Nexa+Bold.otf` both map to `Nexa_Bold.otf` — mkvpropedit selects the
    attachment to rewrite BY NAME, so a duplicate would make the second rewrite ambiguous."""
    renames = mal.plan(["Nexa Bold.otf", "Nexa+Bold.otf"])
    news = [new for _, new in renames]
    assert len(set(n.lower() for n in news)) == 2, news
    assert all(mal.is_safe(n) for n in news)


def test_rename_never_lands_on_an_existing_safe_name():
    renames = mal.plan(["Nexa_Bold.otf", "Nexa Bold.otf"])
    assert renames == [("Nexa Bold.otf", "Nexa_Bold_1.otf")]


def test_all_illegal_stem_becomes_underscores():
    ((_, new),) = mal.plan(["!!!.ttf"])
    assert mal.is_safe(new)
    assert new == "___.ttf"


def test_bare_extension_gets_a_stem():
    """The only input whose sanitized stem is empty — the `f` prefix exists for exactly this."""
    ((_, new),) = mal.plan([".otf"])
    assert new == "f.otf"
    assert mal.is_safe(new)


def test_extension_with_illegal_chars_is_sanitized():
    ((_, new),) = mal.plan(["font.t tf"])
    assert mal.is_safe(new)
    assert new == "font.t_tf"


# --- mkvpropedit_args: the option-before-selector ordering ---------------------------------------


def test_property_option_precedes_its_selector():
    args = mal.mkvpropedit_args([("a b.otf", "a_b.otf")])
    assert args == [
        "--attachment-name",
        "a_b.otf",
        "--update-attachment",
        "name:a b.otf",
    ]


def test_each_rename_gets_its_own_option_selector_pair():
    args = mal.mkvpropedit_args([("a b.otf", "a_b.otf"), ("c d.ttf", "c_d.ttf")])
    assert args.count("--update-attachment") == 2
    assert args.index("--attachment-name") < args.index("--update-attachment")


# --- verdict: the arm that catches a sweep which stopped seeing files -----------------------------


def test_clean_library_is_up_and_names_the_count():
    ok, msg = mal.verdict(61, 0, [])
    assert ok
    assert "61 scanned" in msg


def test_zero_scanned_is_down():
    ok, msg = mal.verdict(0, 0, [])
    assert not ok
    assert "no mkv files" in msg


def test_repairs_are_up_and_counted():
    ok, msg = mal.verdict(61, 21, [])
    assert ok
    assert "21 repaired" in msg


def test_a_failure_is_down_even_with_files_scanned():
    ok, msg = mal.verdict(61, 20, ["x.mkv: mkvpropedit rc=2"])
    assert not ok
    assert "FAILED" in msg
    assert "x.mkv" in msg
