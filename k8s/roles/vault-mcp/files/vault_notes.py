"""The reads behind vault-mcp's tools, over the markdown vault livesync-cli mirrors.

Every tool goes through one `Vault`, which owns the rule that hides a path from Claude: a path
is hidden when any segment starts with `.`, or when it lies under a configured exclude path.
`Vault.notes` never yields a hidden note, and `Vault.resolve` refuses a hidden path with the
same error as a missing one, so no tool, search result or backlink can name a hidden note.

Stdlib and PyYAML only; vault_server.py is the MCP wiring.
"""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

import yaml

NOTE_SUFFIX = ".md"
MAX_RESULTS = 100
SNIPPET_CHARS = 160

_WIKILINK = re.compile(r"!?\[\[([^\]|#^]*)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
_MDLINK = re.compile(r"!?\[[^\]]*\]\(<?([^)<>\s]+)>?(?:\s+\"[^\"]*\")?\)")
_INLINE_TAG = re.compile(r"(?<![\w/#&])#([\w/-]*[^\W\d][\w/-]*)")
_FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)


class NoteNotFound(LookupError):
    """The path names no visible note or folder: absent, hidden, or outside the vault."""

    def __init__(self, path: str) -> None:
        super().__init__(f"no note or folder at {path!r}")


def _parts(path: str) -> tuple[str, ...]:
    return tuple(p for p in PurePosixPath(path).parts if p not in ("/", "."))


def _folded(parts: Iterable[str]) -> tuple[str, ...]:
    return tuple(p.casefold() for p in parts)


@dataclass(frozen=True)
class Link:
    """One outgoing link: its target as written, and the note it resolves to, if any."""

    target: str
    path: str | None


@dataclass(frozen=True)
class Note:
    """A parsed note. `path` is relative to the vault root, with its `.md` suffix."""

    path: str
    frontmatter: dict
    body: str
    targets: tuple[str, ...]
    tags: frozenset[str]
    modified: float

    @property
    def title(self) -> str:
        return PurePosixPath(self.path).stem


def parse_note(path: str, text: str, modified: float = 0.0) -> Note:
    """Split a note's YAML frontmatter from its body, and read its link targets and tags.

    Frontmatter loads with PyYAML's BaseLoader, so every scalar is a string and the result is
    always JSON-serialisable. Frontmatter that does not parse to a mapping reads as empty.
    """
    frontmatter: dict = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                loaded = yaml.load(text[4:end], Loader=yaml.BaseLoader)
            except yaml.YAMLError:
                loaded = None
            frontmatter = loaded if isinstance(loaded, dict) else {}
            body = text[end + 4 :].split("\n", 1)[1] if "\n" in text[end + 4 :] else ""
    targets = [m.group(1).strip() for m in _WIKILINK.finditer(body)]
    targets += [
        unquote(m.group(1).split("#", 1)[0])
        for m in _MDLINK.finditer(body)
        if ":" not in m.group(1)
    ]
    return Note(
        path=path,
        frontmatter=frontmatter,
        body=body,
        targets=tuple(t for t in targets if t),
        tags=frozenset(_frontmatter_tags(frontmatter) | _inline_tags(body)),
        modified=modified,
    )


def _frontmatter_tags(frontmatter: dict) -> set[str]:
    raw = frontmatter.get("tags", frontmatter.get("tag", []))
    items = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw))
    return {str(t).lstrip("#").casefold() for t in items if str(t).strip("# ")}


def _inline_tags(body: str) -> set[str]:
    return {m.group(1).casefold() for m in _INLINE_TAG.finditer(_FENCE.sub("", body))}


def _has_tag(note: Note, tag: str) -> bool:
    want = tag.lstrip("#").casefold()
    return any(t == want or t.startswith(want + "/") for t in note.tags)


def _snippet(body: str, terms: list[str]) -> str:
    lowered = body.casefold()
    hits = [i for i in (lowered.find(t) for t in terms) if i != -1]
    start = max(0, min(hits) - SNIPPET_CHARS // 4) if hits else 0
    return " ".join(body[start : start + SNIPPET_CHARS].split())


def _limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_RESULTS))


@dataclass
class Vault:
    """The vault at `root`, with the folders `exclude_paths` names hidden from every read.

    An exclude path matches by whole path segments, case-insensitively, so `94 Docs/superpowers/`
    hides that folder and everything under it but not a sibling `94 Docs/superpowers-x/`.
    Hidden segments (a leading `.`) are hidden at any depth whatever the list says.
    """

    root: Path
    exclude_paths: Iterable[str] = ()
    _excluded: tuple[tuple[str, ...], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self._excluded = tuple(
            _folded(parts) for parts in map(_parts, self.exclude_paths) if parts
        )

    def hidden(self, parts: tuple[str, ...]) -> bool:
        """True when a vault-relative path, as segments, is hidden from Claude."""
        if any(p.startswith(".") for p in parts):
            return True
        folded = _folded(parts)
        return any(folded[: len(e)] == e for e in self._excluded)

    def resolve(self, path: str) -> tuple[Path, str]:
        """The absolute path and vault-relative POSIX path a caller's `path` names.

        Raises NoteNotFound for a path that is absent, hidden, climbs out with `..`, or
        resolves through a symlink to somewhere outside the vault or hidden inside it.
        """
        parts = _parts(path)
        if ".." in parts or self.hidden(parts):
            raise NoteNotFound(path)
        full = self.root.joinpath(*parts).resolve()
        if not full.is_relative_to(self.root) or not full.exists():
            raise NoteNotFound(path)
        rel = full.relative_to(self.root).parts
        if self.hidden(rel):
            raise NoteNotFound(path)
        return full, PurePosixPath(*rel).as_posix() if rel else ""

    def notes(self, under: str = "") -> Iterator[Note]:
        """Every visible note under the folder `under`, parsed. Symlinks are never followed."""
        base, _ = self.resolve(under)
        for dirpath, dirnames, filenames in os.walk(base):
            here = Path(dirpath)
            rel_dir = here.relative_to(self.root).parts
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not self.hidden((*rel_dir, d)) and not (here / d).is_symlink()
            )
            for name in sorted(filenames):
                full = here / name
                if (
                    not name.endswith(NOTE_SUFFIX)
                    or self.hidden((*rel_dir, name))
                    or full.is_symlink()
                ):
                    continue
                rel = PurePosixPath(*rel_dir, name).as_posix()
                yield parse_note(
                    rel,
                    full.read_text(encoding="utf-8", errors="replace"),
                    full.stat().st_mtime,
                )

    def note(self, path: str) -> Note:
        """The visible note `path` names, with or without its `.md` suffix."""
        if not path.endswith(NOTE_SUFFIX):
            path += NOTE_SUFFIX
        full, rel = self.resolve(path)
        if not full.is_file():
            raise NoteNotFound(path)
        return parse_note(
            rel,
            full.read_text(encoding="utf-8", errors="replace"),
            full.stat().st_mtime,
        )

    def search(
        self, query: str = "", tag: str = "", path: str = "", limit: int = 20
    ) -> list[dict]:
        """Notes holding every whitespace-separated term of `query`, in their path or body.

        `tag` keeps notes carrying that tag or a nested child of it (`project` matches
        `project/x`); `path` keeps notes under that folder. Results rank by term count.
        """
        terms = query.casefold().split()
        scored = []
        for note in self.notes(path):
            if tag and not _has_tag(note, tag):
                continue
            haystack = f"{note.path}\n{note.body}".casefold()
            if not all(t in haystack for t in terms):
                continue
            scored.append((sum(haystack.count(t) for t in terms), note))
        scored.sort(key=lambda pair: (-pair[0], pair[1].path))
        return [
            {"path": n.path, "title": n.title, "snippet": _snippet(n.body, terms)}
            for _, n in scored[: _limit(limit)]
        ]

    def read_note(self, path: str) -> dict:
        """A note's body, frontmatter, tags and outgoing links, each resolved where it can be.

        A link to a hidden or missing note keeps its target text and resolves to no path.
        """
        note = self.note(path)
        index = _LinkIndex(self.notes())
        return {
            "path": note.path,
            "frontmatter": note.frontmatter,
            "body": note.body,
            "tags": sorted(note.tags),
            "links": [
                {"target": t, "path": index.resolve(t, note.path)} for t in note.targets
            ],
        }

    def list_folder(self, path: str = "") -> list[dict]:
        """The visible folders and files directly inside a folder, folders first."""
        full, rel = self.resolve(path)
        if not full.is_dir():
            raise NoteNotFound(path)
        entries = []
        for child in full.iterdir():
            parts = (*_parts(rel), child.name)
            if self.hidden(parts) or child.is_symlink():
                continue
            kind = (
                "folder"
                if child.is_dir()
                else "note"
                if child.name.endswith(NOTE_SUFFIX)
                else "file"
            )
            entries.append({"name": child.name, "path": "/".join(parts), "type": kind})
        return sorted(
            entries, key=lambda e: (e["type"] != "folder", e["name"].casefold())
        )

    def recent_notes(self, limit: int = 20) -> list[dict]:
        """The most recently modified visible notes, newest first."""
        newest = sorted(self.notes(), key=lambda n: (-n.modified, n.path))
        return [
            {
                "path": n.path,
                "title": n.title,
                "modified": datetime.fromtimestamp(
                    n.modified, timezone.utc
                ).isoformat(),
            }
            for n in newest[: _limit(limit)]
        ]

    def backlinks(self, path: str) -> list[dict]:
        """The visible notes with a link that resolves to the note `path` names."""
        target = self.note(path).path
        notes = list(self.notes())
        index = _LinkIndex(notes)
        return [
            {"path": n.path, "title": n.title}
            for n in notes
            if n.path != target
            and any(index.resolve(t, n.path) == target for t in n.targets)
        ]


class _LinkIndex:
    """Resolves a link target the way Obsidian does, over the visible notes only.

    A target with a folder resolves against the linking note's folder, then the vault root.
    A bare name resolves to the note of that name in the linking note's folder, else the one
    with the shortest path. Matching ignores case and the `.md` suffix.
    """

    def __init__(self, notes: Iterable[Note]) -> None:
        self._by_path: dict[str, str] = {}
        self._by_stem: dict[str, list[str]] = {}
        for n in notes:
            self._by_path[n.path[: -len(NOTE_SUFFIX)].casefold()] = n.path
            self._by_stem.setdefault(n.title.casefold(), []).append(n.path)

    def resolve(self, target: str, source: str) -> str | None:
        stem = (
            target[: -len(NOTE_SUFFIX)]
            if target.casefold().endswith(NOTE_SUFFIX)
            else target
        )
        folder = PurePosixPath(source).parent
        if "/" in stem:
            for base in (folder, PurePosixPath()):
                joined = posixpath.normpath((base / stem).as_posix())
                found = self._by_path.get(joined.casefold())
                if found:
                    return found
            return None
        candidates = self._by_stem.get(stem.casefold(), [])
        same_folder = [c for c in candidates if PurePosixPath(c).parent == folder]
        ranked = same_folder or sorted(candidates, key=lambda c: (c.count("/"), c))
        return ranked[0] if ranked else None
