"""Diff evidence for a ``moved`` finding: which removed identifiers the section's prose names.

A ``moved`` finding says only that a cited atom's hash changed. Most such moves are refactors
that leave the prose true, and the author needs a cue to tell one from a change the section
describes. Two kinds of evidence came out of replaying the lock's ten recorded content moves
from 2026-09-19 to 2026-10-10 (#4255):

- **Atom-scoped.** The identifier-shaped tokens the atom itself lost between the row's
  ``verified_sha`` and now. In ``e66aca65e`` the changed function stopped naming
  ``docs/reference/scripts.md``, which the section also named.
- **Range-wide.** The tokens removed from any non-Markdown file in ``verified_sha..HEAD`` that
  no longer occur anywhere in the tree. In ``a91ed2c6d`` the atom only renamed a local name,
  while the same commit renamed the defaults key the section named in backticks.

Evidence never changes a verdict: the finding stays ``moved`` and CI stays red until a person
re-reads the section and runs ``verify`` (#2817).
"""

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.git import git

from .atoms import Ambiguous, atom_content
from .citations import Citation, mask_history, tracked_files
from .removed import LOCK_PATH, identifiers, removed_tokens, still_present


@dataclass
class EvidenceCache:
    """What every ``moved`` finding against one tree shares: a base per hash, a range diff per base.

    Many rows share a base (a bulk verify records hundreds at one commit), and the range-wide
    diff is the expensive half, so it is read once per base.
    """

    removed: dict[str, set[str] | None] = field(default_factory=dict)
    bases: dict[str, str | None] = field(default_factory=dict)
    tracked: frozenset[str] | None = None


def _on_head(repo: Path, sha: str) -> bool:
    """Whether ``sha`` names a commit in HEAD's history."""
    if not sha:
        return False
    resolved = git(
        "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", cwd=repo, check=False
    )
    if resolved.returncode != 0:
        return False
    return (
        git(
            "merge-base", "--is-ancestor", sha, "HEAD", cwd=repo, check=False
        ).returncode
        == 0
    )


def _source_at(repo: Path, rev: str, path: str) -> str | None:
    shown = git("show", f"{rev}:{path}", cwd=repo, check=False)
    return shown.stdout if shown.returncode == 0 else None


def _hashes_as(c: Citation, source: str, recorded: str) -> bool:
    try:
        content = atom_content(c, source)
    except Ambiguous, SyntaxError:
        return False
    return (
        content is not None and hashlib.sha256(content.encode()).hexdigest() == recorded
    )


# How many lock commits that changed the count of a hash to try before giving up. A hash is
# written once per verify and removed once per re-verify, so a long list means a hash shared
# by many rows, such as a path cited from several sections, which is never a `moved` atom.
_MAX_WRITERS = 20


def evidence_base(
    repo: Path, c: Citation, verified_sha: str, recorded: str
) -> str | None:
    """The commit to diff a moved atom from: ``verified_sha`` when HEAD holds it, else the lock writer.

    DECIDED: ``verified_sha`` is usually not on master. A verify inside a PR records the
    branch's HEAD, and the squash merge leaves that commit out of master's history: on
    2026-10-10, 22 of the 55 distinct values in master's lock did not resolve, and one more was
    not an ancestor of master. The squash commit carries the lock change, though, so the
    fallback is the newest commit on HEAD's history whose ``docs/facts.lock`` diff adds or
    drops the recorded hash (``git log -S``) and whose tree still hashes the atom to it. That
    commit itself is the base, not its first parent: the verify and the code it read land in
    the one squash commit, so its tree is the state the hash was recorded from, and the
    re-hash proves it. ``None`` when no commit on HEAD qualifies.
    """
    if _on_head(repo, verified_sha):
        return verified_sha
    writers = git(
        "log",
        "--format=%H",
        f"-S{recorded}",
        "HEAD",
        "--",
        LOCK_PATH,
        cwd=repo,
        check=False,
    ).stdout.split()
    for sha in writers[:_MAX_WRITERS]:
        source = _source_at(repo, sha, c.path)
        if source is not None and _hashes_as(c, source, recorded):
            return sha[:9]
    return None


def _atom_tokens(c: Citation, old_source: str | None, new_source: str) -> set[str]:
    """The tokens the atom's content held at ``verified_sha`` and does not hold now."""

    def tokens(source: str | None) -> set[str]:
        if source is None:
            return set()
        try:
            content = atom_content(c, source)
        except Ambiguous, SyntaxError:
            return set()
        return identifiers(content) if content else set()

    return tokens(old_source) - tokens(new_source)


def moved_evidence(
    repo: Path,
    c: Citation,
    verified_sha: str,
    recorded: str,
    prose: str,
    cache: EvidenceCache,
) -> str:
    """A clause naming the removed identifiers the section's ``prose`` names, or saying it names none.

    The diff runs from ``evidence_base``: the row's ``verified_sha`` when HEAD holds it, else
    the commit that wrote ``recorded`` into the lock. Empty when neither is on HEAD (a shallow
    clone, a rewritten history) or git fails: the finding then keeps its plain message, never
    an error. A history paragraph or bullet is masked, since it names a gone thing by design.
    """
    try:
        if recorded not in cache.bases:
            cache.bases[recorded] = evidence_base(repo, c, verified_sha, recorded)
        base = cache.bases[recorded]
        if base is None:
            return ""
        old_source = _source_at(repo, base, c.path)
        target = repo / c.path
        new_source = target.read_text(encoding="utf-8") if target.is_file() else ""
        atom_removed = _atom_tokens(c, old_source, new_source)
        if base not in cache.removed:
            cache.removed[base] = removed_tokens(repo, base)
        range_removed = cache.removed[base] or set()
        named = identifiers(mask_history(prose))
        atom_hits = atom_removed & named
        candidates = sorted((range_removed & named) - atom_hits)
        if candidates and cache.tracked is None:
            cache.tracked = tracked_files(repo)
        gone = (
            set(candidates) - still_present(repo, candidates, cache.tracked)
            if candidates and cache.tracked is not None
            else set()
        )
    except OSError, subprocess.SubprocessError:
        return ""
    if not atom_hits and not gone:
        return (
            f"; none of the {len(atom_removed)} identifiers the atom's change removed "
            f"appears in the section, and none it names left the tree in {base}..HEAD"
        )
    named_list = [f"`{t}` (the atom's change removed it)" for t in sorted(atom_hits)]
    named_list += [
        f"`{t}` (removed in {base}..HEAD, and gone from the tree)" for t in sorted(gone)
    ]
    return "; the section names " + ", ".join(named_list)
