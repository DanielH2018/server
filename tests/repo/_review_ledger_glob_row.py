"""The census row holding the review skills to a ledger listing that resolves for either user."""

import re

from _row_table import Census, Subject, lines_matching, text_lacks

LEDGER_GLOB = '"$HOME"/.claude/projects/-*-server/memory'
_LITERAL_STORE_PATH = re.compile(r"\.claude/projects/-home-[a-z]+-server/")
_LEDGER_LISTING_SKILLS = frozenset(
    {".claude/skills/homelab-review/SKILL.md", ".claude/skills/ha-review/SKILL.md"}
)
_names_a_literal_store = lines_matching(_LITERAL_STORE_PATH)
_lacks_the_glob = text_lacks(LEDGER_GLOB, "lists ledgers through the glob")


REVIEW_SKILLS_LIST_LEDGERS_FROM_ANY_USERS_STORE = Census(
    name="review-skills-list-ledgers-from-any-users-store",
    reason=(
        "Claude Code keys a memory store by the checkout path, so the operator's store is "
        "`-home-ubuntu-server` and the claude agent user's is `-home-claude-server`. A literal "
        "store path lists nothing for the other user, and zero ledgers is a valid state, so "
        f"the review primes from nothing without an error (#8318). `{LEDGER_GLOB}` resolves "
        "for either user."
    ),
    files=lambda: sorted(_LEDGER_LISTING_SKILLS),
    offence=lambda s: _names_a_literal_store(s) + _lacks_the_glob(s),
    red=(
        Subject("a.md", "ls -1 ~/.claude/projects/-home-ubuntu-server/memory/x.md"),
        Subject("a.md", "ls -1 ~/.claude/projects/-home-claude-server/memory/x.md"),
    ),
    green=(Subject("a.md", f"ls -1 {LEDGER_GLOB}/review-*-state.md"),),
    min_matches=len(_LEDGER_LISTING_SKILLS),
    must_find=_LEDGER_LISTING_SKILLS,
)
