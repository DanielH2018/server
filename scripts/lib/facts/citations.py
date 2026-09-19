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

import re
from dataclasses import dataclass

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
