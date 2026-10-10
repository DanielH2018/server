"""Rank repo docs by how often they load, and one doc's sections by how often they are read.

`ablate.py rank` uses these to choose what to ablate first. instructions.log records which
doc loaded, never which section was read (#4302). A session's transcript records what it
wrote, so a heading the assistant quotes, cites or searches for is the measurable trace of a
read. `ablate.py rank --sections` prints that ranking, and `ablate.py run --by-reads`
ablates the most-read sections first.
"""

import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path


def rank_docs(log_lines: list[str]) -> list[tuple[str, int]]:
    """Count instructions.log loads per repo-relative doc, most-loaded first.

    A log line is `<ts> [<session>] <reason> <scope> <path> [trigger=...]`. Absolute paths
    are the user-level docs the dotfiles repo owns, so they are dropped here. The log
    records whole docs, never headings; section_reads ranks the sections of one doc.
    """
    counts = Counter()
    for line in log_lines:
        fields = line.split()
        if len(fields) < 5 or fields[4].startswith("/"):
            continue
        counts[fields[4]] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def heading_key(heading: str) -> str:
    """The part of a heading a session would quote: the text before an em-dash or a bracket.

    "Shell Commands — Shape Them to Auto-Approve" is cited as "Shell Commands", and
    "Claude Tooling in This Repo (`.claude/`)" as "Claude Tooling in This Repo".
    """
    return re.split(r"\s+(?:—|\()", heading, maxsplit=1)[0].strip()


def _assistant_text(record: dict) -> str:
    """The text an assistant turn wrote: its prose, its thinking and its tool call inputs."""
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "thinking":
            parts.append(block.get("thinking", ""))
        elif block.get("type") == "tool_use":
            parts.append(json.dumps(block.get("input"), ensure_ascii=False))
    return "\n".join(parts)


def section_reads(
    headings: list[str], transcripts: Iterable[Path]
) -> list[tuple[str, int]]:
    """Rank a doc's `## ` headings by how many sessions' assistant turns name them.

    Only what the assistant wrote counts. A tool result, a user turn and hook-injected context
    carry the whole doc whenever it loads, so counting them would measure loading, not reading.
    Each session counts once per heading, so one long session cannot outweigh many short
    ones. A subagent's transcript, `<session>/subagents/<agent>.jsonl`, counts as its parent
    session's, so a fan-out of ten reviewers citing one heading counts once. A heading
    matches on its heading_key, case-sensitively. Ties keep doc order.
    """
    keys = {h: heading_key(h) for h in headings}
    by_session: dict[Path, set[str]] = {}
    for path in transcripts:
        session = (
            path.parent.parent
            if path.parent.name == "subagents"
            else path.with_suffix("")
        )
        found = by_session.setdefault(session, set())
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    # Cheap prefilter: most lines are tool results and user turns.
                    if '"assistant"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") != "assistant":
                        continue
                    text = _assistant_text(record)
                    found |= {h for h in headings if h not in found and keys[h] in text}
        except OSError:
            continue
    counts = dict.fromkeys(headings, 0)
    for found in by_session.values():
        for h in found:
            counts[h] += 1
    order = {h: i for i, h in enumerate(headings)}
    return sorted(counts.items(), key=lambda kv: (-kv[1], order[kv[0]]))


def default_transcripts(root: Path) -> list[Path]:
    """Every transcript of a session in the checkout at `root` or one of its worktrees."""
    # Claude Code names a project's transcript directory after its path, with every
    # non-alphanumeric character replaced by "-"; a worktree's directory extends the primary's.
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(root))
    projects = Path.home() / ".claude" / "projects"
    return sorted(p for d in projects.glob(f"{slug}*") for p in d.rglob("*.jsonl"))
