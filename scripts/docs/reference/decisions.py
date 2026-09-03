#!/usr/bin/env python3
"""Generate docs/reference/decisions.md — every `# DECIDED:` marker in the tree.

WHY THIS PAGE IS WORTH HAVING. `CLAUDE.md`'s "Review & Memory Hygiene" section asks a
reviewer to write a `# DECIDED:` marker at the line a trade-off governs, rather than only in
a memory file or a commit message, and asks the reviewer brief to grep the literal text
before re-opening a settled decision. That grep only works if the reader knows what to search
for. This page is the register: every marker the tree carries today, grouped by the plane
that owns it, so a reviewer can browse rather than guess a phrase to grep.

WHAT A MARKER LOOKS LIKE. `# DECIDED: <reasoning>`, usually followed by more `#`-prefixed
lines continuing the same paragraph — `docs/adr/index.md`'s convention. A marker can also
appear in plain prose (a CLAUDE.md or ADR pointing at one), which this page renders as a
single line with no continuation, since prose has no comment prefix to extend past.

STATIC TEXT SCAN, NOT A PARSER. Every line containing the literal substring `DECIDED:`, in
every file `git ls-files` tracks, is a row. A `#`/`//`/`--`/`;`-prefixed line pulls in the
immediately following same-prefix, non-blank lines as its continuation, mirroring how a
reader's eye follows the comment block. Nothing here resolves Jinja or parses YAML — the
substring match is what the reviewer brief's own grep does, so this page cannot show a
marker text that grep disagrees with. See `_tracked_files`'s own `DECIDED:` marker for why
file discovery goes through git rather than a walk plus a hand-maintained skip list.

GIT BLAME PER MARKER LINE. `generated_at`/`generated_sha` say when the PAGE last changed;
each row's own "Decided" column is a second, independent date — the author date of the git
blame for that specific line, so a reader can tell a decision from 2026-08 apart from one
made yesterday. `git blame` fails on an uncommitted line (a file this worktree added or
edited and hasn't committed), and that failure is rendered as "unknown" rather than raised —
the page should still list the marker.

POSSIBLE DUPLICATES. Two markers whose first sentence normalises (lowercased, whitespace
collapsed) to a near-identical string are flagged at the top of the page. A near-duplicate
usually means the same trade-off was re-decided in two places, or a marker was copied and
never specialised — either way it is worth a human's second look, not a generator's verdict.

Usage::

    uv run python scripts/docs/reference/decisions.py --out docs/reference/decisions.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import os
import re
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.git import git
from lib.repo_paths import REPO

_MARKER = "DECIDED:"

# Directories a tree-wide text scan must prune: VCS internals, other sessions' worktrees,
# build output and virtualenvs. Pruning, not filtering, is what keeps this fast — see
# _mkdocs_repo_links.py's identical rationale for the same skip list.
_SKIP_DIR_NAMES = {".git", ".venv", "site", "node_modules", "__pycache__"}
_SKIP_PATH_PARTS = (".claude/worktrees",)

# This generator's own source, its dedicated test fixtures, and the page it writes all name
# the literal marker text while explaining or exercising the convention -- not recording a
# project trade-off. Left unexcluded, this generator's docstring and this test file's escaped
# fixture strings (each a real grep hit, since the scan IS a grep) render as garbled rows, and
# the generated page's own "N markers found" sentence would count itself, so a second run
# always finds one more marker than the first. Excluded by exact path rather than a pattern,
# so a rename breaks this loudly (an unrecognised path just re-appears in the output) instead
# of silently widening what else gets skipped.
_SELF_PATHS = frozenset(
    {
        "scripts/docs/reference/decisions.py",
        "scripts/docs/tests/test_gen_reference_decisions.py",
        "docs/reference/decisions.md",
    }
)

# A line-comment prefix: leading whitespace, then one comment token, then a space or EOL.
# Matches `# `, `    # `, `// `, `-- `, `; `. Deliberately excludes Jinja's `{# #}` and
# C-style `/* */` block comments — no marker in the tree today uses either, and detecting a
# block comment's continuation needs a close token this page does not need to chase.
_COMMENT_PREFIX_RE = re.compile(r"^(\s*(?:#|//|--|;))(?:\s|$)")

# Where the "first sentence" ends: a period followed by a space or end of string. A period
# inside an abbreviation or a code token (`v1.2`, `e.g.`) can trip this, same as any sentence
# splitter; the fallback (the whole line) keeps that failure mode from truncating mid-clause.
_SENTENCE_END_RE = re.compile(r"\.(?:\s|$)")

_PLANES: list[tuple[str, str]] = [
    ("ansible/roles/k8s/", "roles/k8s"),
    ("ansible/roles/setup/", "roles/setup"),
    ("ansible/roles/containers/", "roles/containers"),
    ("scripts/", "scripts"),
    ("docs/", "docs"),
]

# Near-duplicate detection: two normalised first sentences at or above this similarity are
# flagged. 1.0 is exact (after case/whitespace normalisation); this is deliberately looser so
# a marker copied and lightly edited still surfaces.
_DUPLICATE_THRESHOLD = 0.90


def _tracked_files(root: Path) -> list[str] | None:
    """`git ls-files` under `root`, or None when `root` is not a git repository.

    DECIDED: `git ls-files`, not a walk plus a hand-maintained skip list — the same
    reasoning `ansible/tests/repo/test_documented_paths_exist.py` gives for the same
    choice. A walk sees whatever happens to be on disk, and this repo grows untracked
    trees during ordinary work: `.venv`, `ansible/collections/` (vendored per worktree),
    `__pycache__`, and — the concrete miss that motivated this — `.mkdocs-strict-check/`,
    the prek hook's own build output, which is not one of those named exceptions and
    is not going to be the last one nobody thought to skip. `git ls-files` already knows
    what the repo considers real, via `.gitignore`, without a second list to keep in sync.
    """
    try:
        result = git("ls-files", "-z", cwd=root, check=False, timeout=30)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return [p for p in result.stdout.split("\0") if p]


def _iter_candidate_files(root: Path):
    """Every file under `root`: every tracked file when `root` is a git repo.

    Falls back to a pruned walk when `root` is not a repo — a fixture tree under
    `tmp_path` in a test, which `git ls-files` can't see anything in.
    """
    tracked = _tracked_files(root)
    if tracked is not None:
        for rel in tracked:
            if rel not in _SELF_PATHS:
                yield root / rel
        return

    for directory, subdirectories, filenames in os.walk(root):
        current = Path(directory)
        relative = current.relative_to(root).as_posix()
        prefix = "" if relative == "." else f"{relative}/"
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in _SKIP_DIR_NAMES and f"{prefix}{name}" not in _SKIP_PATH_PARTS
        ]
        for name in filenames:
            rel = f"{prefix}{name}"
            if rel in _SELF_PATHS:
                continue
            yield current / name


def _first_sentence(text: str) -> str:
    """The leading sentence of `text`, or the whole thing if no sentence boundary is found."""
    match = _SENTENCE_END_RE.search(text)
    return text[: match.end()].strip() if match else text.strip()


def _continuation_lines(lines: list[str], start_idx: int, prefix: str) -> list[str]:
    """Same-prefix lines immediately following `start_idx`, up to the next paragraph break.

    Stops at a blank line, a line that does not repeat the comment prefix, a comment line
    with no content after the prefix (a bare `#` — the paragraph break a reader's eye uses
    inside a comment block that never has a truly blank line between paragraphs), or a line
    that starts a second marker (this file already gets its own row).
    """
    out = []
    for line in lines[start_idx + 1 :]:
        if not line.strip():
            break
        found = _COMMENT_PREFIX_RE.match(line)
        if not found or found.group(1).strip(" ") != prefix.strip(" "):
            break
        content = line[found.end() :].strip()
        if not content or _MARKER in content:
            break
        out.append(content)
    return out


def _plane(rel_path: str) -> str:
    for path_prefix, name in _PLANES:
        if rel_path.startswith(path_prefix):
            return name
    return "other"


def _blame_date(rel_path: str, line_no: int, repo: Path) -> str:
    """The author date (YYYY-MM-DD) of the git blame for one line, or "unknown".

    Never raises: an uncommitted file, a path git does not track, or any blame failure all
    mean the honest answer is "unknown" rather than a crashed generator.
    """
    try:
        result = git(
            "blame",
            "--porcelain",
            "-L",
            f"{line_no},{line_no}",
            "--",
            rel_path,
            cwd=repo,
            check=False,
            timeout=15,
        )
    except OSError:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    for out_line in result.stdout.splitlines():
        if out_line.startswith("author-time "):
            try:
                ts = int(out_line.split(" ", 1)[1])
                return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime(
                    "%Y-%m-%d"
                )
            except ValueError, OverflowError, OSError:
                return "unknown"
    return "unknown"


def build_rows(root: Path = REPO, repo: Path = REPO) -> list[dict[str, str]]:
    """Collect every `DECIDED:` marker under `root` into one row per marker.

    Args:
        root: Root directory to search for marker text.
        repo: The git repository `git blame` runs against — separate from `root` so a
            fixture tree under `tmp_path` (not a repo) can still be handed a real `repo`
            in a test, or the blame can be skipped by handing a non-repo path.

    Returns:
        One dict per marker, keyed by the columns rendered in `render_markdown`.
    """
    rows: list[dict[str, str]] = []
    for path in sorted(_iter_candidate_files(root)):
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if _MARKER not in text:
            continue
        lines = text.splitlines()
        try:
            rel_path = path.relative_to(root).as_posix()
        except ValueError:
            rel_path = path.as_posix()
        for idx, line in enumerate(lines):
            marker_at = line.find(_MARKER)
            if marker_at == -1:
                continue
            after = line[marker_at + len(_MARKER) :].strip()
            first_sentence = _first_sentence(after) if after else ""
            continuation: list[str] = []
            comment_match = _COMMENT_PREFIX_RE.match(line)
            if comment_match:
                continuation = _continuation_lines(lines, idx, comment_match.group(1))
            full_text = " ".join([first_sentence, *continuation]).strip()
            rows.append(
                {
                    "path": rel_path,
                    "line": str(idx + 1),
                    "plane": _plane(rel_path),
                    "first_sentence": first_sentence,
                    "text": full_text or "(no text after the marker)",
                    "decided": _blame_date(rel_path, idx + 1, repo),
                }
            )
    return rows


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def find_possible_duplicates(
    rows: list[dict[str, str]],
) -> list[tuple[dict[str, str], dict[str, str]]]:
    """Pairs of rows whose normalised first sentence is near-identical.

    O(n^2) over the marker count (~150 today); fine at this size, and simple enough that a
    reviewer can re-derive what it does without reading a second helper.
    """
    pairs = []
    normalised = [
        (_normalise(r["first_sentence"]), r) for r in rows if r["first_sentence"]
    ]
    for i in range(len(normalised)):
        norm_a, row_a = normalised[i]
        for j in range(i + 1, len(normalised)):
            norm_b, row_b = normalised[j]
            if row_a["path"] == row_b["path"] and row_a["line"] == row_b["line"]:
                continue
            ratio = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
            if ratio >= _DUPLICATE_THRESHOLD:
                pairs.append((row_a, row_b))
    return pairs


def render_markdown(rows: list[dict[str, str]]) -> str:
    """Render `rows` as the "Decisions" reference page, banner and duplicate flags included.

    Args:
        rows: Marker rows as returned by `build_rows`.

    Returns:
        The full page as Markdown text, ending in a single trailing newline.
    """
    from lib.docs_provenance import generated_banner, md_cell

    parts = [generated_banner("scripts/docs/reference/decisions.py")]
    parts.append("# Decisions\n")
    parts.append(
        f"{len(rows)} `DECIDED:` marker(s) found across the tree. A marker is a settled "
        "trade-off recorded as a comment at the line it governs, per CLAUDE.md's "
        '"Review & Memory Hygiene" section — written so a reviewer trips over the reasoning '
        "before re-opening a decision that already has one. A reviewer brief greps the "
        "literal marker text before flagging something as new; this page exists so a human "
        "can browse the same set instead of guessing a phrase to grep.\n"
    )

    duplicates = find_possible_duplicates(rows)
    if duplicates:
        parts.append('!!! warning "Possible duplicates"')
        parts.append(
            "    Two markers below have a near-identical first sentence once case and "
            "whitespace are normalised — usually the same trade-off decided twice, or a "
            "marker copied and never specialised. Worth a look, not a verdict.\n"
        )
        for row_a, row_b in duplicates:
            parts.append(
                f"    * `{row_a['path']}:{row_a['line']}` and "
                f"`{row_b['path']}:{row_b['line']}`"
            )
        parts.append("")

    for _plane_prefix, plane_name in [*_PLANES, ("", "other")]:
        plane_rows = [r for r in rows if r["plane"] == plane_name]
        if not plane_rows:
            continue
        parts.append(f"## {plane_name}\n")
        parts.append("| Marker | File | Decided |")
        parts.append("|---|---|---|")
        for row in sorted(plane_rows, key=lambda r: (r["path"], int(r["line"]))):
            parts.append(
                f"| {md_cell(row['text'])} | `{row['path']}:{row['line']}` | "
                f"{row['decided']} |"
            )
        parts.append("")

    return "\n".join(parts).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    """Build the marker rows, render the reference page, and write it if the body changed.

    Returns:
        The exit code from `finish_generator` (0 on success, non-zero on a write failure).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    parser.add_argument("--root", type=Path, default=REPO)
    args = parser.parse_args(argv)

    from lib.docs_provenance import finish_generator

    rows = build_rows(args.root, args.root)
    return finish_generator(
        "docs.reference.decisions", args.out, rows, render_markdown, "marker"
    )


if __name__ == "__main__":
    raise SystemExit(main())
