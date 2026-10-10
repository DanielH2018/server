"""Identifier-shaped tokens a range of commits removed, and whether the tree still holds them.

Two readers share this. The ``vanished-identifier`` lint rule (``lint.vanished_identifiers``)
flags a section that names a token the branch removed from the tree. The ``moved`` finding
(``evidence.moved_evidence``) names the tokens a moved atom's range removed, so its author
can tell a refactor from a change the prose describes.
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
# Markdown is the prose under test, not the tree it describes, and the lock records citations.
# `lock.LOCK_REL`, restated: `lock` imports this module through `evidence`, so importing the
# constant back would be a cycle. `test_facts_lock_evidence.py` pins the two together.
LOCK_PATH = "docs/facts.lock"
_CODE = (".", ":!*.md", f":!{LOCK_PATH}")


def identifiers(text: str) -> set[str]:
    """The identifier-shaped tokens in ``text``.

    Six or more characters holding ``_``, ``.``, ``/`` or ``-``, with no edge punctuation, and
    not starting like a version or a number. A bare word is too common to have "vanished".
    """
    out = set()
    for raw in _TOKEN.findall(text):
        # A leading `.` stays: `.claude/rules/facts.md` is a path, and `claude/rules/...` is not.
        tok = _EDGE.sub("", raw).rstrip("./-")
        if (
            len(tok) >= 6
            and any(c in tok for c in "_./-")
            and not _VERSIONISH.match(tok)
        ):
            out.add(tok)
    return out


def removed_tokens(repo: Path, base: str) -> set[str]:
    """Identifiers on the lines the working tree removed from ``base``, outside ``_CODE``'s exclusions.

    Against the working tree, not HEAD: the prek hook runs before the commit exists, and prek
    stashes unstaged edits first, so the tree it reads is the index about to be committed.
    ``--no-textconv`` keeps git from decrypting ``ansible/vars/secrets.yml`` through its
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
    removed: set[str] = set()
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("-"):
            removed |= identifiers(line[1:])
    return removed


def still_present(repo: Path, tokens: list[str], tracked: frozenset[str]) -> set[str]:
    """The ``tokens`` that a tracked path ends with or a tracked non-Markdown file holds.

    A path counts component-aligned, so ``tasks/agent.yml`` is held by any ``.../tasks/agent.yml``
    and ``roles/setup/deploy_ui`` by any file under it.

    DECIDED: content counts as a substring, not a whole word. Docs name a family by its stem
    (speedtest's `_upload_bits` for `speedtest_tracker_upload_bits`), and `git grep -w` read
    every such stem as vanished. The cost is a rename that only appends to the old name, which
    this rule then misses. The 2026-10-10 replay that measured the rule's precision used
    substring matching too.
    """
    held = {t for t in tokens if any(f"/{t}/" in f"/{p}/" for p in tracked)}
    rest = [t for t in tokens if t not in held]
    if not rest:
        return held

    def grep(*args: str) -> str:
        return git(
            "grep", "-I", "-F", *args, "--", *_CODE, cwd=repo, check=False
        ).stdout

    held |= set(grep("-o", "-h", *[a for t in rest for a in ("-e", t)]).splitlines())
    # `-o` prints one match per position, so a token that occurs only inside a longer token
    # that also matched is never printed. Ask once more, alone, for each one not seen.
    held |= {t for t in rest if t not in held and grep("-l", "-e", t)}
    return held & set(tokens)
