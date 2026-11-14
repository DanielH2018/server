"""Identifier-shaped tokens a range of commits removed, and whether the tree still holds them.

The removed-name lint rules (``lint.vanished_identifiers``) read this to flag a doc section
or a code comment that names a token the branch removed from the tree.
"""

import re
import sys as _sys
from pathlib import Path
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.git import git

_TOKEN = re.compile(r"[A-Za-z0-9_./-]*[A-Za-z_][A-Za-z0-9_./-]*")
# A version or a number: `v0.33.0-rootless`, `3.14-alpine`, `2026.10.0-ls253`, `10.0.0.240`.
# `version-as-fact` owns a version in prose, and a Renovate bump removes one on every PR, which
# #4012 ruled must not arrive red over documentation.
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


def removed_lines(repo: Path, base: str) -> list[str]:
    """The lines the working tree removed from ``base``, outside ``_CODE``'s exclusions.

    Against the working tree, not HEAD: the prek hook runs before the commit exists, and prek
    stashes unstaged edits first, so the tree it reads is the index about to be committed.
    ``--no-textconv`` keeps git from decrypting ``secrets/secrets.yml`` through its
    ``diff=sops`` attribute.
    """
    diff = git(
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "-U0",
        base,
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


class Code:
    """The text of every tracked non-Markdown file in the working tree, read once.

    Answering from memory keeps the cost flat. One ``git grep`` per token scans the tree
    each time, and a branch that deletes a role removes thousands of tokens.

    A file git would call binary is left out, and so is a path the working tree does not
    hold as a file.
    """

    def __init__(self, repo: Path, tracked: frozenset[str]) -> None:
        self._paths = "\n".join(f"/{p}/" for p in tracked)
        self._texts: dict[str, str] = {}
        for rel in sorted(tracked):
            if rel.endswith(".md"):
                continue
            try:
                raw = (repo / rel).read_bytes()
            except OSError:
                continue
            if b"\0" not in raw[:8000]:
                self._texts[rel] = raw.decode("utf-8", "replace")
        self._all = "\n".join(self._texts.values())
        self._no_comments: str | None = None

    def _on_a_path(self, tok: str) -> bool:
        return f"/{tok}/" in self._paths

    def holds(self, tok: str) -> bool:
        """Whether a tracked path ends with ``tok`` or a non-Markdown file holds it.

        A path counts component-aligned, so ``tasks/agent.yml`` is held by any
        ``.../tasks/agent.yml`` and ``roles/setup/deploy_ui`` by any file under it.

        DECIDED: content counts as a substring, not a whole word. Docs name a family by its
        stem (speedtest's `_upload_bits` for `speedtest_tracker_upload_bits`), and a
        whole-word match read every such stem as vanished. The cost is a rename that only
        appends to the old name, which this rule then misses. The 2026-10-10 replay that
        measured the rule's precision used substring matching too.
        """
        return self._on_a_path(tok) or tok in self._all

    def holds_outside_comments(self, tok: str) -> bool:
        """``holds``, with every full-line comment left out of the content."""
        if self._no_comments is None:
            self._no_comments = COMMENT_LINES.sub("", self._all)
        return self._on_a_path(tok) or tok in self._no_comments

    def comment_uses(self, tokens: set[str]) -> dict[str, list[str]]:
        """Each of ``tokens`` that a full-line comment names, with those lines as ``path:line``.

        A comment names a token only as a whole identifier, so a longer name that contains
        the token is no use of it.
        """
        if not tokens:
            return {}
        # One pass over every comment finds the few tokens worth locating line by line.
        every_comment = "\n".join(COMMENT_LINES.findall(self._all))
        named = identifiers(every_comment) & tokens
        uses: dict[str, list[str]] = {}
        for rel, text in self._texts.items():
            if not any(tok in text for tok in named):
                continue
            line, at = 1, 0
            for found in COMMENT_LINES.finditer(text):
                here = identifiers(found.group()) & named
                if here:
                    line += text.count("\n", at, found.start())
                    at = found.start()
                    for tok in here:
                        uses.setdefault(tok, []).append(f"{rel}:{line}")
        return uses
