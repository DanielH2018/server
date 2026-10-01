"""The review skills and agents name the standing-list memory by the slug the store holds.

The standing-list memory file is `review-standing-decisions`. A missing memory is not an
error the harness reports, so a skill that primes from any other slug proceeds unprimed and
the fan-out re-derives settled decisions.

The memory store lives outside the repo (`~/.claude/projects/-home-ubuntu-server/memory/`),
is not tracked by git or chezmoi, and does not exist in CI, so no test here can assert a file
is in it. What a test CAN hold is the invariant that made the drift possible: one slug, spelled
the same in every place a reviewer reads it. A retired spelling coming back fails here.

Run: uv run pytest ansible/tests/repo/test_review_skills_name_one_standing_list_slug.py
"""

import pytest
from _helpers import REPO

# The slug the memory store holds, and the spellings that must not come back.
STANDING_SLUG = "review-standing-decisions"
RETIRED_SLUGS = ("homelab-review-standing-donot-reflag",)

# Non-vacuity: the files that must name the slug. A glob over a moved directory matches
# nothing, and a check over an empty census passes on nothing.
MUST_NAME = (
    ".claude/skills/homelab-review/SKILL.md",
    ".claude/agents/skeptic.md",
    ".claude/skills/ha-review/SKILL.md",
)

SEARCHED = (".claude/skills", ".claude/agents", "evals/cases")


def retired_slugs_in(text: str) -> list[str]:
    """The retired standing-list spellings this text still carries."""
    return [slug for slug in RETIRED_SLUGS if slug in text]


def _searched_files():
    for rel in SEARCHED:
        yield from sorted((REPO / rel).rglob("*.md"))
        yield from sorted((REPO / rel).rglob("*.json"))


def test_a_retired_spelling_is_flagged():
    """Red proof: the reject half of the pair."""
    assert retired_slugs_in(
        "prime from `homelab-review-standing-donot-reflag` first"
    ) == ["homelab-review-standing-donot-reflag"]


def test_the_live_spelling_is_clean():
    """Red proof: the accept half of the pair."""
    assert retired_slugs_in(f"prime from `{STANDING_SLUG}` first") == []


@pytest.mark.parametrize("rel", MUST_NAME)
def test_each_primed_surface_names_the_live_slug(rel):
    assert STANDING_SLUG in (REPO / rel).read_text(), (
        f"{rel} no longer names `{STANDING_SLUG}`, the standing-list memory the store holds"
    )


def test_no_searched_file_carries_a_retired_slug():
    files = list(_searched_files())
    names = {str(p.relative_to(REPO)) for p in files}
    assert set(MUST_NAME) <= names, f"census lost {set(MUST_NAME) - names}"
    offenders = {
        str(p.relative_to(REPO)): retired_slugs_in(p.read_text())
        for p in files
        if retired_slugs_in(p.read_text())
    }
    assert not offenders, (
        f"retired standing-list slug in {offenders}; the memory store holds "
        f"`{STANDING_SLUG}`, and a slug it does not hold primes nothing"
    )
