"""How old a hand-written doc is, and whether the files it names have moved under it.

WHY. The generated reference pages carry `generated_at`, so a reader can tell a page that
was rebuilt this morning from one nobody has looked at. The hand-written pages carry
nothing: no date, and no way to tell whether the retain count or the script the page
describes changed after the page did. On 2026-09-02 five quoted values on those pages were
stale, and nothing on the site said so. Git already knows both halves -- when a page last
changed, and when each file it names last changed -- and this module reads them.

TWO NUMBERS PER PAGE. `changed` is the date of the page's last commit. `moved` is the
subset of the repo files the page names whose last commit is later than that. A page with
three moved sources is the page to reread next; a page with none is as fresh as its sources.
This is a ranking, not a verdict -- a source can change without changing what the page
says about it -- which is why it is shown on the page and on a reference table rather than
asserted by a test.

WHAT COUNTS AS A NAMED SOURCE. Any backticked token that reads as a repo path, with or
without a `:line` or `::node` suffix, that resolves to a tracked file: doc-relative first,
repo-relative second, and as the unique suffix of some tracked path third, which is what
makes `deploy_logic.py` resolve when there is one such file and stay unresolved when a
tests/ copy makes it ambiguous. This is deliberately wider than the citation guard in
`ansible/tests/repo/test_documented_paths_exist.py`, which counts only line-numbered
citations because a bare path there is a claim it cannot always check. Here an unresolved
path costs nothing: it is simply not a source.

ONE GIT CALL. `git log --format=%cs --name-only` over the whole history, walked once: the
first date a path appears under is its last change. A per-file `git log -1` would cost one
process per named source, several hundred per build.

A mechanical rewrite that touches many pages (the 2026-09-01 test-path rename rewrote 305
citations) resets those pages' `changed` without anyone reading them. Accepted: the `moved`
count is the more honest number, and it survives.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# A directly-imported lib module gets only its importer's directory on sys.path; the
# sibling package needs the scripts/ root, the same way docs_provenance.py reaches it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.git import git_stdout  # noqa: E402

# A code span, and within it a path-like word: a letter-led extension, an optional `:12`,
# `:12-40` or `::test_name` suffix, which is dropped. Words rather than whole spans, because
# the docs name a file inside a command as often as on its own -- `./scripts/deploy.sh
# --tags x` names deploy.sh. The extension must start with a letter so that
# `10.0.0.240:51820` does not parse as a file.
SPAN = re.compile(r"`([^`\n]+)`")
PATH_WORD = re.compile(
    r"(?:\./)?([\w.][\w./-]*\.[a-z][a-z0-9]*)(?::\d+(?:-\d+)?|::[\w\[\]:.-]+)?"
)


def code_spans(text: str) -> list[str]:
    """Every inline code span, plus every line inside a fenced block, as span text."""
    spans: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif in_fence:
            spans.append(line)
        else:
            spans.extend(SPAN.findall(line))
    return spans


def named_paths(text: str) -> list[str]:
    """Every path-like word in the page's code, with any line or node suffix removed."""
    found: list[str] = []
    for span in code_spans(text):
        for word in span.split():
            match = PATH_WORD.fullmatch(word.strip("(),;'\""))
            if match:
                found.append(match.group(1))
    return found


# Not hand-written, or not live: the generated trees (their own stamp), the archive (frozen
# by design), the ADR form, and the fragments a page splices in.
SKIPPED_PREFIXES = ("docs/archive/", "docs/assets/", "docs/reference/")
SKIPPED_PAGES = ("docs/adr/template.md",)
GENERATED_MARKER = "generated_from:"


@dataclass(frozen=True)
class PageFreshness:
    """One hand-written page, dated, with the sources it names.

    Attributes:
        page: repo-relative path of the page.
        changed: ISO date of the page's last commit, or "" when git has none.
        sources: (path, date) for every named file that resolved, in first-mention order.
        moved: the subset of `sources` whose date is later than `changed`.
    """

    page: str
    changed: str
    sources: list[tuple[str, str]] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)


def tracked_files(repo: Path) -> set[str]:
    return {p for p in git_stdout("ls-files", "-z", cwd=repo).split("\0") if p}


def parse_change_dates(log: str) -> dict[str, str]:
    """The last-change date per path, from `git log --format=%cs --name-only` output.

    The log is newest-first, so the first date a path is seen under is its latest. A
    path's own line never starts with a digit-dash-digit date, and a date line is never a
    path: `%cs` is strict ISO, and no tracked file is named like one.
    """
    dates: dict[str, str] = {}
    current = ""
    for line in log.splitlines():
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", line):
            current = line
        elif line and line not in dates:
            dates[line] = current
    return dates


def last_change_dates(repo: Path) -> dict[str, str]:
    return parse_change_dates(
        git_stdout("log", "--format=%cs", "--name-only", cwd=repo)
    )


def resolve(named: str, page: str, tracked: set[str]) -> str | None:
    """The tracked path a backticked name refers to, or None when it names nothing here.

    Doc-relative, then repo-relative, then the unique tracked path ending in `/<named>`.
    A suffix, never a basename alone, and never an ambiguous suffix: two matches mean the
    reader could not tell either, so neither is a source.
    """
    relative = (Path(page).parent / named).as_posix()
    if relative in tracked:
        return relative
    if named in tracked:
        return named
    tail = "/" + named
    matches = [p for p in tracked if p.endswith(tail)]
    return matches[0] if len(matches) == 1 else None


def named_sources(text: str, page: str, tracked: set[str]) -> list[str]:
    """Every distinct tracked file the page names, in order of first mention."""
    seen: list[str] = []
    for named in named_paths(text):
        resolved = resolve(named, page, tracked)
        if resolved and resolved != page and resolved not in seen:
            seen.append(resolved)
    return seen


def page_freshness(
    page: str, text: str, dates: dict[str, str], tracked: set[str]
) -> PageFreshness:
    changed = dates.get(page, "")
    sources = [(p, dates.get(p, "")) for p in named_sources(text, page, tracked)]
    moved = [(p, d) for p, d in sources if d > changed]
    return PageFreshness(page, changed, sources, moved)


def is_hand_written(page: str, text: str) -> bool:
    if not page.startswith("docs/") or not page.endswith(".md"):
        return False
    if page.startswith(SKIPPED_PREFIXES) or page in SKIPPED_PAGES:
        return False
    # Only a marker in the frontmatter counts: a page ABOUT the generators mentions the
    # string in prose, and the edit hook scopes its own check to the generated trees.
    if not text.startswith("---\n"):
        return True
    frontmatter = text.split("---", 2)[1]
    return GENERATED_MARKER not in frontmatter


def hand_written_pages(repo: Path, tracked: set[str]) -> list[str]:
    return sorted(
        p
        for p in tracked
        if p.startswith("docs/")
        and p.endswith(".md")
        and is_hand_written(p, (repo / p).read_text(encoding="utf-8"))
    )


def survey(repo: Path) -> list[PageFreshness]:
    """Every hand-written page, dated, with its sources -- the input both consumers share."""
    tracked = tracked_files(repo)
    dates = last_change_dates(repo)
    return [
        page_freshness(p, (repo / p).read_text(encoding="utf-8"), dates, tracked)
        for p in hand_written_pages(repo, tracked)
    ]
