"""``fact_status.py report``: the notice's yield and the docs' load, read from git and the log.

Four measurements, none kept in a store of its own.

- **The sections citing nothing, ranked by reads.** Every section with no in-tree citation,
  a CONVENTION section, ranked by how often its doc loaded into a session according to
  ``.claude/logs/instructions.log``, so the backlog is worked in the order agents read it. The
  log has one row per loaded doc, so a section inherits its doc's count.
- **The docs read as a head, ranked by reads.** ``inject-nested-docs.py`` tags the row of a doc
  it delivered as a head with ``form=head``. The count per doc is the order a trim should follow.
- **The instruction bytes each session loaded.** ``log-instructions.py`` ends each row with
  ``bytes=<n>``, the loaded file's size, and ``inject-nested-docs.py`` ends each
  ``bash_path_match`` row with the size of the block it injected (#4653). The report sums them
  per session id, user-level files outside the repo included (#4610). That is the always-loaded context's trend, which
  otherwise shows only when someone runs ``wc -c`` by hand.
- **The notice replay.** ``notice.moved_sections`` runs once per first-parent commit in the
  ``--days`` window, with the parent as the base (``replay.notice_replay``).
  ``docs/adr/0023-fact-support-is-a-diff-notice.md`` records why the notice replaced the lock.
"""

import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.repo_paths import primary_checkout_of

from .citations import Citations
from .notice import Notice, moved_sections
from .replay import notice_replay, render_replay

LOG_REL = ".claude/logs/instructions.log"
_ROW = re.compile(r"^(\S+) \[[^\]]*\]\s+\S+\s+\S+\s+(\S+)")
_WORKTREE = re.compile(r"/\.claude/worktrees/[^/]+/")
_FORM_HEAD = re.compile(r"(?<!\S)form=head(?!\S)")


@dataclass
class Reads:
    """Load counts per repo doc, and the span of the log rows that fed them."""

    counts: Counter = field(default_factory=Counter)
    rows: int = 0
    first: str = ""
    last: str = ""


def _log_lines(log: Path) -> Iterator[str]:
    """The lines of ``log``'s rotated ``.1`` and then of ``log``; a missing file has none."""
    for path in (log.with_name(log.name + ".1"), log):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        yield from text.splitlines()


def default_log(repo: Path) -> Path:
    """The injection log every checkout writes: ``.claude/logs/`` in the primary checkout."""
    return primary_checkout_of(repo) / LOG_REL


def _repo_relative(path: str, roots: tuple[str, ...]) -> str:
    """A log row's path as the repo names it: a worktree or checkout prefix is dropped."""
    if not path.startswith("/"):
        return path
    m = _WORKTREE.search(path)
    if m:
        return path[m.end() :]
    for root in roots:
        if path.startswith(root + "/"):
            return path[len(root) + 1 :]
    return path


def doc_reads(
    log: Path, docs: set[str], since: datetime, roots: tuple[str, ...] = ()
) -> Reads:
    """Count each doc in ``docs`` over the rows of ``log`` and its rotated ``.1`` since ``since``.

    A missing file counts as no rows: a fresh checkout has no log yet, and the log rotates at
    256 KiB, so ``.1`` exists only once it has. The span says how far back the rows actually
    reach, which is usually days, not the whole window.
    """
    out = Reads()
    cutoff = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    stamps: list[str] = []
    for line in _log_lines(log):
        m = _ROW.match(line)
        if not m or m.group(1) < cutoff:
            continue
        rel = _repo_relative(m.group(2), roots)
        if rel in docs:
            out.counts[rel] += 1
            out.rows += 1
            stamps.append(m.group(1))
    if stamps:
        out.first, out.last = min(stamps), max(stamps)
    return out


def head_reads(log: Path, since: datetime) -> list[tuple[str, int]]:
    """Each doc delivered as a head since ``since`` as ``(doc, reads)``, the most-read first.

    A read is one ``form=head`` row. The doc's whole-form rows do not count, and a subagent's
    head does, since a subagent reads the head too. No doc set filters the rows: the hook also
    delivers ``.claude/rules/*.md`` as heads, and those have no section in the citations.
    """
    cutoff = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    counts: Counter = Counter()
    for line in _log_lines(log):
        m = _ROW.match(line)
        if m and m.group(1) >= cutoff and _FORM_HEAD.search(line):
            counts[m.group(2)] += 1
    return sorted(counts.items(), key=lambda r: (-r[1], r[0]))


_SESSION_ROW = re.compile(r"^(\S+) \[([^\]]*)\]\s+(\S+)")
_BYTES = re.compile(r"(?<!\S)bytes=(\d+)(?!\S)")
_AGENT = re.compile(r"(?<!\S)agent=")


@dataclass
class SessionBytes:
    """The instruction bytes one session loaded: at ``session_start``, and over every load."""

    session: str
    first: str
    loads: int = 0
    start: int = 0
    total: int = 0


def session_bytes(log: Path, since: datetime) -> list[SessionBytes]:
    """Sum the ``bytes=`` fields of ``log`` and its rotated ``.1`` per session, oldest first.

    Every row counts whatever doc it names, so a user-level file such as ``~/.claude/CLAUDE.md``
    is in the total. The harness fires no InstructionsLoaded event for the auto-memory
    ``MEMORY.md`` or the output style, so ``session-health.py`` writes a ``session_start`` row
    for each at SessionStart (#4656), and a ``compact`` row for ``MEMORY.md`` at each
    compaction (#4668). ``total`` also counts ``compact`` reloads and
    path-scoped loads, so it is what entered context, not what a session started with;
    ``start`` is that.

    Two kinds of row are left out. A row tagged ``agent=`` is a subagent's load into its own
    context window, though it carries its parent's session id. A row with no ``bytes=`` field
    predates the field, and a session with no sized row at all is not listed rather than
    listed as 0. A ``bash_path_match`` row counts the block the hook injected, which for a doc
    over its budget is the head rather than the whole file.
    """
    cutoff = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    by_session: dict[str, SessionBytes] = {}
    for line in _log_lines(log):
        m = _SESSION_ROW.match(line)
        size = _BYTES.search(line)
        if not m or not size or m.group(1) < cutoff or _AGENT.search(line):
            continue
        ts, sid, reason = m.group(1), m.group(2).strip(), m.group(3)
        row = by_session.setdefault(sid, SessionBytes(session=sid, first=ts))
        row.first = min(row.first, ts)
        row.loads += 1
        row.total += int(size.group(1))
        if reason == "session_start":
            row.start += int(size.group(1))
    return sorted(by_session.values(), key=lambda r: (r.first, r.session))


def ranked_backlog(by_unit: Citations, reads: Reads) -> list[tuple[str, int]]:
    """Every section citing nothing as ``(section, reads)``, the most-read doc first."""
    rows = [
        (unit, reads.counts.get(unit.partition("#")[0], 0))
        for unit, cites in by_unit.items()
        if not cites
    ]
    return sorted(rows, key=lambda r: (-r[1], r[0]))


def build(
    repo: Path,
    by_unit: Citations,
    days: int,
    log: Path,
    now: datetime | None = None,
    notice: Callable[[Path, str, str], list[Notice]] = moved_sections,
) -> dict:
    """Every measurement as one JSON-ready mapping; ``render`` prints the same thing as text."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    docs = {u.partition("#")[0] for u in by_unit}
    roots = tuple(
        sorted({str(repo.resolve()), str(primary_checkout_of(repo).resolve())})
    )
    reads = doc_reads(log, docs, since, roots)
    sessions = session_bytes(log, since)
    return {
        "window_days": days,
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "log": {
            "path": str(log),
            "rows": reads.rows,
            "first": reads.first,
            "last": reads.last,
        },
        "backlog": [
            {"section": u, "reads": n} for u, n in ranked_backlog(by_unit, reads)
        ],
        "heads": [{"doc": doc, "reads": n} for doc, n in head_reads(log, since)],
        "sessions": [
            {
                "session": r.session,
                "first": r.first,
                "loads": r.loads,
                "start_bytes": r.start,
                "total_bytes": r.total,
            }
            for r in sessions
        ],
        "notice_replay": notice_replay(repo, since, notice),
    }


def render(report: dict, top: int) -> str:
    """The text form of ``build``'s mapping, cut to ``top`` rows per list.

    The ``top`` most-read sections citing nothing come first, then the ``top`` most-read docs
    delivered as a head, then the instruction bytes of the ``top`` most recent sessions, then
    the notice replay, which names every crash whatever ``top`` says.
    """
    log = report["log"]
    span = f"{log['first']} to {log['last']}" if log["rows"] else "no rows"
    lines = [
        f"Sections citing nothing since {report['since']}, by doc reads"
        f" ({log['rows']} rows, {span}, {log['path']})",
        f"  {'reads':>5}  section",
    ]
    lines += [f"  {r['reads']:>5}  {r['section']}" for r in report["backlog"][:top]]
    rest = len(report["backlog"]) - top
    if rest > 0:
        lines.append(f"  ... {rest} more; --top or --json lists them")
    heads = report["heads"]
    lines += ["", "Docs read as a head, by reads", f"  {'reads':>5}  doc"]
    lines += [f"  {r['reads']:>5}  {r['doc']}" for r in heads[:top]]
    rest = len(heads) - top
    if rest > 0:
        lines.append(f"  ... {rest} more; --top or --json lists them")
    sessions = report["sessions"]
    lines += [
        "",
        f"Instruction bytes loaded per session ({len(sessions)} sessions with sized rows)",
        f"  {'session':<8}  {'first load':<20}  {'loads':>5}  {'at start':>10}  {'total':>10}",
    ]
    lines += [
        f"  {r['session']:<8}  {r['first']:<20}  {r['loads']:>5}"
        f"  {r['start_bytes']:>10,}  {r['total_bytes']:>10,}"
        for r in sessions[max(len(sessions) - top, 0) :]
    ]
    rest = len(sessions) - top
    if rest > 0:
        lines.append(f"  ... {rest} older; --top or --json lists them")
    return "\n".join(lines + render_replay(report["notice_replay"]))
