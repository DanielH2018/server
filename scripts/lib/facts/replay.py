"""The notice replay in ``fact_status.py report``: whether the diff notice lists every lock move.

``notice.moved_sections`` runs once per first-parent commit, with the parent as the base, and
every content-form lock move that ``report.precision`` walks is graded by whether the replay
listed it. That parity decides whether the notice can replace ``docs/facts.lock`` (G2), so the
replay always runs today's notice and hashing code over the whole window. The lock moves were
each hashed by their own commit's code, so ``content_lock_moves`` does not change when that
code does.

The plan is ``docs/target-architecture/docs-guard/g1-diff-notice.md``.
"""

import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.git import git

from .notice import Notice, moved_sections

if TYPE_CHECKING:
    from .report import Precision


@dataclass(frozen=True)
class LockMove:
    """One potential: a recorded atom whose hash a lock commit changed.

    Attributes:
      commit, parent: the lock commit and its first parent.
      committed_at: the commit time in UTC, as ``2026-10-11T12:00:00Z``.
      recorded: the lock commit that recorded the atom's previous hash, or None when that
        predates the walk.
    """

    commit: str
    parent: str
    committed_at: str
    unit: str
    atom: str
    form: str
    prose_edited: bool
    recorded: str | None


# A change to any of these can move a lock hash with no change to the atom it hashes.
HASHING_PATHS = frozenset(
    {
        "scripts/lib/facts/atoms.py",
        "scripts/lib/facts/citations.py",
        "scripts/lib/yaml_fast.py",
    }
)
CLASSES = ("listed", "hashing", "split", "miss")


def _pyyaml_at(repo: Path, rev: str) -> str | None:
    shown = git("show", f"{rev}:uv.lock", cwd=repo, check=False)
    if shown.returncode != 0:
        return None
    packages = tomllib.loads(shown.stdout).get("package", [])
    return next((p.get("version") for p in packages if p.get("name") == "pyyaml"), None)


def _hashing_change(repo: Path, parent: str, sha: str) -> bool:
    """Whether ``sha`` touched the hashing code or changed the ``pyyaml`` version in ``uv.lock``."""
    names = git("diff", "--no-renames", "--name-only", parent, sha, cwd=repo).stdout
    touched = set(names.splitlines())
    if touched & HASHING_PATHS:
        return True
    return "uv.lock" in touched and _pyyaml_at(repo, parent) != _pyyaml_at(repo, sha)


def notice_replay(
    repo: Path,
    prec: Precision,
    notice: Callable[[Path, str, str], list[Notice]] = moved_sections,
) -> dict:
    """Replay ``notice`` over every first-parent commit from ``prec.start`` to HEAD, and grade parity.

    ``prec.start`` is replayed too, so a move split across the window's edge keeps its first
    half. With no ``start`` the replay covers the window. An exception from ``notice`` on one
    commit becomes a ``crashed`` entry and the replay goes on.

    Each content-form lock move takes the first class that applies, from ``CLASSES``:
    ``listed`` when the replay listed its unit and atom at the lock commit; ``hashing`` when
    that commit touched ``HASHING_PATHS`` or the ``pyyaml`` version; ``split`` when the replay
    listed them at an earlier commit after the one that recorded the atom's previous hash;
    ``miss`` otherwise. Only ``miss`` counts against parity.
    """
    fmt = "--format=%H %P"
    if prec.start:
        lines = git(
            "log", "--first-parent", fmt, f"{prec.start}..HEAD", cwd=repo
        ).stdout
        lines += git("log", "-1", fmt, prec.start, cwd=repo).stdout
    else:
        since = f"--since={prec.since.isoformat()}"
        lines = git("log", "--first-parent", since, fmt, cwd=repo).stdout
    order: dict[str, int] = {}
    listed: dict[tuple[str, str], list[int]] = {}
    kinds, edited = Counter(), Counter()
    crashed, content_notices = [], []
    with_notice = moved = moved_edited = 0
    for pos, line in enumerate(reversed(lines.splitlines())):
        sha, *parents = line.split()
        order[sha] = pos
        if not parents:
            continue
        try:
            rows = notice(repo, parents[0], sha)
        except Exception as e:  # a crash is a finding, not a stop
            crashed.append({"commit": sha, "error": type(e).__name__})
            continue
        with_notice += bool(rows)
        for r in rows:
            kinds[r.kind] += 1
            edited[r.kind] += r.prose_edited
            listed.setdefault((r.unit, r.atom), []).append(pos)
            if r.kind == "moved":
                moved += 1
                moved_edited += r.prose_edited
            if r.form != "path":
                content_notices.append({"commit": sha, "unit": r.unit})

    def grade(m: LockMove) -> str:
        at = order.get(m.commit, -1)
        seen = listed.get((m.unit, m.atom), [])
        if at in seen:
            return "listed"
        if _hashing_change(repo, m.parent, m.commit):
            return "hashing"
        lo = order.get(m.recorded, -1) if m.recorded else -1
        return "split" if any(lo < p < at for p in seen) else "miss"

    content = [m for m in prec.moves if m.form != "path"]
    return {
        "start": prec.start,
        "commits": len(order),
        "commits_with_notice": with_notice,
        "notices": dict(sorted(kinds.items())),
        "prose_edited": dict(sorted(edited.items())),
        "moved": moved,
        "moved_prose_edited": moved_edited,
        "crashed": crashed,
        "content_lock_moves": [
            {
                "commit": m.commit,
                "committed_at": m.committed_at,
                "unit": m.unit,
                "atom": m.atom,
                "class": grade(m),
            }
            for m in content
        ],
        "content_notices": content_notices,
        "path_lock_moves": [
            {"commit": m.commit, "unit": m.unit, "atom": m.atom}
            for m in prec.moves
            if m.form == "path"
        ],
        "python_changed": [{"commit": c, "unit": u} for c, u in prec.python_changed],
    }


def _by_kind(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {n}" for k, n in counts.items()) or "none"


def render_replay(replay: dict) -> list[str]:
    start = (
        f"from lock commit {replay['start'][:12]}, the last before the window"
        if replay["start"]
        else "over the window, which the lock is younger than"
    )
    moves = replay["content_lock_moves"]
    classes = Counter(m["class"] for m in moves)
    lines = [
        "",
        f"Notice replay {start}",
        f"  commits in the window       {replay['commits']}",
        f"  commits carrying a notice   {replay['commits_with_notice']}",
        f"  notices, by kind            {_by_kind(replay['notices'])}",
        f"  prose-edited, by kind       {_by_kind(replay['prose_edited'])}",
        f"  moved rows                  {replay['moved']}"
        f" ({replay['moved_prose_edited']} with the section's prose edited)",
        f"  crashes                     {len(replay['crashed'])}",
    ]
    lines += [f"    {c['commit'][:12]}  {c['error']}" for c in replay["crashed"]]
    lines.append(
        f"  content-form lock moves     {len(moves)}: "
        + ", ".join(f"{c} {classes[c]}" for c in CLASSES)
    )
    lines += [
        f"    {m['class']:<8}{m['commit'][:12]}  {m['committed_at']}  {m['unit']}  {m['atom']}"
        for m in moves
        if m["class"] != "listed"
    ]
    lines.append(
        f"  path-form lock moves        {len(replay['path_lock_moves'])} (context:"
        " a path atom hashes its path, so no diff moves it)"
    )
    lines += [
        f"    {m['commit'][:12]}  {m['unit']}  {m['atom']}"
        for m in replay["path_lock_moves"]
    ]
    python = Counter(r["commit"] for r in replay["python_changed"])
    lines.append(
        f"  lock commits where a row's python changed  {len(python)}"
        " (context: precision skips their rows)"
    )
    lines += [f"    {c[:12]}  {n} rows" for c, n in python.items()]
    return lines
