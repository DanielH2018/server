"""The census row holding the read-only review skills to writing no tracked file."""

import re

from _row_table import Census, Subject, lines_matching

_READ_ONLY_SKILLS = frozenset(
    {".claude/skills/homelab-review/SKILL.md", ".claude/skills/ha-review/SKILL.md"}
)
_REDIRECT_INTO_THE_TREE = re.compile(
    r">>?\s*(?:evals|docs|scripts|k8s|hosts|inventory|\.claude)/"
)


READ_ONLY_REVIEW_SKILLS_WRITE_NO_TRACKED_FILE = Census(
    name="read-only-review-skills-write-no-tracked-file",
    reason=(
        "Both review skills declare themselves read-only and commit nothing. A step that "
        "redirects output into a tracked path contradicts that, and a run under a "
        "commit-nothing brief leaves the file in a scratch directory (#9617)."
    ),
    files=lambda: sorted(_READ_ONLY_SKILLS),
    offence=lines_matching(_REDIRECT_INTO_THE_TREE),
    red=(
        Subject("a.md", "printf '%s\\n' '{}' >> evals/review_outcomes.jsonl"),
        Subject("a.md", "uv run tool > docs/out.md"),
    ),
    green=(Subject("a.md", "uv run tool 2>&1 | head -5"),),
    min_matches=len(_READ_ONLY_SKILLS),
    must_find=_READ_ONLY_SKILLS,
)
