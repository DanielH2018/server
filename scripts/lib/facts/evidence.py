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

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.git import git

from .atoms import Ambiguous, atom_content
from .citations import Citation, mask_history, tracked_files
from .removed import identifiers, removed_tokens, still_present


@dataclass
class EvidenceCache:
    """What every ``moved`` finding against one tree shares: a range diff per ``verified_sha``.

    Many rows share a ``verified_sha`` (a bulk verify records hundreds at one commit), and the
    range-wide diff is the expensive half, so it is read once per sha.
    """

    removed: dict[str, set[str] | None] = field(default_factory=dict)
    tracked: frozenset[str] | None = None


def _resolves(repo: Path, sha: str) -> bool:
    return (
        git(
            "rev-parse",
            "--verify",
            "--quiet",
            f"{sha}^{{commit}}",
            cwd=repo,
            check=False,
        ).returncode
        == 0
    )


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
    repo: Path, c: Citation, verified_sha: str, prose: str, cache: EvidenceCache
) -> str:
    """A clause naming the removed identifiers the section's ``prose`` names, or saying it names none.

    Empty when ``verified_sha`` does not resolve (a shallow clone, a rewritten history) or git
    fails: the finding then keeps its plain message, never an error. A history paragraph or
    bullet is masked, since it names a gone thing by design.
    """
    try:
        if not _resolves(repo, verified_sha):
            return ""
        old = git("show", f"{verified_sha}:{c.path}", cwd=repo, check=False)
        old_source = old.stdout if old.returncode == 0 else None
        target = repo / c.path
        new_source = target.read_text(encoding="utf-8") if target.is_file() else ""
        atom_removed = _atom_tokens(c, old_source, new_source)
        if verified_sha not in cache.removed:
            cache.removed[verified_sha] = removed_tokens(repo, verified_sha)
        range_removed = cache.removed[verified_sha] or set()
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
            f"appears in the section, and none it names left the tree in {verified_sha}..HEAD"
        )
    named_list = [f"`{t}` (the atom's change removed it)" for t in sorted(atom_hits)]
    named_list += [
        f"`{t}` (removed in {verified_sha}..HEAD, and gone from the tree)"
        for t in sorted(gone)
    ]
    return "; the section names " + ", ".join(named_list)
