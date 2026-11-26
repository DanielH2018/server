"""The census rows holding code comments free of provenance: ticket references and dates."""

import re

from _row_table import Census, Subject, tracked

_COMMENTED = (
    "*.py", "*.sh", "*.yml", "*.yaml", "*.toml", "*.j2", "*.jinja",
    "*.conf", "*.service", "*.timer", "*.alloy",
)  # fmt: skip

# A line that opens with `#` or a Jinja `{#`; a comment after code is skipped, since telling it from
# a `#` in a string needs a tokenizer per language.
_COMMENT = re.compile(r"^\s*(?:#(?!!)|\{#)")

_TICKET = re.compile(
    r"(?<![\w$/{&])#\d{3,}\b|\b(?:[Ii]ssue|PR) #?\d{3,}\b|/(?:issues|pull)/\d+"
)

# A TODO keeps its link, and the other markers are text a tool or a citation reads.
_EXEMPT = (
    "TODO", "fact:", "renovate:", "gen-hooks:", "ENFORCED", "Non-vacuity:",
)  # fmt: skip


# A date from this century's third decade on: what a comment cites when it records an event. An
# example value such as `2001-12-14` stays out of reach.
_DATE = re.compile(r"\b20[2-9]\d-[01]\d-[0-3]\d\b")


def _commented(pattern: re.Pattern[str]):
    """Each comment line `pattern` matches, outside a marker line and a ruling's block.

    A `DECIDED` comment block records a ruling, which may name its ticket and its date on any
    of its lines.
    """

    def offence(subject: Subject) -> list[str]:
        found, in_ruling = [], False
        for n, line in enumerate(subject.text.splitlines(), 1):
            if not _COMMENT.search(line):
                in_ruling = False
                continue
            in_ruling = in_ruling or "DECIDED" in line
            if (
                pattern.search(line)
                and not in_ruling
                and not any(marker in line for marker in _EXEMPT)
            ):
                found.append(f"line {n}: {line.strip()}")
        return found

    return offence


COMMENTS_CITE_NO_TICKET = Census(
    name="comments-cite-no-ticket",
    reason=(
        "A ticket number in a code comment is provenance: which issue asked for the code. "
        "`~/.claude/rules/comments.md` puts that in the commit message, where `git log -L` "
        "finds it, and keeps the comment to the constraint a maintainer needs. Restate the "
        "constraint without the number, or turn a pending item into `TODO: <link> - <why>`."
    ),
    files=lambda: tracked(*_COMMENTED),
    offence=_commented(_TICKET),
    red=(
        Subject("a.py", "x = 1\n# The lock must be held (#1234).\n"),
        Subject("a.sh", "# Issue 2135: the cd makes this the primary checkout.\n"),
        Subject("a.yml.j2", "{# since #4521 the cap is 3 #}\nkey: 3\n"),
        Subject("a.yml", "  # See https://github.com/o/r/pull/77 for why.\n"),
    ),
    green=(
        Subject(
            "a.py",
            "# TODO: https://github.com/o/r/issues/12 - drop once upstream fixes\n",
        ),
        Subject("a.py", "x = '#1234'  # a literal, not a comment\n"),
        Subject("a.sh", 'n=${#var}\n# The length of "$var" in bytes.\n'),
        Subject("a.py", "# DECIDED: keep the retry (#999 measured it).\n"),
    ),
    min_matches=1000,
    must_find=frozenset({".claude/hooks/run-hook.sh", "prek.toml"}),
)

COMMENTS_CARRY_NO_DATE = Census(
    name="comments-carry-no-date",
    reason=(
        "A date in a code comment is provenance: when a value was measured, a ruling made or "
        "an incident seen. `~/.claude/rules/comments.md` puts that in the commit message and "
        "keeps the comment to the constraint that still holds. A ruling a reviewer must not "
        "reopen is a `# DECIDED:` marker, which may carry its date."
    ),
    files=lambda: tracked(*_COMMENTED),
    offence=_commented(_DATE),
    red=(
        Subject("a.py", "# Measured on daniel-box on 2026-11-22: 17 GiB.\nx = 17\n"),
        Subject("a.yml", "  # (operator ruling, 2026-11-22). The cap holds.\n"),
        Subject("a.yml.j2", "{# retired 2026-08-14 #}\nkey: 1\n"),
        Subject(
            "a.yml", "# DECIDED: keep it.\nkey: 1\n# Seen on 2026-11-22.\nother: 2\n"
        ),
    ),
    green=(
        Subject("a.py", "# DECIDED: two cycles (operator ruling 2026-10-31).\n"),
        Subject(
            "a.yml", "# DECIDED: keep it (operator,\n# 2026-11-22). It holds.\nkey: 1\n"
        ),
        Subject("a.py", "# A date such as 2001-12-14 is read back as a string.\n"),
        Subject("a.py", "when = '2026-11-22'  # a literal, not a full-line comment\n"),
        Subject(
            "a.sh", "# TODO: https://github.com/o/r/issues/1 - drop after 2026-12-01\n"
        ),
    ),
    min_matches=1000,
    must_find=frozenset({".claude/hooks/run-hook.sh", "prek.toml"}),
    allow={
        "hosts/components/host-maintenance/files/ucf-dist-check.sh": (
            "the date decodes the epoch constant DPKG_SINCE on the line below it"
        ),
        "scripts/lib/facts/citations.py": "it quotes the history sentences the facts lint skips",
        "k8s/roles/monitor-bridge/tests/check_bodies/test_check_speedtest.py": (
            "the dates are the two serializations of the fixture row the test reads"
        ),
    },
)
