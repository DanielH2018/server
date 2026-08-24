"""ADRs and `# DECIDED:` markers must reference each other, in both directions.

WHY THIS IS A TEST. The repo already had a decision record before ADRs existed: 37
`# DECIDED:` markers at the lines they govern, which `.claude/skills/homelab-review/
SKILL.md` step 3 greps before a reviewer flags anything in a role. An ADR set that
referenced those only by convention would be a second registry drifting from the first --
which is the failure ADRs exist to prevent.

WHAT IS NOT CHECKED. A marker without an ADR is fine and common: an ADR exists only when
the reasoning outgrows the line. This asserts that the links which DO exist resolve, not
that every marker has one.

WHY THE SCAN IS CODE-ONLY. A marker annotates a line of code, and every `# DECIDED:`
occurrence in a Markdown file in this repo is documentation ABOUT the convention -- the
ADR template, the ADR index, the plan, `CLAUDE.md` and the reviewer skill all quote it.
Scanning Markdown would read those examples as real markers and demand ADRs for them. The
deliberate cost: a marker added to a role's `CLAUDE.md` is not checked here.

Run: uv run pytest ansible/tests/test_adr_links.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
ADR_DIR = REPO / "docs" / "adr"
MKDOCS = REPO / "mkdocs.yml"

ADR_REF = re.compile(r"\bADR-(\d{4})\b")
MARKER = re.compile(r"#\s*DECIDED:")
# A continuation line of a marker: a comment line carrying no new marker of its own. The
# reference often lands here, because the reasoning comes first and wraps.
COMMENT_LINE = re.compile(r"^\s*(#|--|//)")

SEARCH_SUFFIXES = (".py", ".yml", ".yaml", ".j2", ".sh", ".toml", ".cfg")
SKIP_PARTS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "site",
    "collections",
    "worktrees",
    "Email-to-RSS",
}

VALID_STATUS = re.compile(r"^(Accepted|Proposed|Rejected|Superseded by ADR-\d{4})$")


def _adr_files() -> list[Path]:
    return sorted(p for p in ADR_DIR.glob("[0-9]*.md"))


def _frontmatter(path: Path) -> dict:
    text = path.read_text()
    if not text.startswith("---\n"):
        pytest.fail(f"{path.name}: no frontmatter")
    _, fm, _ = text.split("---", 2)
    loaded = yaml.safe_load(fm)
    if not isinstance(loaded, dict):
        pytest.fail(f"{path.name}: frontmatter is not a mapping")
    return loaded


def _source_files() -> list[Path]:
    out = []
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in SEARCH_SUFFIXES:
            continue
        # Relative, not absolute. A session works from .claude/worktrees/<name>, so
        # "worktrees" is a part of every ABSOLUTE path here and an absolute check skips
        # the entire tree — leaving every link assertion vacuously true.
        if SKIP_PARTS & set(path.relative_to(REPO).parts):
            continue
        # A scanner must not scan itself. This file quotes the marker syntax five times —
        # in its own docstring and in an assertion message — and each quote would count as
        # a marker, inflating the corpus floor with prose about markers rather than markers.
        # The same is true of any file whose subject is the convention; the headlamp
        # mapping test is the one other case, and it names no ADR so it stays harmless.
        if path == Path(__file__).resolve():
            continue
        out.append(path)
    return sorted(out)


def _marker_block(lines: list[str], index: int) -> str:
    """The marker's anchor line plus the contiguous comment lines that continue it.

    A marker's reasoning routinely wraps, and the `(ADR-NNNN)` reference lands on a
    continuation line rather than the anchor. Reading only the anchor line would report a
    correctly-annotated marker as unlinked.
    """
    block = [lines[index]]
    for line in lines[index + 1 :]:
        if not COMMENT_LINE.match(line) or MARKER.search(line):
            break
        block.append(line)
    return "\n".join(block)


def _markers() -> list[tuple[Path, int, str]]:
    """Every real marker as (path, 1-indexed line number, block text)."""
    found = []
    for path in _source_files():
        try:
            lines = path.read_text().splitlines()
        except OSError, UnicodeDecodeError:
            continue
        for i, line in enumerate(lines):
            if MARKER.search(line):
                found.append((path, i + 1, _marker_block(lines, i)))
    return found


# Frontmatter schema — task 1 fixes these key names, and everything below parses them.


@pytest.mark.parametrize("adr", _adr_files(), ids=lambda p: p.name)
def test_frontmatter_carries_every_required_key(adr):
    fm = _frontmatter(adr)
    missing = {"id", "title", "status", "date", "governs"} - set(fm)
    assert not missing, f"{adr.name} is missing {sorted(missing)}"


@pytest.mark.parametrize("adr", _adr_files(), ids=lambda p: p.name)
def test_the_id_matches_the_filename(adr):
    """The filename is how a reader finds a record; the id is how the tree references it."""
    assert f"{_frontmatter(adr)['id']:04d}" == adr.name[:4]


@pytest.mark.parametrize("adr", _adr_files(), ids=lambda p: p.name)
def test_status_is_one_of_the_four_forms(adr):
    status = str(_frontmatter(adr)["status"])
    assert VALID_STATUS.match(status), (
        f"{adr.name}: status {status!r} is not Accepted/Proposed/Rejected/"
        "'Superseded by ADR-NNNN'"
    )


@pytest.mark.parametrize("adr", _adr_files(), ids=lambda p: p.name)
def test_governs_is_a_list(adr):
    """An empty list is valid — some decisions are enforced by no single line."""
    assert isinstance(_frontmatter(adr)["governs"], list)


def test_ids_are_unique():
    ids = [_frontmatter(p)["id"] for p in _adr_files()]
    assert len(ids) == len(set(ids)), f"duplicate ADR ids: {sorted(ids)}"


# Direction 1: an ADR's `governs:` anchors must resolve to a marker naming that ADR.


@pytest.mark.parametrize("adr", _adr_files(), ids=lambda p: p.name)
def test_every_governs_anchor_resolves_to_a_marker_naming_this_adr(adr):
    fm = _frontmatter(adr)
    number = int(fm["id"])
    for anchor in fm["governs"]:
        assert ":" in str(anchor), f"{adr.name}: {anchor!r} is not file:line"
        rel, _, lineno = str(anchor).rpartition(":")
        target = REPO / rel
        assert target.is_file(), f"{adr.name}: {rel} does not exist"
        lines = target.read_text().splitlines()
        index = int(lineno) - 1
        assert 0 <= index < len(lines), f"{adr.name}: {rel} has no line {lineno}"
        assert MARKER.search(lines[index]), (
            f"{adr.name}: {anchor} carries no '# DECIDED:' marker. The anchor must be the "
            "marker's first line, not a line near it."
        )
        block = _marker_block(lines, index)
        assert number in {int(m) for m in ADR_REF.findall(block)}, (
            f"{adr.name}: the marker at {anchor} does not reference ADR-{number:04d}"
        )


# Direction 2: a marker naming an ADR must be listed in that ADR's `governs:`.


def test_every_adr_reference_in_a_marker_is_listed_by_that_adr():
    governs = {}
    for adr in _adr_files():
        fm = _frontmatter(adr)
        governs[int(fm["id"])] = {str(a) for a in fm["governs"]}

    broken = []
    for path, lineno, block in _markers():
        anchor = f"{path.relative_to(REPO)}:{lineno}"
        for ref in {int(m) for m in ADR_REF.findall(block)}:
            if ref not in governs:
                broken.append(
                    f"{anchor} references ADR-{ref:04d}, which does not exist"
                )
            elif anchor not in governs[ref]:
                broken.append(f"{anchor} is not in ADR-{ref:04d}'s governs list")
    assert not broken, "\n".join(broken)


# The index and the nav are two more places an ADR has to appear, and both drift silently.


@pytest.mark.parametrize("adr", _adr_files(), ids=lambda p: p.name)
def test_the_index_links_every_adr(adr):
    """An ADR absent from the index is served but unreachable, which reads as missing."""
    assert adr.name in (ADR_DIR / "index.md").read_text(), (
        f"{adr.name} has no row in docs/adr/index.md"
    )


@pytest.mark.parametrize("adr", _adr_files(), ids=lambda p: p.name)
def test_the_nav_lists_every_adr(adr):
    """scripts/test_mkdocs_config.py only covers docs/*.md, so subdirectories need this."""
    assert f"adr/{adr.name}" in MKDOCS.read_text(), (
        f"{adr.name} is not in the mkdocs.yml nav"
    )


def test_the_template_is_not_treated_as_a_record():
    """template.md is a form. It carries a placeholder id and must stay out of the set."""
    assert (ADR_DIR / "template.md").is_file()
    assert (ADR_DIR / "template.md") not in _adr_files()


def test_the_marker_scan_finds_the_known_corpus():
    """A regex that silently stops matching would make every link check vacuous.

    37 markers across 28 code files on 2026-08-24, once this file is excluded from its own
    scan. A raw grep reports 41 across 29 — the difference is the five times this file
    quotes the marker syntax. Pinned as a floor, not an exact count, so adding a marker
    does not fail the suite while removing the whole convention does.
    """
    markers = _markers()
    assert len(markers) >= 34, (
        f"only {len(markers)} markers found — has the syntax moved?"
    )
    assert len({p for p, _, _ in markers}) >= 24
