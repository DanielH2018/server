"""The census row holding code comments free of ticket and PR references."""

import re

from _row_table import Census, Subject, tracked

_COMMENTED = (
    "*.py", "*.sh", "*.yml", "*.yaml", "*.toml", "*.j2", "*.jinja",
    "*.conf", "*.service", "*.timer", "*.alloy",
)  # fmt: skip

# A line that opens with `#` or a Jinja `{#`. A comment after code is skipped: telling it from
# a `#` inside a string needs a tokenizer per language.
_COMMENT = re.compile(r"^\s*(?:#(?!!)|\{#)")

_TICKET = re.compile(
    r"(?<![\w$/{&])#\d{3,}\b|\b(?:[Ii]ssue|PR) #?\d{3,}\b|/(?:issues|pull)/\d+"
)

# A TODO keeps its link, and the other markers are text a tool or a citation reads.
_EXEMPT = (
    "TODO", "DECIDED:", "fact:", "renovate:", "gen-hooks:", "ENFORCED", "Non-vacuity:",
)  # fmt: skip

_ALLOW = {
    "scripts/deploy_tools/chained_timeout.py": (
        "test_chained_timeout.py parses the `#5868: <n>s` measurement out of this comment"
    ),
    "scripts/dev/findings_lib/issue_model.py": "the issue-URL closing syntax is the example",
    "tests/_ratchet.py": "the `#NNNN` issue-reference syntax is the example",
    "tests/repo/test_role_claude_md.py": "its DECIDED paragraph cites the operator ruling",
}


def _ticket_offence(subject: Subject) -> list[str]:
    return [
        f"line {n}: {line.strip()}"
        for n, line in enumerate(subject.text.splitlines(), 1)
        if _COMMENT.search(line)
        and _TICKET.search(line)
        and not any(marker in line for marker in _EXEMPT)
    ]


COMMENTS_CITE_NO_TICKET = Census(
    name="comments-cite-no-ticket",
    reason=(
        "A ticket number in a code comment is provenance: which issue asked for the code. "
        "`~/.claude/rules/comments.md` puts that in the commit message, where `git log -L` "
        "finds it, and keeps the comment to the constraint a maintainer needs. Restate the "
        "constraint without the number, or turn a pending item into `TODO: <link> - <why>`."
    ),
    files=lambda: tracked(*_COMMENTED),
    offence=_ticket_offence,
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
    allow=_ALLOW,
)
