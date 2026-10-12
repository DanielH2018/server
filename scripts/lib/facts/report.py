"""``fact_status.py report``: whether ``docs/facts.lock`` pays for itself, read from git and the log.

Three measurements, none kept in a store of its own.

- **Potential against actual, per atom form.** A *potential* is a lock commit in which a
  recorded atom's hash changed. It is *actual* when the same commit also changed the owning
  section's prose, which is the case the lock exists to force. The ratio is the lock's
  precision, which every judgement of it so far (#2808, #2817, ``96365b0b5``) re-derived by
  hand from ``git log -p``.
- **The non-IN sections, ranked by reads.** Every section that is not IN, ranked by how often
  its doc loaded into a session according to ``.claude/logs/instructions.log``, so the backlog
  is worked in the order agents read it. The log has one row per loaded doc, so a section
  inherits its doc's count.
- **The instruction bytes each session loaded.** ``log-instructions.py`` ends each row with
  ``bytes=<n>``, the loaded file's size, and ``inject-nested-docs.py`` ends each
  ``bash_path_match`` row with the size of the block it injected (#4653). The report sums them
  per session id, user-level files outside the repo included (#4610). That is the always-loaded context's trend, which
  otherwise shows only when someone runs ``wc -c`` by hand.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.git import git
from lib.repo_paths import primary_checkout_of

from .citations import parse_citations
from .lint import bodies_at
from .lock import LOCK_REL, parse_lock

FORMS = ("path", "symbol", "yaml", "test", "marker")
LOG_REL = ".claude/logs/instructions.log"


@dataclass
class FormCount:
    potential: int = 0
    actual: int = 0


@dataclass
class Precision:
    """Per-form counts over the lock commits in a window."""

    since: datetime
    lock_commits: int = 0
    forms: dict[str, FormCount] = field(
        default_factory=lambda: {f: FormCount() for f in FORMS}
    )


def atom_form(atom: str) -> str | None:
    """The citation form of a recorded atom key, or None when it no longer parses as one."""
    cites, _ = parse_citations(f"`{atom}`")
    return cites[0].form if cites else None


def _lock_at(repo: Path, rev: str) -> dict[str, dict] | None:
    shown = git("show", f"{rev}:{LOCK_REL}", cwd=repo, check=False)
    return parse_lock(shown.stdout) if shown.returncode == 0 else None


def _prose_changed(
    repo: Path,
    parent: str,
    sha: str,
    unit: str,
    bodies: dict[tuple[str, str], dict[str, str]],
) -> bool:
    """Whether ``sha`` changed the section ``unit`` against ``parent``; ``bodies`` caches each doc read."""
    doc = unit.partition("#")[0]
    for rev in (parent, sha):
        if (rev, doc) not in bodies:
            bodies[(rev, doc)] = bodies_at(repo, rev, doc)
    return bodies[(parent, doc)].get(unit) != bodies[(sha, doc)].get(unit)


def precision(repo: Path, days: int, now: datetime | None = None) -> Precision:
    """Count potential and actual moves in every lock commit of the last ``days`` days.

    A commit is compared with its first parent. Three kinds of hash change are not potentials:
    a row the commit created (nothing was recorded before it), a row whose ``python`` changed
    (an interpreter bump moves every symbol and test hash at once and says nothing about the
    prose), and the commit that created the lock.

    No row's ``verified_sha`` is read. That is often a branch commit a squash merge dropped,
    and the walk needs none: it follows the lock's own first-parent history on HEAD, where
    each squash commit carries the lock change and the prose change together.
    """
    now = now or datetime.now(timezone.utc)
    out = Precision(since=now - timedelta(days=days))
    bodies: dict[tuple[str, str], dict[str, str]] = {}
    log = git(
        "log",
        "--first-parent",
        f"--since={out.since.isoformat()}",
        "--format=%H %P",
        "--",
        LOCK_REL,
        cwd=repo,
    ).stdout
    for line in log.splitlines():
        sha, *parents = line.split()
        if not parents:
            continue
        before, after = _lock_at(repo, parents[0]), _lock_at(repo, sha)
        if before is None or after is None:
            continue
        out.lock_commits += 1
        for unit, rec in sorted(after.items()):
            old = before.get(unit)
            if old is None or old.get("python") != rec.get("python"):
                continue
            moved = [
                a
                for a, h in rec.get("atoms", {}).items()
                if a in old.get("atoms", {}) and old["atoms"][a] != h
            ]
            if not moved:
                continue
            actual = _prose_changed(repo, parents[0], sha, unit, bodies)
            for atom in moved:
                form = atom_form(atom)
                if form not in out.forms:
                    continue
                out.forms[form].potential += 1
                out.forms[form].actual += int(actual)
    return out


_ROW = re.compile(r"^(\S+) \[[^\]]*\]\s+\S+\s+\S+\s+(\S+)")
_WORKTREE = re.compile(r"/\.claude/worktrees/[^/]+/")


@dataclass
class Reads:
    """Load counts per repo doc, and the span of the log rows that fed them."""

    counts: Counter = field(default_factory=Counter)
    rows: int = 0
    first: str = ""
    last: str = ""


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
    for path in (log.with_name(log.name + ".1"), log):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
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
    is in the total. The total is a floor, not the whole context: the harness fires no
    InstructionsLoaded event for the auto-memory ``MEMORY.md`` or the output style, so neither
    has a row (#4610 measured them at about 20 KB of its 61.7 KB). ``total`` also counts ``compact`` reloads and path-scoped loads, so it is
    what entered context, not what a session started with; ``start`` is that.

    Two kinds of row are left out. A row tagged ``agent=`` is a subagent's load into its own
    context window, though it carries its parent's session id. A row with no ``bytes=`` field
    predates the field, and a session with no sized row at all is not listed rather than
    listed as 0. A ``bash_path_match`` row counts the block the hook injected, which for a doc
    over its budget is the head rather than the whole file.
    """
    cutoff = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    by_session: dict[str, SessionBytes] = {}
    for path in (log.with_name(log.name + ".1"), log):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
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


def ranked_backlog(
    statuses: dict[str, str], reads: Reads
) -> list[tuple[str, str, int]]:
    """Every section not IN as ``(section, status, reads)``, the most-read doc first."""
    rows = [
        (unit, status, reads.counts.get(unit.partition("#")[0], 0))
        for unit, status in statuses.items()
        if status != "IN"
    ]
    return sorted(rows, key=lambda r: (-r[2], r[0]))


def build(
    repo: Path,
    statuses: dict[str, str],
    days: int,
    log: Path,
    now: datetime | None = None,
) -> dict:
    """Both measurements as one JSON-ready mapping; ``render`` prints the same thing as text."""
    now = now or datetime.now(timezone.utc)
    prec = precision(repo, days, now)
    docs = {u.partition("#")[0] for u in statuses}
    roots = tuple(
        sorted({str(repo.resolve()), str(primary_checkout_of(repo).resolve())})
    )
    reads = doc_reads(log, docs, prec.since, roots)
    sessions = session_bytes(log, prec.since)
    return {
        "window_days": days,
        "since": prec.since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lock_commits": prec.lock_commits,
        "forms": {
            f: {"potential": c.potential, "actual": c.actual}
            for f, c in prec.forms.items()
        },
        "log": {
            "path": str(log),
            "rows": reads.rows,
            "first": reads.first,
            "last": reads.last,
        },
        "backlog": [
            {"section": u, "status": s, "reads": n}
            for u, s, n in ranked_backlog(statuses, reads)
        ],
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
    }


def render(report: dict, top: int) -> str:
    """The text form of ``build``'s mapping, cut to ``top`` rows per list.

    The per-form table comes first, then the ``top`` most-read sections that are not IN, then
    the instruction bytes of the ``top`` most recent sessions.
    """
    lines = [
        f"Lock precision since {report['since']} "
        f"({report['window_days']} days, {report['lock_commits']} lock commits)",
        f"  {'form':<8}{'potential':>10}{'actual':>8}  ratio",
    ]
    for form, c in report["forms"].items():
        ratio = f"{c['actual'] / c['potential']:.2f}" if c["potential"] else "-"
        lines.append(f"  {form:<8}{c['potential']:>10}{c['actual']:>8}  {ratio}")
    # A path atom hashed the file's bytes until 96365b0b5 (2026-09-28) and hashes its path since,
    # so a path potential in the window is that commit's rehash or an edit from before it.
    lines.append(
        "  Since 96365b0b5 a path atom hashes its path, not the file: a path potential"
        " predates it or is its one-time rehash."
    )
    log = report["log"]
    span = f"{log['first']} to {log['last']}" if log["rows"] else "no rows"
    lines += [
        "",
        f"Sections not IN, by doc reads ({log['rows']} rows, {span}, {log['path']})",
        f"  {'reads':>5}  {'status':<10}  section",
    ]
    lines += [
        f"  {r['reads']:>5}  {r['status']:<10}  {r['section']}"
        for r in report["backlog"][:top]
    ]
    rest = len(report["backlog"]) - top
    if rest > 0:
        lines.append(f"  ... {rest} more; --top or --json lists them")
    sessions = report["sessions"]
    lines += [
        "",
        f"Instruction bytes loaded per session ({len(sessions)} sessions with sized rows;"
        " MEMORY.md and the output style log no row, so they are not counted)",
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
    return "\n".join(lines)
