"""Rewrite the references a rename left behind, where the removed-name rules look for them.

A commit that moves a file or a directory fixes the code that imports it, because a test
fails otherwise. Nothing fails for prose. ``lint.vanished_identifiers`` and
``lint.vanished_advice`` report the old path where a doc or a comment still uses it, and
``move`` is the fix for the rename case: it rewrites the same places.

- An instruction file or a live doc, outside a ``**HISTORY —`` block and a
  ``generated_from`` block. An archive page, an ADR and a slice plan keep the old path:
  they describe the tree as it stood.
- A full-line ``#`` or ``//`` comment in any other tracked text file. Code and docstrings
  are left alone: a rename that leaves code naming the old path fails its own tests.
- With a store root, every Markdown file under it outside an ``archive/`` directory, and
  nothing in the repo. A memory store links docs by the main checkout's absolute path,
  so a reference may follow that prefix.

A directory seldom moves whole. A reference is rewritten only when the path it would then
name is tracked, and the rest are returned for a person to place.
"""

import re
import sys as _sys
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.facts.citations import (
    GENERATED_BLOCK,
    is_fact_doc,
    is_live_doc,
    mask_history,
    tracked_files,
)
from lib.facts.removed import COMMENT_LINES
from lib.git import git


@dataclass
class Moved:
    """What one ``move`` did.

    Attributes:
        rewritten: references rewritten, per file.
        left: per file, each old path that was left because its new path is not tracked.
    """

    rewritten: dict[str, int] = field(default_factory=dict)
    left: dict[str, list[str]] = field(default_factory=dict)


def _reference(old: str, checkout: Path) -> re.Pattern[str]:
    """Matches ``old`` as a whole path or as the leading directories of a longer one.

    Group ``rest`` holds the path below ``old``. The path may open a token or follow
    ``checkout``'s absolute path. It is no match inside a longer path (``x/old``) or a
    longer name (``old-v2``, ``old.bak``).
    """
    absolute = re.escape(f"{checkout}/")
    below = r"(?P<rest>(?:/\.?[\w-]+(?:\.[\w-]+)*)*)"
    return re.compile(
        rf"(?:(?<![\w./-])|(?<={absolute})){re.escape(old)}{below}(?![\w-]|\.\w)"
    )


def _blank(text: str, spans: re.Pattern[str], keep: bool) -> str:
    """``text`` with each match of ``spans`` blanked, or everything else when ``keep``."""
    out, at = [], 0
    for found in spans.finditer(text):
        between, inside = text[at : found.start()], found.group()
        out += [
            re.sub(r"\S", " ", between) if keep else between,
            inside if keep else re.sub(r"\S", " ", inside),
        ]
        at = found.end()
    tail = text[at:]
    return "".join(out) + (re.sub(r"\S", " ", tail) if keep else tail)


def _main_checkout(repo: Path) -> Path:
    common = git(
        "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo
    ).stdout.strip()
    return Path(common).parent


def _held(path: str, tracked: frozenset[str]) -> bool:
    return path in tracked or any(p.startswith(f"{path}/") for p in tracked)


def move(
    repo: Path, old: str, new: str, root: Path | None = None, write: bool = True
) -> Moved:
    """Rewrite each reference to ``old`` as ``new``, where the result names a tracked path.

    Args:
        repo: the checkout whose tree decides what is tracked.
        old: the repo path that is gone, a file or a directory.
        new: the repo path that replaced it.
        root: a store of Markdown outside the repo to rewrite, in place of the repo.
        write: False to count the references and change nothing.

    Returns:
        The references rewritten and the ones left, keyed by file relative to ``root`` or
        to ``repo``.

    Raises:
        ValueError: ``old`` is still tracked, or ``new`` is not. Rewriting then would
            point references at a path that is not there, or away from one that is.
    """
    old, new = old.rstrip("/"), new.rstrip("/")
    tracked = tracked_files(repo)
    if _held(old, tracked):
        raise ValueError(f"{old} is still tracked: move it first, then rewrite")
    if not _held(new, tracked):
        raise ValueError(f"{new} is not tracked: nothing to point the references at")
    ref = _reference(old, _main_checkout(repo))

    base = root or repo
    if root:
        docs = sorted(
            str(p.relative_to(root))
            for p in root.rglob("*.md")
            if "archive" not in p.relative_to(root).parts
        )
        code: list[str] = []
    else:
        docs = sorted(p for p in tracked if is_fact_doc(p) or is_live_doc(p))
        code = sorted(p for p in tracked if not p.endswith(".md"))

    moved = Moved()
    for rel, is_doc in [(d, True) for d in docs] + [(c, False) for c in code]:
        path = base / rel
        if path.is_symlink() or not path.is_file():
            continue
        raw = path.read_bytes()
        if old.encode() not in raw:
            continue
        try:
            text = raw.decode()
        except UnicodeDecodeError:
            continue
        if is_doc:
            visible = _blank(mask_history(text), GENERATED_BLOCK, keep=False)
        else:
            visible = _blank(text, COMMENT_LINES, keep=True)
        out, at = [], 0
        for found in ref.finditer(visible):
            rest = found.group("rest")
            if not _held(new + rest, tracked):
                moved.left.setdefault(rel, []).append(old + rest)
                continue
            out += [text[at : found.start()], new + rest]
            at = found.end()
        if out:
            moved.rewritten[rel] = len(out) // 2
            if write:
                path.write_text("".join(out) + text[at:])
    return moved
