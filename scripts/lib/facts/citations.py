"""The closed citation grammar for the two fact stores.

A backticked span in a memory entry or a ``CLAUDE.md`` section is one of three things:
support (one of ``FORMS``), rejected (a form the spec excludes because it drifts on its own,
``REJECT_REASONS``), or not a citation at all (a command, a flag, a bare word, a host:port).
The third case is ignored, never rejected: prose quotes commands constantly, and a lint
that flagged every backtick would be switched off within a day.

The grammar is closed on purpose. An unrecognised span is not support, so a new form is a
change here plus a paired test, never an ad-hoc regex in a caller. Long form:
``docs/superpowers/specs/2026-09-19-fact-support-invalidation-design.md``.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

FORMS = frozenset({"path", "symbol", "yaml", "test", "marker", "probe"})
REJECT_REASONS = frozenset({"file:line"})


@dataclass(frozen=True)
class Citation:
    form: str
    raw: str
    path: str
    selector: str


@dataclass(frozen=True)
class Rejected:
    raw: str
    reason: str


_FENCE = re.compile(r"```.*?```", re.DOTALL)
_SPAN = re.compile(r"`([^`\n]+)`")
_FILE = r"[\w.-]+(?:/[\w.-]+)+"  # at least one slash: a bare filename is not a claim about this tree

# Order matters: the first pattern to match decides the form.
_PATTERNS = (
    (
        "test",
        re.compile(
            rf"^(?P<path>{_FILE}\.py)::(?P<sel>[A-Za-z_]\w*(?:::[A-Za-z_]\w*)?)$"
        ),
    ),
    ("marker", re.compile(rf"^(?P<path>{_FILE}):DECIDED: (?P<sel>\S.*)$")),
    ("symbol", re.compile(rf"^(?P<path>{_FILE}\.py):(?P<sel>[A-Za-z_]\w*)$")),
    ("yaml", re.compile(rf"^(?P<path>{_FILE}\.ya?ml):(?P<sel>[\w-]+(?:\.[\w-]+)*)$")),
    ("probe", re.compile(r"^probe\.py (?P<sel>[\w-]+(?: [\w./:-]+)?)$")),
    ("path", re.compile(rf"^(?P<path>{_FILE}/?)$")),
)
_FILE_LINE = re.compile(r"^[\w./-]+\.[a-z][a-z0-9]*:\d+(?:-\d+)?$")


def parse_citations(text: str) -> tuple[list[Citation], list[Rejected]]:
    """Every support citation and every rejected span in ``text``, in document order."""
    cites: list[Citation] = []
    rejects: list[Rejected] = []
    for raw in _SPAN.findall(_FENCE.sub("", text)):
        if _FILE_LINE.match(raw):
            rejects.append(Rejected(raw, "file:line"))
            continue
        for form, pattern in _PATTERNS:
            m = pattern.match(raw)
            if m:
                path = m.groupdict().get("path") or ""
                cites.append(
                    Citation(
                        form,
                        raw,
                        path,
                        m.group("sel") if "sel" in m.groupdict() else "",
                    )
                )
                break
    return cites, rejects


@dataclass(frozen=True)
class Section:
    """A logical unit of documentation: a heading and everything until the next same-or-higher heading."""

    key: str
    """<doc path>#<heading text>; "<doc path>#" for text before the first heading."""

    heading: str
    """The heading text, or empty string for preamble."""

    body: str
    """Text under the heading, including everything down to the next heading of the same or higher level."""


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def sections(doc_path: str, text: str) -> list[Section]:
    """Split ``text`` at its headings; a section runs to the next heading of the same or higher level.

    The key ``<doc>#<heading>`` is the unit ``facts.lock`` is keyed by, so a renamed heading
    is a new unit and the old lock row surfaces as a missing section — which is the right
    verdict, since nobody can tell a rename from a deletion by reading the text.
    """
    # Mask fenced blocks to avoid matching # lines inside them, while preserving positions.
    masked = _FENCE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    heads = list(_HEADING.finditer(masked))
    out: list[Section] = []
    if not heads or heads[0].start() > 0:
        end = heads[0].start() if heads else len(text)
        out.append(Section(f"{doc_path}#", "", text[:end]))
    for i, h in enumerate(heads):
        level = len(h.group(1))
        end = len(text)
        for later in heads[i + 1 :]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        body = text[h.end() : end].lstrip("\n")
        out.append(Section(f"{doc_path}#{h.group(2)}", h.group(2), body))
    return out


def repo_docs(repo: Path) -> list[Path]:
    """Every TRACKED ``CLAUDE.md`` under ``repo`` — the repo store, and nothing else.

    ``git ls-files`` rather than ``rglob``: a worktree session has other sessions' full
    checkouts under ``.claude/worktrees/``, and a walk would grade this commit against them.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "CLAUDE.md", "**/CLAUDE.md"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [repo / p for p in sorted(listed.split("\0")) if p]
