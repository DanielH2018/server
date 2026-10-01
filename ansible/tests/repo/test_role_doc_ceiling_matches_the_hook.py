"""The role-doc character ceiling is the inject hook's payload budget, and stays equal to it.

`_doc_size.MAX_CHARS` is what the two role-doc guards fail over, and it means something only
because `.claude/hooks/inject-nested-docs.py:INLINE_MAX_CHARS` is the point where that hook
stops inlining a doc and injects its head instead. Two numbers in two trees drift, and the
drift is silent: the guard keeps passing docs the hook has started truncating.

Run: uv run pytest ansible/tests/repo/test_role_doc_ceiling_matches_the_hook.py
"""

import pytest
from _doc_size import INJECT_HOOK, MAX_CHARS, hook_inline_max_chars


def test_the_ceiling_equals_the_hooks_inline_budget():
    assert MAX_CHARS == hook_inline_max_chars(), (
        f"_doc_size.MAX_CHARS is {MAX_CHARS} and {INJECT_HOOK.name} inlines up to "
        f"{hook_inline_max_chars()} — move the ceiling to the hook's budget, or say in both "
        f"files why a role doc is held to a different number"
    )


def test_the_live_hooks_budget_is_the_number_this_repo_was_measured_against():
    """Non-vacuity, and a second failure the equality test above cannot give on its own.

    The pin passes when both numbers move together, which is the edit that changes what every
    session reads without anyone re-reading the measurement behind it. Spelling 7,500 out here
    means moving the budget fails until someone writes the new number down deliberately.
    """
    assert hook_inline_max_chars() == 7500


def test_a_hook_without_the_literal_is_flagged(tmp_path):
    hook = tmp_path / "inject-nested-docs.py"
    hook.write_text("BUDGET = 7500\n")
    with pytest.raises(AssertionError, match="no longer assigns INLINE_MAX_CHARS"):
        hook_inline_max_chars(hook)


def test_a_hook_with_the_literal_is_read(tmp_path):
    hook = tmp_path / "inject-nested-docs.py"
    hook.write_text('"""Doc."""\n\nINLINE_MAX_CHARS = 1234\nINLINE_MAX_LINES = 190\n')
    assert hook_inline_max_chars(hook) == 1234
