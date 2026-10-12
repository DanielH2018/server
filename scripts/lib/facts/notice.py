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

``moved_sections`` opens a ``Tree`` at each end of one diff and lists a ``Notice`` for every
cited atom that the diff moved. It compares the two ends of that diff, never a hash recorded
earlier, so a row means the diff itself moved the atom. The row rules are on
``moved_sections``.

The plan is ``docs/target-architecture/docs-guard/g1-diff-notice.md``.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.git import git, git_process

from .atoms import Ambiguous, atom_content
from .citations import Citation, Section, in_tree, parse_citations, sections
from .lint import bodies_at

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
        self._present: frozenset[str] | None = None
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

    def present(self) -> frozenset[str]:
        """The tracked paths that hold a file: all of ``tracked`` at a revision.

        In the working tree, ``tracked`` lists the index, which still holds a file deleted
        from disk without ``git rm``. This set leaves such a file out, as the lock's
        ``atoms.hash_atom`` does by checking ``is_file()``.
        """
        if self._blobs is not None:
            return self.tracked
        if self._present is None:
            self._present = frozenset(
                p for p in self.tracked if (self.repo / p).is_file()
            )
        return self._present

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


@dataclass(frozen=True)
class Notice:
    """One cited atom that a diff moved, in a section that cites it.

    Attributes:
      unit: the citing section, keyed ``<doc>#<heading>``.
      atom: the citation as the section spells it.
      form: one of ``citations.FORMS``.
      kind: ``moved``, ``gone``, ``ambiguous`` or ``unparsable``.
      prose_edited: whether the diff also changed the section's body.
    """

    unit: str
    atom: str
    form: str
    kind: str
    prose_edited: bool


# What ``atom_content`` raises for a file it cannot parse. ``ast.parse`` raises the first two,
# and ``yaml_fast.safe_load`` raises a ``yaml.YAMLError`` subclass.
_UNPARSABLE = (SyntaxError, ValueError, yaml.YAMLError)


def _changed_paths(repo: Path, base: str, head: str | None) -> set[str]:
    """Every path that the diff from ``base`` to ``head``, or to the working tree, touched."""
    ends = [base] if head is None else [base, head]
    out = git("diff", "--no-renames", "--name-status", "-z", *ends, cwd=repo).stdout
    # -z prints "<status>\0<path>\0" per file, and --no-renames keeps each entry to one path.
    return set(out.split("\0")[1::2]) - {""}


def _touched(c: Citation, changed: set[str]) -> bool:
    """Whether the diff touched the file ``c`` names, or any file under the directory it names.

    The match mirrors ``citations.in_tree``, which also accepts a directory cited without its
    trailing slash.
    """
    if c.path.endswith("/"):
        return any(p.startswith(c.path) for p in changed)
    return c.path in changed or any(p.startswith(c.path + "/") for p in changed)


def _content(c: Citation, tree: Tree) -> str | None:
    text = tree.text(c.path)
    return None if text is None else atom_content(c, text)


def _kind(c: Citation, base: Tree, head: Tree) -> str | None:
    """The kind of row ``c`` gives, or None when the diff did not move it."""
    if c.form == "path":
        # The lock hashes a path atom's existence, so a path atom can go but never move.
        gone = in_tree(c, base.tracked) and not in_tree(c, head.present())
        return "gone" if gone else None
    try:
        before = _content(c, base)
        after = _content(c, head)
    except Ambiguous:
        return "ambiguous"
    except _UNPARSABLE:
        return "unparsable"
    if before is None:
        # An atom that resolves only at the head is new, and the lock holds no hash for it.
        return None
    if after is None:
        return "gone"
    return "moved" if before != after else None


def moved_sections(repo: Path, base: str, head: str | None = None) -> list[Notice]:
    """Lists a ``Notice`` for every cited atom that the diff from ``base`` to ``head`` moved.

    With ``head`` None the head end is the working tree, so an uncommitted edit counts. With a
    revision, every head-side input is read at that revision: the docs, the tracked set, the
    diff and the atoms. Rows come in doc order, then section order, one per unit and atom.

    - The sections are those of every ``CLAUDE.md`` at the head. Their citations are parsed
      with no tracked-set filter.
    - A citation whose path the diff did not touch gives no row. Neither does one that names
      a tracked file or directory at neither end. A file that the diff deletes is tracked at
      the base, so its citations still reach the rules below.
    - A content atom reads ``moved`` when it resolves at both ends to different content. It
      reads ``gone`` when it resolves at the base only.
    - A path atom reads ``gone`` when it is tracked at the base and not at the head. In the
      working tree, a file deleted from disk without ``git rm`` counts as not there.
    - A marker prefix that matches twice at either end reads ``ambiguous``. A cited file that
      fails to parse at either end reads ``unparsable``. Any other exception propagates,
      because it is a defect in the notice rather than in the diff.
    - ``prose_edited`` compares the section's body at the base, from ``lint.bodies_at``, with
      its body at the head.

    Raises:
      subprocess.CalledProcessError: ``base`` or ``head`` does not name a revision.
      MissingObject: a revision lists a blob that the object store does not hold.
    """
    changed = _changed_paths(repo, base, head)
    if not changed:
        return []
    rows: list[Notice] = []
    kinds: dict[str, str | None] = {}
    with Tree(repo, base) as before, Tree(repo, head) as after:
        for doc in after.docs():
            base_bodies: dict[str, str] | None = None
            for sec in after.sections(doc) or []:
                cites, _ = parse_citations(sec.body)
                seen: set[str] = set()
                for c in cites:
                    if c.raw in seen or not _touched(c, changed):
                        continue
                    seen.add(c.raw)
                    if not (in_tree(c, before.tracked) or in_tree(c, after.tracked)):
                        continue
                    if c.raw not in kinds:
                        kinds[c.raw] = _kind(c, before, after)
                    kind = kinds[c.raw]
                    if kind is None:
                        continue
                    if base_bodies is None:
                        # One ``git show`` per doc that holds a row, not one per doc read.
                        base_bodies = bodies_at(repo, base, doc)
                    edited = base_bodies.get(sec.key) != sec.body
                    rows.append(Notice(sec.key, c.raw, c.form, kind, edited))
    return rows
