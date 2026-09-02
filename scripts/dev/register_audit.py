#!/usr/bin/env python3
"""Report which rows of the homelab-review recurring-open register look closed.

THE PROBLEM. `/homelab-review`'s standing don't-re-flag memory carries a table of
confirmed-open findings under "Open and recurring — report as recurrence, not
discovery". That register has gone stale in the same direction four times: a row
stays listed as open for days after the underlying work landed, because nobody
re-checks the repo before a review run re-derives the same finding cold. Two rows
were still listed open on 2026-08-22 when the fix had already shipped days earlier
(`test_b2_storage.py`, the Longhorn Grafana dashboard).

WHAT THIS DOES. For each register row, extract whatever evidence its prose cites —
a `path:line`, a bare repo path, a bare identifier (`check_b2_storage`), or a
**bolded** description of what's missing — and check whether matching evidence
exists in the repo today. A row's prose usually cites the CURRENT anchor of the gap
(the code that still has the problem), not the fix, so a bare citation resolving is
not itself proof of closure — that citation almost always resolves, since it was
correct when the row was written. What DOES indicate closure is a *different* path
that matches what the row says is missing: an identifier this repo names
`check_X` gets a `test_X.py` fixture by convention (Tier A below), or the row's
bolded/proper-noun vocabulary ("Grafana dashboard", "Longhorn") co-occurs in a path
that didn't necessarily exist when the row was written (Tier B). A bare citation
that merely exists is downgraded to context, not closure (Tier C).

This is read-only and best-effort. It never edits the register or the repo, and it
never drops a row: a row with no extractable reference is reported UNRESOLVED so a
human reads it, rather than silently passed over. Exit is always 0 -- this is a
report, not a gate.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import REPO  # noqa: E402

DEFAULT_REGISTER = (
    Path.home()
    / ".claude"
    / "projects"
    / "-home-ubuntu-server"
    / "memory"
    / "homelab-review-standing-donot-reflag.md"
)
SECTION_HEADING = "Open and recurring"

SKIP_DIR_NAMES = {
    ".git",
    "collections",
    "node_modules",
    ".venv",
    "__pycache__",
    ".claude",
}

KNOWN_PREFIXES = ("check_", "verify_", "get_", "fetch_")

STOPWORDS = {
    "this",
    "that",
    "with",
    "asked",
    "answered",
    "verified",
    "exists",
    "half",
    "arm",
    "give",
    "only",
    "still",
    "also",
    "brief",
    "again",
    "after",
    "before",
    "which",
    "where",
    "there",
    "their",
    "does",
    "done",
    "each",
    "have",
    "here",
    "into",
    "just",
    "more",
    "most",
    "over",
    "some",
    "such",
    "than",
    "them",
    "then",
    "when",
    "will",
    "your",
    "citation",
    "carried",
    "seen",
    "runs",
}

# A Markdown table cell boundary is an UNESCAPED pipe. Splitting on every `|` shifted every
# cell of the push-token row, whose prose carries a backticked `grep -E 'a\|b\|c'` alternation —
# it reported `runs=longhorn_backup\` and `first_seen=etcd_snapshot\` off the middle of a
# sentence. A row that parses into the wrong columns is worse than one that fails to parse.
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")

BACKTICK_RE = re.compile(r"`([^`]+)`")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
PATHLINE_RE = re.compile(r"^([\w./*-]+):(\d+)$")
PATH_RE = re.compile(r"^[\w./*-]*/[\w./*-]+\.[A-Za-z0-9]+$|^[\w.*-]+\.[A-Za-z0-9]+$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
UNANSWERED_ASK_RE = re.compile(r"not answered|not re-verified", re.I)

STATUS_CLOSED = "LIKELY-CLOSED"
STATUS_OPEN = "OPEN"
STATUS_UNRESOLVED = "UNRESOLVED"


@dataclass
class Reference:
    """One piece of evidence extracted from a row's prose."""

    text: str
    kind: str  # "path:line" | "path" | "identifier" | "keyword"
    tier: str  # "A" (fixture heuristic) | "B" (keyword search) | "C" (bare citation)
    resolved: Path | None


@dataclass
class Row:
    item: str
    first_seen: str
    runs_carried: str


@dataclass
class RowResult:
    row: Row
    status: str
    reference: Reference | None


def find_section(text: str, heading_substr: str) -> str:
    """Return the body text under the first heading containing `heading_substr`.

    Heading-anchored, not line-number-anchored, because the register is edited
    often and a line number would drift the next commit.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") and heading_substr in stripped:
            start = i
            break
    if start is None:
        raise LookupError(f"no heading containing {heading_substr!r} found in register")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip().startswith("#"):
            end = j
            break
    return "\n".join(lines[start + 1 : end])


def parse_table_rows(section_text: str) -> list[Row]:
    """Parse the first markdown table in `section_text` into Row objects.

    Skips the header row and the `---` separator row. Stops at the first
    non-`|`-prefixed line after the table has started, so trailing prose in the
    same section doesn't get mistaken for rows.
    """
    rows: list[Row] = []
    header_seen = False
    sep_seen = False
    for line in section_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if header_seen:
                break
            continue
        cells = [c.strip() for c in _CELL_SPLIT_RE.split(stripped.strip("|"))]
        if not header_seen:
            header_seen = True
            continue
        if not sep_seen:
            sep_seen = True
            continue
        if len(cells) >= 3:
            rows.append(Row(item=cells[0], first_seen=cells[1], runs_carried=cells[2]))
    return rows


def iter_repo_files(repo: Path):
    """Yield every file under `repo`, pruning vendored/noise directories."""
    for dirpath, dirnames, filenames in repo.walk():
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for fn in filenames:
            yield dirpath / fn


def _sorted_files(repo: Path) -> list[Path]:
    return sorted(iter_repo_files(repo), key=lambda p: str(p))


def resolve_path(repo: Path, path_str: str, files: list[Path]) -> Path | None:
    """Resolve a cited path/filename against the repo. Bare names search by basename."""
    if "*" in path_str:
        matches = sorted(repo.glob(path_str))
        return matches[0] if matches else None
    candidate = repo / path_str
    if candidate.exists():
        return candidate
    basename = Path(path_str).name
    matches = [f for f in files if f.name == basename]
    return matches[0] if matches else None


def strip_known_prefix(identifier: str) -> str:
    for prefix in KNOWN_PREFIXES:
        if identifier.startswith(prefix):
            return identifier[len(prefix) :]
    return identifier


def find_fixture(stem: str, files: list[Path]) -> Path | None:
    """Tier A: does a `test_*<stem>*` file exist for a `check_<stem>`-style identifier?"""
    stem = stem.lower()
    matches = [
        f
        for f in files
        if f.name.lower().startswith("test_") and stem in f.name.lower()
    ]
    return matches[0] if matches else None


def collect_keywords(item: str, bolds: list[str]) -> set[str]:
    """Distinctive lowercase words from bolded spans plus proper nouns in the prose.

    Bold text in this register usually marks the explicit ask ("Give it a
    recorded-response fixture"); a capitalized word outside bold (e.g. "Longhorn")
    is often the subject the bold phrase is missing evidence for. The first word
    of the sentence is excluded so ordinary capitalization at the sentence start
    doesn't count as a proper noun.
    """
    words: set[str] = set()
    for span in bolds:
        for w in re.findall(r"[A-Za-z]+", span):
            wl = w.lower()
            if len(wl) >= 4 and wl not in STOPWORDS:
                words.add(wl)
    tokens = item.split()
    for idx, tok in enumerate(tokens):
        w = re.sub(r"[^A-Za-z]", "", tok)
        if idx == 0 or len(w) < 4:
            continue
        if w[0].isupper() and w.lower() not in STOPWORDS:
            words.add(w.lower())
    return words


def keyword_search(keywords: set[str], files: list[Path]) -> Path | None:
    """Tier B: find a repo path whose lowercased text contains enough of `keywords`.

    Needs at least 2 extracted keywords and requires most of them to co-occur in
    one path, so a single word -- "Grafana" alone matched grafana-secret.yaml.j2
    for a row that had nothing to do with it -- can't fire this tier by itself.
    """
    if len(keywords) < 2:
        return None
    threshold = max(2, -(-len(keywords) * 3 // 5))  # ceil(0.6 * len(keywords)), floor 2
    for f in files:
        rel = str(f).lower()
        count = sum(1 for k in keywords if k in rel)
        if count >= threshold:
            return f
    return None


def classify_row(row: Row, repo: Path, files: list[Path]) -> RowResult:
    item = row.item
    backticks = BACKTICK_RE.findall(item)
    bolds = BOLD_RE.findall(item)
    any_candidate = False

    # Tier A: `check_X`-style identifier + "fixture"/"test" in the row -> look for test_X.
    if re.search(r"fixture|test", item, re.I):
        for span in backticks:
            if IDENT_RE.match(span) and "_" in span:
                any_candidate = True
                stem = strip_known_prefix(span)
                hit = find_fixture(stem, files)
                if hit is not None:
                    ref = Reference(
                        text=span, kind="identifier", tier="A", resolved=hit
                    )
                    return RowResult(row, STATUS_CLOSED, ref)

    # Tier B: bolded / proper-noun keywords co-occurring in a repo path. Skipped
    # when the bold text is this register's "asked and not answered" boilerplate --
    # that marks the ask as still outstanding, not a description of what to find.
    keywords = (
        set() if UNANSWERED_ASK_RE.search(item) else collect_keywords(item, bolds)
    )
    if keywords:
        any_candidate = True
        hit = keyword_search(keywords, files)
        if hit is not None:
            ref = Reference(
                text=", ".join(sorted(keywords)), kind="keyword", tier="B", resolved=hit
            )
            return RowResult(row, STATUS_CLOSED, ref)

    # Tier C: a bare path/path:line citation. This is the row's anchor for the
    # CURRENT gap, not evidence of a fix -- it almost always resolves, so it
    # downgrades to context and the row stays OPEN.
    decisive: Reference | None = None
    for span in backticks:
        m = PATHLINE_RE.match(span)
        if m:
            any_candidate = True
            hit = resolve_path(repo, m.group(1), files)
            decisive = Reference(text=span, kind="path:line", tier="C", resolved=hit)
            break
        if PATH_RE.match(span):
            any_candidate = True
            hit = resolve_path(repo, span, files)
            decisive = Reference(text=span, kind="path", tier="C", resolved=hit)
            break
    if decisive is None:
        for span in backticks:
            if IDENT_RE.match(span):
                any_candidate = True
                decisive = Reference(
                    text=span, kind="identifier", tier="C", resolved=None
                )
                break

    if decisive is not None:
        return RowResult(row, STATUS_OPEN, decisive)
    if any_candidate:
        return RowResult(row, STATUS_OPEN, None)
    return RowResult(row, STATUS_UNRESOLVED, None)


def first_clause(item: str) -> str:
    """The row's lead clause, for a compact one-line report."""
    for sep in (" — ", " -- "):
        if sep in item:
            item = item.split(sep, 1)[0]
            break
    else:
        if ". " in item:
            item = item.split(". ", 1)[0]
    item = item.strip()
    if len(item) > 100:
        item = item[:97] + "..."
    return item


def format_report(results: list[RowResult], repo: Path) -> str:
    from collections import Counter

    counts = Counter(r.status for r in results)
    lines = [
        f"register audit: {len(results)} row(s) -- "
        f"{counts.get(STATUS_CLOSED, 0)} likely-closed, "
        f"{counts.get(STATUS_OPEN, 0)} open, "
        f"{counts.get(STATUS_UNRESOLVED, 0)} unresolved",
        "",
    ]
    for r in results:
        clause = first_clause(r.row.item)
        if r.reference is None:
            ref_note = "ref: (none extracted)"
        else:
            if r.reference.resolved is not None:
                try:
                    shown = str(r.reference.resolved.relative_to(repo))
                except ValueError:
                    shown = str(r.reference.resolved)
                resolved_note = f" -> {shown}"
            else:
                resolved_note = " (does not resolve)"
            ref_note = f"ref[{r.reference.tier}]: {r.reference.text}{resolved_note}"
        lines.append(
            f"[{r.status:13}] runs={r.row.runs_carried:>3} "
            f"first_seen={r.row.first_seen:12} {clause}  {ref_note}"
        )
    return "\n".join(lines)


def audit(register_path: Path, repo: Path) -> list[RowResult]:
    text = register_path.read_text()
    section = find_section(text, SECTION_HEADING)
    rows = parse_table_rows(section)
    files = _sorted_files(repo)
    return [classify_row(row, repo, files) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--register",
        default=str(DEFAULT_REGISTER),
        help="path to the memory file holding the recurring-open register",
    )
    parser.add_argument(
        "--repo",
        default=str(REPO),
        help="repo checkout to resolve cited evidence against",
    )
    args = parser.parse_args(argv)

    register_path = Path(args.register)
    repo = Path(args.repo)
    results = audit(register_path, repo)
    print(format_report(results, repo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
