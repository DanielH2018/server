"""The role-doc character ceiling is what the inject hook has left for the doc itself.

`_doc_size.MAX_CHARS` is what the two role-doc guards fail over, and it means something only
because `.claude/hooks/inject-nested-docs.py` stops inlining a doc past it and injects the
doc's head instead. The hook does not weigh a doc against `INLINE_MAX_CHARS`: `_fits_inline`
weighs the doc plus its `===== <doc> (applies to <trigger>) =====` header against
`INLINE_MAX_CHARS - len(_PREAMBLE)`. A ceiling pinned to the raw 7,500 therefore passed twelve
role docs the hook had already started truncating (#3245).

So the tests here do not restate the arithmetic. They build a doc of exactly `MAX_CHARS` and
ask the hook's own `_fits_inline` whether it is inlined, then ask the same question of every
real role doc.

Run: uv run pytest ansible/tests/repo/test_role_doc_ceiling_matches_the_hook.py
"""

import pytest
from _doc_size import (
    HEADER_ALLOWANCE,
    INJECT_HOOK,
    MAX_CHARS,
    effective_ceiling,
    hook_inline_budget,
    hook_inline_max_chars,
    load_inject_hook,
)
from _helpers import REPO, run
from lib.repo_paths import REPO as _REPO_ROOT

# Empty: every role doc in the tree is expected to arrive whole. It held
# `ansible/roles/k8s/manifests/CLAUDE.md` while #3245 left that doc out of scope, and #3246
# split it, so the exception is a named escape hatch with nothing in it rather than a list.
OVER_BUDGET_BY_DESIGN: set[str] = set()


@pytest.fixture(scope="module")
def hook():
    return load_inject_hook()


def _tracked() -> list[str]:
    listed = run(["git", "ls-files", "-z"], cwd=REPO, check=True).stdout
    return [p for p in listed.split("\0") if p]


def _role_docs_with_longest_trigger() -> list[tuple[str, str]]:
    """Every role CLAUDE.md paired with the longest tracked path that selects it.

    The trigger is whichever path in the Bash command made the hook load the doc, so the
    longest path under the role is this tree's worst case for that doc's header.
    """
    tracked = _tracked()
    docs = sorted(
        p
        for p in tracked
        if p.startswith("ansible/roles/") and p.endswith("/CLAUDE.md")
    )
    assert "ansible/roles/k8s/pihole/CLAUDE.md" in docs, (
        "the role-doc census found no pihole doc — `git ls-files` returned something other "
        "than this repo's role tree, and every assertion below would pass vacuously"
    )
    pairs = []
    for doc in docs:
        role = doc.rsplit("/", 1)[0]
        under = [p for p in tracked if p.startswith(role + "/")]
        pairs.append((doc, max(under, key=len)))
    return pairs


def _filler(chars: int) -> str:
    """`chars` characters over few lines, so only the char half of `_fits_inline` decides."""
    body = "# Role\n\nfiller words that stand in for prose "
    return body + "x" * (chars - len(body))


def test_a_doc_at_the_ceiling_is_inlined_whole(hook):
    """The ceiling's whole promise: a doc at it arrives whole, header and all.

    No trailing newline, and a header padded to exactly `HEADER_ALLOWANCE`: the worst shape
    `MAX_CHARS` has to cover, since `_fits_inline` rstrips the doc before measuring it.
    """
    text = _filler(MAX_CHARS)
    header = "=" * HEADER_ALLOWANCE
    assert len(text) == MAX_CHARS and not text.endswith("\n")
    assert hook._fits_inline(text, header)


def test_a_doc_one_char_over_the_ceiling_is_not_inlined(hook):
    """The red case, and what makes the ceiling tight rather than merely safe."""
    text = _filler(MAX_CHARS + 1)
    header = "=" * HEADER_ALLOWANCE
    assert not hook._fits_inline(text, header)


def test_the_header_allowance_covers_every_role_docs_longest_trigger(hook):
    """`HEADER_ALLOWANCE` is a reserve, so something has to fail when the tree outgrows it."""
    too_long = {
        doc: len(hook._header(doc, trigger))
        for doc, trigger in _role_docs_with_longest_trigger()
        if len(hook._header(doc, trigger)) > HEADER_ALLOWANCE
    }
    assert not too_long, (
        f"_doc_size.HEADER_ALLOWANCE reserves {HEADER_ALLOWANCE} chars and these docs' "
        f"longest trigger needs more: {too_long} — raise the allowance (every role doc's "
        f"ceiling drops by the same amount) or shorten the path"
    )


def test_every_role_doc_the_guards_cover_is_inlined_whole(hook):
    """The guards' ceiling is only worth having if the hook agrees doc by doc.

    `MAX_CHARS` reserves the worst-case header, so this passes by a margin for a doc whose own
    paths are short. It fails for a doc over the ceiling whatever the k8s and setup guards'
    `OVER_CEILING` lists say, which is the point: those lists waive the guard, not the hook.
    """
    truncated = [
        doc
        for doc, trigger in _role_docs_with_longest_trigger()
        if doc not in OVER_BUDGET_BY_DESIGN
        and not hook._fits_inline(
            (_REPO_ROOT / doc).read_text(), hook._header(doc, trigger)
        )
    ]
    assert not truncated, (
        f"the inject hook injects these role docs as a head, not whole: {truncated} — move "
        f"history and measurements to the role's docs/ page, or add the doc to "
        f"OVER_BUDGET_BY_DESIGN with the issue tracking it"
    )


def test_the_live_hooks_budget_is_the_numbers_this_repo_was_measured_against():
    """Non-vacuity, and a second failure the derivation cannot give on its own.

    The derivation passes when the hook's budget moves, which is the edit that changes what
    every session reads without anyone re-reading the measurement behind it. Spelling all three
    numbers out here means moving the budget fails until someone writes the new ones down.
    """
    assert hook_inline_max_chars() == 7500
    assert hook_inline_budget() == 7272
    assert MAX_CHARS == effective_ceiling() == 7070


def test_a_hook_without_the_literal_is_flagged(tmp_path):
    hook = tmp_path / "inject-nested-docs.py"
    hook.write_text("BUDGET = 7500\n")
    with pytest.raises(AssertionError, match="no longer assigns INLINE_MAX_CHARS"):
        hook_inline_max_chars(hook)


def test_a_hook_with_the_literal_is_read(tmp_path):
    hook = tmp_path / "inject-nested-docs.py"
    hook.write_text('"""Doc."""\n\nINLINE_MAX_CHARS = 1234\nINLINE_MAX_LINES = 190\n')
    assert hook_inline_max_chars(hook) == 1234


def test_the_hook_this_ceiling_reads_is_the_one_the_harness_runs():
    assert INJECT_HOOK.exists()
