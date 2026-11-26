"""Every `# fact: <doc>#<heading>` comment names a section that exists.

The lint reads a `# fact:` line only inside a test some doc already cites, to check the doc's
side of the pair. A comment whose doc was deleted or whose heading was renamed is cited by
nothing, so the lint never reads it, and it sends the next reader to a section that is gone.

Only real comments count, read with `tokenize`: a `# fact:` line inside a string literal is a
fixture, such as the ones in `scripts/lib/tests/facts/test_facts_atoms.py`.
"""

import io
import tokenize
from collections.abc import Set
from pathlib import Path

from _helpers import REPO
from lib.facts.atoms import _BACKREF
from lib.facts.citations import sections, tracked_files

TRACKED = tracked_files(REPO)
PYTHON = sorted(p for p in TRACKED if p.endswith(".py"))


def fact_comments(source: str) -> list[tuple[int, str]]:
    """The line number and unit key of each `# fact:` comment token in `source`."""
    found = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT and (m := _BACKREF.match(tok.string)):
            found.append((tok.start[0], m.group(1)))
    return found


def dangling_fact_comments(
    root: Path, paths: list[str], tracked: Set[str]
) -> list[str]:
    """Each `# fact:` comment under `root` whose doc is untracked or has no such heading."""
    keys: dict[str, set[str]] = {}
    dangling = []
    for rel in paths:
        for line, unit in fact_comments((root / rel).read_text()):
            doc = unit.partition("#")[0]
            if doc not in tracked:
                dangling.append(f"{rel}:{line}: {doc} is not a tracked file")
                continue
            if doc not in keys:
                keys[doc] = {s.key for s in sections(doc, (root / doc).read_text())}
            if unit not in keys[doc]:
                dangling.append(f"{rel}:{line}: {doc} has no section '{unit}'")
    return dangling


def test_a_fact_comment_at_a_renamed_heading_or_a_deleted_doc_is_flagged(tmp_path):
    (tmp_path / "doc.md").write_text("# Title\n\n## Kept\n\ntext\n")
    (tmp_path / "test_x.py").write_text(
        "def test_a():\n"
        "    # fact: doc.md#Renamed\n"
        "    # fact: gone.md#Kept\n"
        "    assert True\n"
    )
    assert dangling_fact_comments(tmp_path, ["test_x.py"], {"doc.md", "test_x.py"}) == [
        "test_x.py:2: doc.md has no section 'doc.md#Renamed'",
        "test_x.py:3: gone.md is not a tracked file",
    ]


def test_a_fact_comment_at_a_real_heading_or_inside_a_string_is_clean(tmp_path):
    (tmp_path / "doc.md").write_text("# Title\n\n## Kept ##\n\ntext\n")
    (tmp_path / "test_x.py").write_text(
        "FIXTURE = '''\n"
        "    # fact: gone.md#Anything\n"
        "'''\n"
        "def test_a():\n"
        "    # fact: doc.md#Kept\n"
        "    assert True\n"
    )
    assert (
        dangling_fact_comments(tmp_path, ["test_x.py"], {"doc.md", "test_x.py"}) == []
    )


def test_the_walk_finds_fact_comments_to_check():
    """A shrunken walk is a bug: 367 comments stood in the tree on 2026-11-15."""
    found = sum(len(fact_comments((REPO / p).read_text())) for p in PYTHON)
    assert found >= 300, found


def test_every_fact_comment_names_a_section_that_exists():
    # fact: .claude/rules/facts.md#Sections and the citation grammar
    dangling = dangling_fact_comments(REPO, PYTHON, TRACKED)
    assert not dangling, (
        "a `# fact:` comment names a section that is gone; point it at the heading as the "
        "doc spells it, or delete it with the claim it held:\n  "
        + "\n  ".join(dangling)
    )
