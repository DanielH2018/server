"""Identifier-shaped tokens a range of commits removed, and whether the tree still holds them.

The removed-name lint rules (``lint.vanished_identifiers``) read this to flag a doc section
or a code comment that names a token the branch removed from the tree. The comment class
(``stale_comment_uses``) is also what ``replay.comment_replay`` replays over history, so the
rule a replay grades and the rule a commit meets are one function.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lib.git import git

_TOKEN = re.compile(r"[A-Za-z0-9_./-]*[A-Za-z_][A-Za-z0-9_./-]*")
# A version or a number: `v0.33.0-rootless`, `3.14-alpine`, `2026.10.0-ls253`, `10.0.0.240`.
# `version-as-fact` owns a version in prose, and a Renovate bump removes one on every PR, which
# must not arrive red over documentation.
_VERSIONISH = re.compile(r"v?\d")
_EDGE = re.compile(r"^(?:\./|[-/])+")
# Markdown is the prose under test, not the tree it describes.
_CODE = (".", ":!*.md")


def identifiers(text: str) -> set[str]:
    """The identifier-shaped tokens in ``text``.

    Six or more characters holding ``_``, ``.``, ``/`` or ``-``, with no edge punctuation, and
    not starting like a version or a number. A bare word is too common to have "vanished".
    """
    out = set()
    for raw in set(_TOKEN.findall(text)):
        # A leading `.` stays: `.claude/rules/facts.md` is a path, and `claude/rules/...` is not.
        tok = _EDGE.sub("", raw).rstrip("./-")
        if (
            len(tok) >= 6
            and any(c in tok for c in "_./-")
            and not _VERSIONISH.match(tok)
        ):
            out.add(tok)
    return out


def is_comment_line(line: str) -> bool:
    """Whether ``line`` is a full-line ``#`` or ``//`` comment. A shebang is code."""
    text = line.lstrip()
    return (text.startswith("#") and not text.startswith("#!")) or text.startswith("//")


def comment_name(tok: str) -> bool:
    """Whether ``tok`` is name-shaped enough to read out of a comment, which has no backticks.

    An underscore, a dot or two hyphens. One hyphen or a slash alone is an English compound
    far more often than a name (``pre-migration``, ``KVM/libvirt``): of 25 graded comment
    rows in the 30 days to 2026-11-14, 11 were such compounds and none was stale.
    """
    return "_" in tok or "." in tok or tok.count("-") >= 2


def removed_lines(repo: Path, base: str, head: str | None = None) -> list[str]:
    """The lines ``head`` removed from ``base``, outside ``_CODE``'s exclusions.

    Without ``head`` the far end is the working tree, not HEAD: the prek hook runs before the
    commit exists, and prek stashes unstaged edits first, so the tree it reads is the index
    about to be committed. ``--no-textconv`` keeps git from decrypting
    ``secrets/secrets.yml`` through its ``diff=sops`` attribute.
    """
    diff = git(
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "-U0",
        base,
        *([head] if head else []),
        "--",
        *_CODE,
        cwd=repo,
    ).stdout
    removed: list[str] = []
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("-"):
            removed.append(line[1:])
    return removed


# A full-line comment, as ``is_comment_line`` reads one line.
COMMENT_LINES = re.compile(r"(?m)^[ \t]*(?:#(?!!)|//).*$")


@dataclass(frozen=True)
class _Split:
    """One file's text cut at its full-line comments.

    Attributes:
      code: the text with every full-line comment removed.
      comments: each comment line that names an identifier, as its 1-based line number and
        the identifiers it names.
      named: every identifier any comment line in the file names.
    """

    code: str
    comments: tuple[tuple[int, frozenset[str]], ...]
    named: frozenset[str]


# Keyed by the text itself: a replay hands ``Code`` the same string objects for every file a
# commit did not change, so each file is cut once per version, not once per commit.
_SPLITS: dict[str, _Split] = {}


def _split(text: str) -> _Split:
    if text not in _SPLITS:
        comments, line, at = [], 1, 0
        for found in COMMENT_LINES.finditer(text):
            names = identifiers(found.group())
            line += text.count("\n", at, found.start())
            at = found.start()
            if names:
                comments.append((line, frozenset(names)))
        _SPLITS[text] = _Split(
            COMMENT_LINES.sub("", text),
            tuple(comments),
            frozenset().union(*(n for _, n in comments)),
        )
    return _SPLITS[text]


def is_text(rel: str, text: str) -> bool:
    """Whether ``Code`` reads the tracked file ``rel``: not Markdown, and not binary to git."""
    return not rel.endswith(".md") and "\0" not in text[:8000]


class Code:
    """The text of every tracked non-Markdown file of one tree, held in memory.

    Answering from memory keeps the cost flat. One ``git grep`` per token scans the tree
    each time, and a branch that deletes a role removes thousands of tokens.
    """

    def __init__(self, tracked: Iterable[str], texts: Mapping[str, str]) -> None:
        """Holds ``texts``, each a file ``is_text`` accepts, beside every tracked path."""
        self._paths = "\n".join(f"/{p}/" for p in tracked)
        self._texts = texts
        self._all: str | None = None
        self._no_comments: str | None = None

    @classmethod
    def in_worktree(cls, repo: Path, tracked: frozenset[str]) -> "Code":
        """The working tree's files. A path the working tree does not hold as a file is out."""
        texts = {}
        for rel in sorted(tracked):
            if rel.endswith(".md"):
                continue
            try:
                text = (repo / rel).read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            if is_text(rel, text):
                texts[rel] = text
        return cls(tracked, texts)

    def _on_a_path(self, tok: str) -> bool:
        return f"/{tok}/" in self._paths

    def holds(self, tok: str) -> bool:
        """Whether a tracked path ends with ``tok`` or a non-Markdown file holds it.

        A path counts component-aligned, so ``tasks/agent.yml`` is held by any
        ``.../tasks/agent.yml`` and ``roles/setup/deploy_ui`` by any file under it.

        DECIDED: content counts as a substring, not a whole word. Docs name a family by its
        stem (speedtest's `_upload_bits` for `speedtest_tracker_upload_bits`), and a
        whole-word match read every such stem as vanished. The cost is a rename that only
        appends to the old name, which this rule then misses.
        """
        if self._all is None:
            self._all = "\n".join(self._texts.values())
        return self._on_a_path(tok) or tok in self._all

    def holds_outside_comments(self, tok: str) -> bool:
        """``holds``, with every full-line comment left out of the content."""
        if self._no_comments is None:
            self._no_comments = "\n".join(_split(t).code for t in self._texts.values())
        return self._on_a_path(tok) or tok in self._no_comments

    def comment_uses(self, tokens: set[str]) -> dict[str, list[str]]:
        """Each of ``tokens`` that a full-line comment names, with those lines as ``path:line``.

        A comment names a token only as a whole identifier, so a longer name that contains
        the token is no use of it.
        """
        uses: dict[str, list[str]] = {}
        if not tokens:
            return uses
        for rel, text in self._texts.items():
            split = _split(text)
            if split.named.isdisjoint(tokens):
                continue
            for line, names in split.comments:
                for tok in sorted(names & tokens):
                    uses.setdefault(tok, []).append(f"{rel}:{line}")
        return uses


def stale_comment_uses(code: Code, lines: list[str]) -> list[tuple[str, str]]:
    """A ``(token, path:line)`` per comment still naming a name ``lines`` took out of code.

    ``lines`` are the lines a diff removed, as ``removed_lines`` returns them, and ``code`` is
    the tree at the diff's far end. A token counts when a removed code line held it, it is
    ``comment_name``-shaped, and only comments still hold it. A name removed from a comment
    alone was stale before the diff, so it seeds nothing.
    """
    names = {
        tok
        for line in lines
        if not is_comment_line(line)
        for tok in identifiers(line)
        if comment_name(tok)
    }
    return [
        (tok, place)
        for tok, places in code.comment_uses(names).items()
        if not code.holds_outside_comments(tok)
        for place in places
    ]
