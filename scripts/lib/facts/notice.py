"""The facts diff notice: each ``CLAUDE.md`` section citing an atom that a diff moved.

The notice compares every cited atom at a diff's base with the same atom at its head, so it
has to read both ends. The section walk in ``citations`` and ``lock`` cannot: ``repo_docs``
and ``tracked_files`` run ``git ls-files`` on the index, and ``lock.repo_citations`` reads each
doc from disk, so all three describe the checkout and no earlier revision. ``Tree`` reads
either end through one interface, with two backends.

- **At a revision.** One ``git ls-tree -r -z <rev>`` gives the tracked set and the blob id of
  every path. Text comes from one long-lived ``git cat-file --batch`` process, which
  ``lib.git.git_process`` starts, so an inherited ``GIT_DIR`` cannot point it at another
  repository. The listing decides what exists: a path absent at the revision reads as
  ``None`` and sends no request, so a ``missing`` reply can only mean a damaged object store,
  and ``Tree`` raises on it rather than waiting for a size that never comes.
- **At the working tree.** ``git ls-files`` gives the tracked set, and text comes from disk, so
  an uncommitted edit is read. A listed file that is missing on disk reads as ``None``.

``git ls-tree`` takes no glob: a ``'**/CLAUDE.md'`` pathspec lists the root doc alone. Both
backends therefore find the docs by basename in the full listing.

A doc's sections are cached by path and blob id for the life of the process, so a replay over
many commits splits a doc that no commit changed only once.

The plan is ``docs/target-architecture/docs-guard/g1-diff-notice.md``. Nothing calls ``Tree``
yet; ``moved_sections`` is its first caller.
"""

from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.git import git, git_process

from .citations import Section, sections

DOC_NAME = "CLAUDE.md"

# (doc path, blob id) -> that blob split into sections. Keyed by path too, because a section
# key starts with its doc's path and two docs can hold the same bytes.
_SECTIONS: dict[tuple[str, str], list[Section]] = {}


class MissingObject(RuntimeError):
    """A blob that a revision's listing names and the object store does not hold."""


class Tree:
    """The tracked paths and file text of one revision, or of the working tree.

    Use it as a context manager, or call ``close()``: the revision backend keeps a
    ``git cat-file`` process open from the first read until then.

    Attributes:
      repo: the repository, which alone decides what git reads.
      rev: the revision read, or None for the working tree.
      tracked: every tracked path at ``rev``, repo-relative with forward slashes, in the
        shape ``citations.in_tree`` takes.
    """

    def __init__(self, repo: Path, rev: str | None = None):
        """Lists the tracked paths at ``rev``, or in the index when ``rev`` is None.

        Raises:
          subprocess.CalledProcessError: ``rev`` does not name a commit or tree.
        """
        self.repo = repo
        self.rev = rev
        self._batch = None
        self._blobs: dict[str, str] | None = None
        if rev is None:
            listed = git("ls-files", "-z", cwd=repo).stdout
            self.tracked = frozenset(p for p in listed.split("\0") if p)
            return
        self._blobs = {}
        paths = []
        for entry in git("ls-tree", "-r", "-z", rev, cwd=repo).stdout.split("\0"):
            if not entry:
                continue
            meta, _, path = entry.partition("\t")
            _mode, kind, oid = meta.split()
            paths.append(path)
            if kind == "blob":
                self._blobs[path] = oid
        self.tracked = frozenset(paths)

    def __enter__(self) -> "Tree":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        """Stop the ``cat-file`` process, if a read started one."""
        if self._batch is None:
            return
        for pipe in (self._batch.stdin, self._batch.stdout):
            if pipe is not None:
                pipe.close()
        self._batch.wait()
        self._batch = None

    def docs(self) -> list[str]:
        """Every tracked ``CLAUDE.md``, at any depth, sorted."""
        return sorted(
            p for p in self.tracked if p == DOC_NAME or p.endswith("/" + DOC_NAME)
        )

    def text(self, rel: str) -> str | None:
        """The text of the tracked file ``rel``, or None when it is not there.

        Raises:
          MissingObject: the revision lists ``rel`` and the object store lacks its blob.
        """
        if self._blobs is not None:
            oid = self._blobs.get(rel)
            return None if oid is None else self._read(oid)
        if rel not in self.tracked:
            return None
        try:
            return (self.repo / rel).read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError, IsADirectoryError, NotADirectoryError:
            return None

    def sections(self, rel: str) -> list[Section] | None:
        """The doc ``rel`` split with ``citations.sections``, or None when it is not there."""
        oid = self._blobs.get(rel) if self._blobs is not None else None
        if oid is None:
            text = self.text(rel)
            return None if text is None else sections(rel, text)
        if (rel, oid) not in _SECTIONS:
            _SECTIONS[rel, oid] = sections(rel, self._read(oid))
        return _SECTIONS[rel, oid]

    def _read(self, oid: str) -> str:
        if self._batch is None:
            self._batch = git_process("cat-file", "--batch", cwd=self.repo)
        stdin, stdout = self._batch.stdin, self._batch.stdout
        assert stdin is not None and stdout is not None, "git_process opens both pipes"
        stdin.write(oid.encode() + b"\n")
        stdin.flush()
        header = stdout.readline().decode()
        # "<oid> <type> <size>" for a hit; "<oid> missing", or nothing once git has died.
        fields = header.split()
        if len(fields) != 3:
            raise MissingObject(
                f"git cat-file --batch in {self.repo} answered {header.strip()!r} "
                f"for blob {oid} at {self.rev}"
            )
        data = stdout.read(int(fields[2]))
        stdout.read(1)  # the LF that ends every reply
        return data.decode("utf-8", errors="replace")
