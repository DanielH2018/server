"""The closed citation grammar for the two fact stores.

A backticked span in a memory entry or a ``CLAUDE.md`` section is one of three things:
support (one of ``FORMS``), rejected (a form the spec excludes because it drifts on its own,
``REJECT_REASONS``), or not a citation at all (a command, a flag, a bare word, a host:port).
The third case is ignored, never rejected: prose quotes commands constantly, and a lint
that flagged every backtick would be switched off within a day.

The grammar is closed on purpose. An unrecognised span is not support, so a new form is a
change here plus a paired test, never an ad-hoc regex in a caller.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.git import git

FORMS = frozenset({"path", "symbol", "yaml", "test", "marker"})
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
    ("path", re.compile(rf"^(?P<path>{_FILE}/?)$")),
)
_FILE_LINE = re.compile(r"^[\w./-]+\.[a-z][a-z0-9]*:\d+(?:-\d+)?$")


def spans(text: str) -> list[str]:
    """Every backticked span in ``text`` outside a fence, citation or not, in document order."""
    return _SPAN.findall(_FENCE.sub("", text))


def parse_citations(text: str) -> tuple[list[Citation], list[Rejected]]:
    """Every support citation and every rejected span in ``text``, in document order."""
    cites: list[Citation] = []
    rejects: list[Rejected] = []
    for raw in spans(text):
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


# A test node id in prose, the one citation shape the operator docs and ``CLAUDE.md`` share.
# Looser than the ``test`` form above on purpose, because it serves a different reader: a
# runtime module cites its sibling test from a docstring or a comment, with no backticks and
# by bare filename, and the caller resolves that filename relative to the citing file. The
# path half must end in ``.py``, which keeps ``host:port`` and ``key::value`` prose out. The
# test half must start with ``test_``, so a fixture cited by node id (``conftest.py::seq``)
# is not a claim that a test exists.
_PROSE_NODE = re.compile(r"([\w.][\w./-]*\.py)::(test_\w+)")


def node_citations(text: str) -> list[tuple[str, str]]:
    """Every ``(path, test_name)`` node id cited in ``text``, fenced or not, in order.

    Unlike ``parse_citations`` this reads inside fences: a fenced example of the ``test``
    form still names a real test, and renaming that test should fail the doc guard.
    """
    return _PROSE_NODE.findall(text)


# The scanners below serve the operator-docs guards under ``ansible/tests/repo/``, not the
# facts store. None of them feeds ``parse_citations``. A ``file:line`` span is support there
# and rejected here, because a line number in a long-lived runbook is still a claim that the
# file exists, while a ``CLAUDE.md`` section's support has to hash stably.
#
# DECIDED: no repo-root prefix is required. Requiring one ("ansible/...", "scripts/...")
# matched 4 citations in the whole tree, because the docs overwhelmingly cite from context --
# `roles/k8s/manifests/tasks/main.yml:112`, `deploy_logic.py:458`. A guard that checks four
# things is a guard that passes vacuously, which is why the guard asserts a citation count.
# The extension must begin with a letter, which is what keeps `127.0.0.1:3100` and
# `10.0.0.240:51820` from parsing as a file called `127.0.0.` with extension `1`. Four role
# docs and two networking docs cite host:port pairs exactly that way.
_LINE_NUMBERED = re.compile(r"`([\w.][\w./-]*\.([a-z][a-z0-9]*)):\d+(?:-\d+)?`")


def line_numbered_citations(
    text: str, extensions: frozenset[str] | set[str]
) -> list[str]:
    """Every backticked ``path:line`` in ``text`` whose extension is in ``extensions``.

    The caller passes the extensions its tree holds, so a citation of another repository's
    file (upstream Longhorn's ``deltablock.go:117``) is not read as a claim about this one.
    """
    return [path for path, ext in _LINE_NUMBERED.findall(text) if ext in extensions]


# A bare `ansible/tests/<file>.py` citation is the ONE exception to the line-number rule. Every
# such path is a claim about this tree: nothing else has a directory by that name, and a
# `pytest <path>` line or an `ENFORCED by <path>` note is an instruction someone runs. The
# move of the guards into subdirectories rewrites these citations, and the line-number rule
# would watch none of them go stale. A trailing `:line` or `::node_id` is allowed and dropped.
_GUARD_PATH = re.compile(
    r"`(ansible/tests/[\w./-]+\.py)(?::\d+(?:-\d+)?|::[\w\[\]:.-]+)?`"
)


def guard_path_citations(text: str) -> list[str]:
    """Every backticked ``ansible/tests/...py`` in ``text``, with or without a line number."""
    return _GUARD_PATH.findall(text)


# A `<name>.yml.j2`, whether bare (a `{% from %}` line or an inline bullet mention) or with
# one directory level in front of it -- role-local app config shares the extension
# (`templates/config/config.yml.j2` in configarr, `templates/config/application.yml.j2` in
# janitorr). The directory is kept so the caller can tell those apart from a shared-macro
# reference, which this repo's docs always give bare or as `templates/<macro>.yml.j2`.
# Unlike every other scanner here it needs no backticks: the skeleton it guards is a
# `{% from '<macro>.yml.j2' import ... %}` line inside a fence.
_MACRO = re.compile(r"\b((?:[a-z0-9_-]+/)?[a-z0-9_-]+\.yml\.j2)\b")


def macro_citations(text: str) -> list[str]:
    """Every ``[dir/]<name>.yml.j2`` named in ``text``, fenced or not, in order."""
    return _MACRO.findall(text)


# A paragraph or bullet that opens with this string is history: a deliberate statement about
# something that no longer holds ("retired 2026-08-14", "went on 2026-09-28"). Rules that read
# prose for a claim about THIS tree skip it -- `retired-host`, `version-as-fact` and
# `vanished-identifier` in `lint.py` -- because the sentence is correct exactly when the thing
# it names is gone. The wg-easy role's doc opened a bullet this way before any rule read it, so
# the convention is adopted rather than invented. The bold need not close after the dash.
HISTORY_MARKER = "**HISTORY —"

_LIST_ITEM = re.compile(r"(?:[-*+]|\d+[.)])\s+")


def mask_history(text: str) -> str:
    """``text`` with every history paragraph or bullet blanked out, line breaks kept.

    A bullet runs from its marker line through every following line indented deeper than the
    marker, blank lines included, so its continuation lines and nested bullets are history
    too. A paragraph runs from its first line to the next blank line or list item. Only a
    block that OPENS with ``HISTORY_MARKER`` is history; the marker mid-sentence is prose.
    """
    out: list[str] = []
    prev_blank = True
    # The open history block as (is_bullet, indent), or None outside one.
    hist: tuple[bool, int] | None = None
    for line in text.split("\n"):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        item = _LIST_ITEM.match(stripped)
        if hist is not None:
            is_bullet, hist_indent = hist
            if is_bullet:
                ended = bool(stripped) and indent <= hist_indent
            else:
                ended = not stripped or bool(item)
            if ended:
                hist = None
            else:
                out.append(re.sub(r"\S", " ", line))
                prev_blank = not stripped
                continue
        opener = stripped[item.end() :] if item else (stripped if prev_blank else "")
        if opener.startswith(HISTORY_MARKER):
            hist = (bool(item), indent)
            out.append(re.sub(r"\S", " ", line))
        else:
            out.append(line)
        prev_blank = not stripped
    return "\n".join(out)


@dataclass(frozen=True)
class Section:
    """A logical unit of documentation: a heading and the text up to the next heading."""

    key: str
    """<doc path>#<heading text>; "<doc path>#" for text before the first heading."""

    heading: str
    """The heading text, or empty string for preamble."""

    body: str
    """Text under the heading, up to (not including) the next heading of any level."""


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_CLOSER = re.compile(r"\s+#+$")


def sections(doc_path: str, text: str) -> list[Section]:
    """Split ``text`` at its headings; a section's body ends at the next heading of any level.

    Ownership is by leaf: an atom cited under a subheading belongs to that subheading alone,
    never to the heading it nests under. Every atom in the document has exactly one owning
    unit — the innermost section it appears in — so the key ``<doc>#<heading>`` names both
    the unit ``facts.lock`` is keyed by and the section a reader actually opens for it. A
    renamed heading is a new unit and the old lock row surfaces as a missing section — which
    is the right verdict, since nobody can tell a rename from a deletion by reading the text.

    A trailing ATX closer (``## Title ##``) is not heading text, the same as in Markdown:
    the key is ``<doc>#Title`` whether or not the author closed the heading.
    """
    # Mask fenced blocks to avoid matching # lines inside them, while preserving positions.
    masked = _FENCE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    heads = list(_HEADING.finditer(masked))
    out: list[Section] = []
    if not heads or heads[0].start() > 0:
        end = heads[0].start() if heads else len(text)
        out.append(Section(f"{doc_path}#", "", text[:end]))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[h.end() : end].lstrip("\n")
        heading = _CLOSER.sub("", h.group(2))
        out.append(Section(f"{doc_path}#{heading}", heading, body))
    return out


def repo_docs(repo: Path) -> list[Path]:
    """Every TRACKED ``CLAUDE.md`` under ``repo`` — the repo store, and nothing else.

    ``git ls-files`` rather than ``rglob``: a worktree session has other sessions' full
    checkouts under ``.claude/worktrees/``, and a walk would grade this commit against them.
    """
    listed = git("ls-files", "-z", "--", "CLAUDE.md", "**/CLAUDE.md", cwd=repo).stdout
    return [repo / p for p in sorted(listed.split("\0")) if p]


def tracked_files(repo: Path) -> frozenset[str]:
    """Every path ``git ls-files`` lists, repo-relative with forward slashes.

    This is the one call every support check runs against: a citation names support only
    when it names something in this set, so an untracked or gitignored file reads the same
    on every checkout, never just the one that happens to have it on disk.
    """
    listed = git("ls-files", "-z", cwd=repo).stdout
    return frozenset(p for p in listed.split("\0") if p)


def in_tree(c: Citation, tracked: frozenset[str]) -> bool:
    """Whether ``c`` is support at all: whether it names a tracked file or directory.

    A citation is support only when it names a tracked file or a directory holding one, so an
    untracked or gitignored file is prose, not a broken fact — neither an error nor a warning.
    A directory citation missing its trailing slash (``d/sub`` rather than ``d/sub/``) still
    counts here, so the ``dir-without-slash`` lint rule sees it and can flag the missing
    slash; ``git ls-files`` never lists a bare directory, so this is the one non-exact match
    the non-slash branch needs. A rejected form stays rejected whatever its prefix: a line
    number is a claim about THIS tree however it is spelled.
    """
    if c.path.endswith("/"):
        return any(p.startswith(c.path) for p in tracked)
    return c.path in tracked or any(p.startswith(c.path + "/") for p in tracked)
