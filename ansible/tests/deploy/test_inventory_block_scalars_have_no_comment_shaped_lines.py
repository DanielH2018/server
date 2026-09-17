"""No inventory block scalar carries a continuation line that starts with `#`.

`narrow_broad._defines_only` decides whether an inventory change only defines new keys by
skipping every whole-line comment. That rule is wrong for one shape: inside a block scalar
(`key: |` or `key: >`) a continuation line that starts with `#` is CONTENT Ansible reads,
not a comment. A key consumed only on such a line would be skipped in the unsafe direction:
the narrowing deploys fewer services than the change reaches instead of refusing.

`_defines_only` relies on no such line existing (#1848). This guard makes that a fact the
suite checks rather than one the docstring asserts. The scan is a function over text, so
the reject half of the pair below runs on a synthetic scalar — the tree holds none, which
is the point.

Run: uv run pytest ansible/tests/deploy/test_inventory_block_scalars_have_no_comment_shaped_lines.py
"""

import re

import pytest

from _helpers import INVENTORY

# `key: |`, `- |`, `key: >-`, `key: |2+` … with an optional trailing comment. The
# indicator is what starts a block; the indentation of the line that carries it is what
# ends one.
_BLOCK_START = re.compile(r"^(\s*)(?:[^#\s][^#]*?:|-)\s*[|>][0-9+-]*\s*(?:#.*)?$")

# `_sort_hits` exempts a `_`-prefixed inventory file on both sides: no host loads it, so
# `_defines_only` never reads it and its scalars cannot mislead the narrowing.
MUST_SCAN = frozenset(
    {
        "group_vars/all.yml",
        "host_vars/daniel-box.yml",
        "host_vars/daniel-server.yml",
        "host_vars/daniel-pi.yml",
        "host_vars/daniel-stage.yml",
    }
)


def comment_shaped_continuations(text: str) -> list[int]:
    """1-based line numbers of block-scalar continuation lines whose first character is `#`.

    A blank line inside a block does not end it; a non-blank line indented no deeper than
    the line carrying the indicator does.
    """
    hits = []
    block_indent = None
    for n, line in enumerate(text.splitlines(), 1):
        if block_indent is not None:
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if indent > block_indent:
                if line.lstrip().startswith("#"):
                    hits.append(n)
                continue
            block_indent = None
        m = _BLOCK_START.match(line)
        if m:
            block_indent = len(m.group(1))
    return hits


def inventory_files() -> dict[str, str]:
    return {
        str(p.relative_to(INVENTORY)): p.read_text()
        for p in sorted(INVENTORY.rglob("*.y*ml"))
        if not p.name.startswith("_")
    }


CLEAN = """\
ssh_keys: |
  ssh-ed25519 AAAA one
  ssh-ed25519 BBBB two
# a real comment after the block
banner: >-
  hello

  world
after: 1
"""

FLAGGED = """\
pihole_lists: |
  https://example.invalid/a
  # not a comment: Ansible hands this line to the consumer
  https://example.invalid/b
after: 1
"""


def test_scan_accepts_blocks_with_no_hash_continuation_is_clean():
    assert comment_shaped_continuations(CLEAN) == []


def test_scan_flags_the_hash_continuation_line_is_flagged():
    assert comment_shaped_continuations(FLAGGED) == [3]


def test_scan_ends_a_block_at_the_dedent_is_clean():
    """A `#` at the block's own indent or shallower is a comment again, not content."""
    text = "k: |\n  content\n# comment\n  # deeper but the block ended above\n"
    assert comment_shaped_continuations(text) == []


@pytest.mark.parametrize("modifier", ["|", "|-", ">", ">+", "|2"])
def test_scan_reads_every_indicator_form_is_flagged(modifier):
    assert comment_shaped_continuations(f"k: {modifier}\n  # x\n") == [2]


def test_scan_reads_a_list_item_block_is_flagged():
    assert comment_shaped_continuations("items:\n  - |\n    # x\n") == [3]


def test_census_names_the_files_defines_only_reads():
    found = inventory_files()
    missing = MUST_SCAN - found.keys()
    assert not missing, (
        f"inventory files the guard must scan are gone: {sorted(missing)}"
    )
    assert not [f for f in found if f.split("/")[-1].startswith("_")]


@pytest.mark.parametrize("rel", sorted(inventory_files()))
def test_no_inventory_block_scalar_has_a_hash_continuation(rel):
    hits = comment_shaped_continuations(inventory_files()[rel])
    assert not hits, (
        f"ansible/inventory/{rel} lines {hits}: a block-scalar line starting with `#` is "
        "content, and narrow_broad._defines_only skips it as a comment. Move the text "
        "out of the block scalar, or teach _defines_only to parse."
    )
