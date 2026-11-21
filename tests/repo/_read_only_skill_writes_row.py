"""The census row holding the read-only review skills to writing no tracked file."""

import re

from _row_table import Census, Subject, lines_matching

_READ_ONLY_SKILLS = frozenset(
    {".claude/skills/homelab-review/SKILL.md", ".claude/skills/ha-review/SKILL.md"}
)
_TREE = r"(?:evals|docs|scripts|k8s|hosts|inventory|tests|secrets|\.claude|\.github)/"
_WRITES_INTO_THE_TREE = re.compile(
    rf">>?\s*{_TREE}"
    rf"|\btee\s+{_TREE}"
    rf"|\b(?:write|writes|append|save|record)\b[^\n]*`{_TREE}[^`]*`"
    r"|\bgit (?:add|commit)\b",
    re.IGNORECASE,
)


READ_ONLY_REVIEW_SKILLS_WRITE_NO_TRACKED_FILE = Census(
    name="read-only-review-skills-write-no-tracked-file",
    reason=(
        "Both review skills declare themselves read-only and commit nothing. A step that "
        "writes, redirects or commits into a tracked path contradicts that, and a run under a "
        "commit-nothing brief leaves the file in a scratch directory (#9617)."
    ),
    files=lambda: sorted(_READ_ONLY_SKILLS),
    offence=lines_matching(_WRITES_INTO_THE_TREE),
    red=(
        Subject("a.md", "printf '%s\\n' '{}' >> evals/review_outcomes.jsonl"),
        Subject("a.md", "uv run tool > docs/out.md"),
        Subject(
            "a.md",
            "**Write that manifest as the ledger**, `evals/review_coverage/<date>.json`",
        ),
        Subject("a.md", "uv run tool | tee evals/out.json"),
    ),
    green=(
        Subject("a.md", "uv run tool 2>&1 | head -5"),
        Subject("a.md", "Read `evals/cases/homelab-review/007.json` for the case."),
    ),
    min_matches=len(_READ_ONLY_SKILLS),
    must_find=_READ_ONLY_SKILLS,
)
