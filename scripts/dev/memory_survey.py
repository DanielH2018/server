#!/usr/bin/env python3
"""Survey the project's Claude memory store and report what it costs and what nothing reads.

The memory index (`MEMORY.md`) is injected verbatim into every session, so every pointer line
is charged every time. The line cap in the SessionStart hook bounds how much gets *written*;
it says nothing about whether what was written still earns its injection. This script supplies
the evidence that judgement needs, and deliberately makes no judgement itself:

  * bytes and an estimated token cost for the injected index, and for the store as a whole
  * pointer lines that name a file which does not exist (a dead link in the index)
  * memory files nothing in the index points at (an orphan nothing will ever surface)
  * per-file last-reference date, computed by scanning session transcripts for the file's slug
  * near-duplicate candidates, by shingled-token overlap between file bodies

Every number here is a *proxy*. `/context` reports the real token share and this does not; a
transcript scan finds a slug that was mentioned, which is weaker than one that was acted on.
Both are still far better than a line count, which is what the cap measures today.

Read-only by construction: it opens files for reading and writes nothing back. The consolidation
pass that consumes this output proposes a diff for review rather than editing the store, because
several worktree sessions append to `MEMORY.md` concurrently and a scheduled writer would race
them.

Usage:
    uv run python scripts/dev/memory_survey.py
    uv run python scripts/dev/memory_survey.py --memory-dir ~/.claude/projects/-home-ubuntu-server/memory
    uv run python scripts/dev/memory_survey.py --transcript-days 30 --json

Exit code is 0 for a clean survey and 1 when it finds a dead index link, so it is usable as a
gate. Orphans and stale files are reported but never fail the run: they are input to a judgement
call, not defects.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

# The checkout a memory's cited check paths are resolved against. Taken from this file's own
# location so the survey works from a worktree, where the primary checkout's paths would be the
# wrong tree to ask.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MEMORY_DIR = Path.home() / ".claude/projects/-home-ubuntu-server/memory"
DEFAULT_TRANSCRIPT_DIR = Path.home() / ".claude/projects/-home-ubuntu-server"

# A pointer line in MEMORY.md looks like `- [Title](some-file.md) — hook`. Links may also
# appear inline in a prose paragraph, which is why this is a search over the whole file
# rather than a per-line parse anchored to a leading dash.
#
# The title alternation admits ONE level of nested square brackets, because real titles carry
# them: `- [A task tagged [config, deploy] is skipped by --skip-tags of either](...)`. A plain
# `[^\]]+` stops at the inner `]` and the link stops being a link at all — which mis-reports the
# file as an orphan AND, worse, hides it from the dead-link check, the one condition that fails
# this run. A title the regex cannot parse is a pointer the gate cannot police.
_LINK = re.compile(r"\[(?:[^\[\]]|\[[^\[\]]*\])+\]\(([^)]+\.md)\)")

# Rough characters-per-token for English prose. Only ever used to turn bytes into an
# order-of-magnitude token figure; it is not a tokenizer and does not pretend to be.
_CHARS_PER_TOKEN = 4

# Shingle width for near-duplicate detection. Four words is long enough that ordinary
# shared vocabulary ("the deploy fails when") does not match by accident, and short enough
# that two entries describing one fact in different sentences still overlap.
_SHINGLE = 4

# A transcript line naming more slugs than this is the injected index, not a citation. Five is
# comfortably above what a sentence citing related memories does (the index's own prose groups
# link two or three) and far below the ~100 a full injection carries.
_BULK_SLUG_LINE = 5


def _read(path: Path) -> str:
    """Read a file as text, tolerating the odd non-UTF-8 byte rather than dying on it."""
    return path.read_text(encoding="utf-8", errors="replace")


def _est_tokens(n_bytes: int) -> int:
    return n_bytes // _CHARS_PER_TOKEN


def index_links(index_path: Path) -> list[str]:
    """Return every `.md` filename the index links to, in document order, deduplicated."""
    if not index_path.exists():
        return []
    seen: dict[str, None] = {}
    for target in _LINK.findall(_read(index_path)):
        # An index link is always a sibling filename; strip any directory prefix so a
        # link written as `./foo.md` and one written as `foo.md` compare equal.
        seen.setdefault(Path(target).name, None)
    return list(seen)


def _body_words(path: Path) -> list[str]:
    """Lowercased word tokens of a memory file's body, with YAML frontmatter removed.

    Frontmatter carries `name:`/`description:` that restate the body, so leaving it in
    would inflate every pairwise overlap by a constant and flatten the ranking.
    """
    text = _read(path)
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return re.findall(r"[a-z0-9_]+", text.lower())


def _shingles(words: list[str], width: int = _SHINGLE) -> set[tuple[str, ...]]:
    if len(words) < width:
        return set()
    return {tuple(words[i : i + width]) for i in range(len(words) - width + 1)}


def duplicate_candidates(
    files: list[Path], threshold: float = 0.12, limit: int = 20
) -> list[tuple[str, str, float]]:
    """Rank file pairs by Jaccard overlap of their body shingles, descending.

    The threshold is deliberately low. This produces *candidates* for a reader to judge,
    and a merge decision that a human or a model makes from the text is cheap, where a
    missed duplicate stays in the store for months.
    """
    shingles = {f.name: _shingles(_body_words(f)) for f in files}
    pairs: list[tuple[str, str, float]] = []
    names = sorted(shingles)
    for i, a in enumerate(names):
        sa = shingles[a]
        if not sa:
            continue
        for b in names[i + 1 :]:
            sb = shingles[b]
            if not sb:
                continue
            union = len(sa | sb)
            if not union:
                continue
            score = len(sa & sb) / union
            if score >= threshold:
                pairs.append((a, b, round(score, 3)))
    pairs.sort(key=lambda p: p[2], reverse=True)
    return pairs[:limit]


def _assistant_text(line: str) -> str:
    """Return the assistant's own words from one transcript record, or "" for anything else.

    A transcript record is a JSON object whose `message.content` is a list of blocks. Only
    `text` and `thinking` blocks are the model speaking; `tool_use` and `tool_result` blocks
    carry command output, which names memory files without citing them.
    """
    try:
        rec = json.loads(line)
    except ValueError, TypeError:
        return ""
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return ""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text") or block.get("thinking") or ""
        for block in content
        if isinstance(block, dict) and block.get("type") in ("text", "thinking")
    ]
    return "\n".join(p for p in parts if p)


def last_referenced(
    files: list[Path], transcript_dir: Path, days: int
) -> dict[str, str | None]:
    """Map each memory filename to the most recent transcript date that mentions its slug.

    A memory's slug is its filename without `.md`. Sessions cite a memory by that slug (the
    `name:` field and the index link agree with it), so searching for it is a sound test for
    "something surfaced this". It cannot distinguish a memory that was surfaced from one that
    was acted on — that distinction needs the model, and this is the mechanical half.

    Only `assistant` records are searched, and within them only text and thinking. Everything
    else in a transcript names slugs without citing them: the injected index arrives as an
    attachment, a directory listing of the store arrives as a tool result, and both would mark
    every entry referenced on the day someone merely looked at the folder. Measured here, the
    naive whole-line scan reported all 107 entries as referenced today — a signal that returns
    "everything is live" regardless of input is not a signal.

    Transcripts are scanned line by line and matched against every slug in one pass, because
    the store is ~100 files against ~800 MB of transcripts: re-reading per file would be a
    hundred passes over the same bytes.

    A line naming more than `_BULK_SLUG_LINE` slugs is skipped. `MEMORY.md` is injected verbatim
    into every session and therefore lands in every transcript, so without this every indexed
    entry reads as referenced today and the signal measures nothing but the injection itself.
    A real citation names one or two slugs; the index names a hundred in one blob. This is the
    difference between "something surfaced this entry" and "the index that lists it was pasted
    in again", and only the first is evidence the entry earns its place.
    """
    result: dict[str, str | None] = {f.name: None for f in files}
    if not transcript_dir.is_dir():
        return result

    cutoff = _dt.datetime.now().timestamp() - days * 86400
    slugs = {f.stem: f.name for f in files}

    transcripts = [
        p
        for p in transcript_dir.glob("*.jsonl")
        if p.is_file() and p.stat().st_mtime >= cutoff
    ]
    # Newest first, so the first hit for a slug is its most recent reference and later
    # transcripts for that slug can be skipped.
    transcripts.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    outstanding = set(slugs)
    for tpath in transcripts:
        if not outstanding:
            break
        day = _dt.datetime.fromtimestamp(tpath.stat().st_mtime).strftime("%Y-%m-%d")
        try:
            with tpath.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not outstanding:
                        break
                    text = _assistant_text(line)
                    if not text:
                        continue
                    hits = [slug for slug in outstanding if slug in text]
                    if len(hits) > _BULK_SLUG_LINE:
                        continue
                    for slug in hits:
                        result[slugs[slug]] = day
                        outstanding.discard(slug)
        except OSError:
            # An unreadable transcript is missing evidence, not a survey failure. Leaving
            # the slug outstanding makes it look unreferenced, which is the safe direction:
            # it surfaces for review rather than being silently retired.
            continue
    return result


# A path that looks like a check this repo could own. Deliberately narrow: a memory naming
# `ansible/roles/k8s/traefik/templates/deployment.yaml.j2` is citing the thing it describes, not
# a check that enforces it.
_CHECK_PATH = re.compile(
    r"(?:ansible/tests/[\w./-]+\.py"
    r"|scripts/[\w./-]*test_[\w./-]+\.py"
    r"|scripts/validate/[\w./-]+\.py"
    r"|\.claude/hooks/[\w./-]+\.(?:py|sh)"
    r"|[\w./-]*files/test_[\w./-]+\.py)"
)


def _resolves(cited: str, repo_root: Path) -> bool:
    """Whether a cited check path exists, under the repo OR the user's `~/.claude` tree.

    `.claude/hooks/` is the one prefix that lives in both places: this repo has its own hooks,
    and chezmoi deploys others to `~/.claude/hooks/`. Resolving against the repo alone reports a
    hook that exists as DANGLING — which is the survey's loudest verdict, and claiming a memory
    has lost its enforcer when it has not is worse than not counting it at all.
    """
    if (repo_root / cited).exists():
        return True
    return cited.startswith(".claude/") and (Path.home() / cited).exists()


def enforcement(files: list[Path], repo_root: Path) -> dict[str, list[str]]:
    """Which memory files name a check that exists, and which name none.

    The repo's own ladder is run-local note -> memory -> CLAUDE.md rule -> executable check,
    and MEMORY.md marks a handful of entries ENFORCED. Nothing counts the rest, so the ladder
    is aspirational: there is no way to ask which memories are still carried by an agent
    remembering them.

    This is a PROXY, and a deliberately weak one. It reports whether a memory cites a check
    path that resolves, which is evidence the fact has an owner — not proof the check tests
    what the memory claims. A cited path that no longer exists is the more interesting half:
    the memory says it is enforced and the enforcer is gone.
    """
    enforced, unenforced, dangling = [], [], []
    for path in files:
        cited = sorted(set(_CHECK_PATH.findall(_read(path))))
        if not cited:
            unenforced.append(path.name)
            continue
        if any(_resolves(c, repo_root) for c in cited):
            enforced.append(path.name)
        else:
            dangling.append(f"{path.name} -> {', '.join(cited)}")
    return {"enforced": enforced, "unenforced": unenforced, "dangling": dangling}


def survey(
    memory_dir: Path,
    transcript_dir: Path,
    transcript_days: int,
    duplicate_threshold: float = 0.12,
) -> dict:
    index_path = memory_dir / "MEMORY.md"
    files = sorted(
        p for p in memory_dir.glob("*.md") if p.is_file() and p.name != "MEMORY.md"
    )

    linked = index_links(index_path)
    on_disk = {p.name for p in files}

    index_bytes = index_path.stat().st_size if index_path.exists() else 0
    store_bytes = sum(p.stat().st_size for p in files)

    refs = last_referenced(files, transcript_dir, transcript_days)
    today = _dt.date.today()

    entries = []
    for p in files:
        seen = refs[p.name]
        age_days = None
        if seen:
            age_days = (today - _dt.date.fromisoformat(seen)).days
        entries.append(
            {
                "file": p.name,
                "bytes": p.stat().st_size,
                "modified": _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime(
                    "%Y-%m-%d"
                ),
                "last_referenced": seen,
                "days_since_referenced": age_days,
                "indexed": p.name in linked,
            }
        )
    entries.sort(key=lambda e: e["bytes"], reverse=True)

    return {
        "memory_dir": str(memory_dir),
        "index": {
            "bytes": index_bytes,
            "est_tokens": _est_tokens(index_bytes),
            "pointer_links": len(linked),
        },
        "store": {
            "files": len(files),
            "bytes": store_bytes,
            "est_tokens": _est_tokens(store_bytes),
        },
        "dead_links": sorted(n for n in linked if n not in on_disk),
        "orphans": sorted(n for n in on_disk if n not in linked),
        "unreferenced": sorted(
            e["file"] for e in entries if e["last_referenced"] is None
        ),
        "duplicate_candidates": duplicate_candidates(files, duplicate_threshold),
        "enforcement": enforcement(files, REPO_ROOT),
        "entries": entries,
        "transcript_window_days": transcript_days,
    }


def _render(s: dict) -> str:
    out: list[str] = []
    idx, store = s["index"], s["store"]
    out.append(f"memory store: {s['memory_dir']}")
    out.append(
        f"  injected index : {idx['bytes']:>8,} bytes  (~{idx['est_tokens']:,} tokens, "
        f"every session)  {idx['pointer_links']} links"
    )
    out.append(
        f"  store on disk  : {store['bytes']:>8,} bytes  (~{store['est_tokens']:,} tokens) "
        f"across {store['files']} files"
    )

    if s["dead_links"]:
        out.append(
            f"\nDEAD INDEX LINKS ({len(s['dead_links'])}) — indexed, not on disk:"
        )
        out.extend(f"  {n}" for n in s["dead_links"])

    if s["orphans"]:
        out.append(f"\norphans ({len(s['orphans'])}) — on disk, nothing links to them:")
        out.extend(f"  {n}" for n in s["orphans"])

    win = s["transcript_window_days"]
    if s["unreferenced"]:
        out.append(
            f"\nunreferenced ({len(s['unreferenced'])}) — no mention in {win}d of transcripts:"
        )
        out.extend(f"  {n}" for n in s["unreferenced"])

    if s["duplicate_candidates"]:
        out.append(f"\nnear-duplicate candidates ({len(s['duplicate_candidates'])}):")
        out.extend(
            f"  {score:.3f}  {a}  <->  {b}" for a, b, score in s["duplicate_candidates"]
        )

    enf = s["enforcement"]
    total = len(enf["enforced"]) + len(enf["unenforced"]) + len(enf["dangling"])
    out.append(
        f"\nenforcement: {len(enf['enforced'])}/{total} memories cite a check that exists"
    )
    if enf["dangling"]:
        out.append(
            f"  DANGLING ({len(enf['dangling'])}) — cites a check that is GONE, so the memory "
            "claims an owner it no longer has (searched the repo, and ~/ for a .claude path):"
        )
        out.extend(f"    {n}" for n in enf["dangling"])
    if enf["unenforced"]:
        out.append(
            f"  unenforced ({len(enf['unenforced'])}) — carried by an agent remembering them. "
            "Candidates for the next rung of the ladder, not a defect list:"
        )
        out.extend(f"    {n}" for n in enf["unenforced"])

    out.append("\nlargest entries:")
    for e in s["entries"][:15]:
        seen = e["last_referenced"] or "never"
        flag = "" if e["indexed"] else "  [orphan]"
        out.append(f"  {e['bytes']:>7,}b  seen {seen:>10}  {e['file']}{flag}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY_DIR)
    ap.add_argument("--transcript-dir", type=Path, default=DEFAULT_TRANSCRIPT_DIR)
    ap.add_argument(
        "--transcript-days",
        type=int,
        default=30,
        help="how far back to scan transcripts for a reference (default: 30)",
    )
    ap.add_argument(
        "--duplicate-threshold",
        type=float,
        default=0.12,
        help="minimum shingle overlap to report a pair as a near-duplicate (default: 0.12). "
        "Lower it to explore a store that reports none — a detector you cannot tune is one "
        "you cannot tell apart from a detector that never fires.",
    )
    ap.add_argument("--json", action="store_true", help="emit the survey as JSON")
    args = ap.parse_args(argv)

    if not args.memory_dir.is_dir():
        print(f"no such memory dir: {args.memory_dir}", file=sys.stderr)
        return 2

    s = survey(
        args.memory_dir,
        args.transcript_dir,
        args.transcript_days,
        args.duplicate_threshold,
    )
    print(json.dumps(s, indent=2) if args.json else _render(s))

    # Only a dead link is a defect: the index promises a file that is not there, so a
    # session following that pointer gets nothing. Everything else needs a judgement.
    return 1 if s["dead_links"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
