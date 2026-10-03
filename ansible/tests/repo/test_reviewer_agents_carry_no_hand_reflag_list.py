"""The reviewer agents point at one shared don't-re-flag source instead of carrying a copy.

The design facts live once, in `.claude/skills/homelab-review/accepted-designs.md`, one
section per reviewer domain; an inline "Honor accepted designs (don't re-flag): ..." list in
each agent would drift. The findings register (`--accepted` / `--refuted` closes) is rendered by the docs-refresh cron
into the *Settled findings* table of `docs/reference/backlog.md`. Each agent names both.

This guards the shape, not the content: a pointer dropping out, or a section heading naming a
domain `findings.py` does not know. An inline list creeping back into an agent is the
`agents-carry-no-inline-reflag-list` row of `test_census_rows_text.py` (#3430). What
a reviewer does with the list is a session's judgment, which no test here can see.

Run: uv run pytest ansible/tests/repo/test_reviewer_agents_carry_no_hand_reflag_list.py
"""

import re

import pytest
from _helpers import REPO
from dev.findings_lib.issue_model import DOMAINS

AGENTS = REPO / ".claude" / "agents"
SHARED = REPO / ".claude" / "skills" / "homelab-review" / "accepted-designs.md"
REGISTER = "docs/reference/backlog.md"

# The domain each pointed agent reviews, keyed by its file. A new reviewer agent that
# carries a domain section joins here so the pointer check covers it.
POINTED_AGENTS = {
    "homelab-container-reviewer.md": "container",
    "homelab-cicd-reviewer.md": "cicd",
    "homelab-backup-observability-reviewer.md": "backup-observability",
}


def _sections(text: str) -> list[str]:
    return re.findall(r"^## (\S+)$", text, flags=re.M)


def test_the_shared_file_holds_a_section_per_pointed_domain():
    """Non-vacuity: the file exists and carries every domain the pointers name."""
    assert SHARED.is_file()
    found = set(_sections(SHARED.read_text()))
    assert set(POINTED_AGENTS.values()) <= found, (
        f"accepted-designs.md lacks a section for {set(POINTED_AGENTS.values()) - found}"
    )


def test_every_section_in_the_shared_file_is_a_findings_domain():
    """A section heading is a `domain/<name>` label, so the register and the file group alike."""
    unknown = [d for d in _sections(SHARED.read_text()) if d not in DOMAINS]
    assert not unknown, f"sections naming no findings.py domain: {unknown}"


@pytest.mark.parametrize(("agent", "domain"), sorted(POINTED_AGENTS.items()))
def test_each_pointed_agent_names_its_domain_in_both_sources(agent, domain):
    text = (AGENTS / agent).read_text()
    assert f"`## {domain}`" in text and SHARED.name in text, (
        f"{agent} does not point at the `## {domain}` section of {SHARED.name}"
    )
    assert f"`### {domain}`" in text and REGISTER in text, (
        f"{agent} does not point at the `### {domain}` table of {REGISTER}"
    )
