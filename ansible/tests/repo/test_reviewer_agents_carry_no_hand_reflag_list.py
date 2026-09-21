"""The reviewer agents point at one shared don't-re-flag source instead of carrying a copy.

#2169: three reviewer agents each carried an inline "Honor accepted designs (don't re-flag):
..." list, and the copies drifted. The design facts now live once, in
`.claude/skills/homelab-review/accepted-designs.md`, one section per reviewer domain; the
findings register (`--accepted` / `--refuted` closes) is rendered by the docs-refresh cron
into the *Settled findings* table of `docs/reference/backlog.md`. Each agent names both.

This guards the shape, not the content: an inline list creeping back into an agent, a
pointer dropping out, or a section heading naming a domain `findings.py` does not know. What
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

# The inline form: the heading phrase followed by a colon and the list itself. The pointer
# form ends the phrase with a full stop, so this matches the drifting copy and not its
# replacement.
INLINE_LIST = re.compile(r"Honor accepted designs \(don't re-flag\):")


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


def test_no_agent_carries_an_inline_reflag_list():
    agents = {p.name: p.read_text() for p in AGENTS.glob("*.md")}
    # Non-vacuity: a glob over a moved directory matches nothing, and an empty offender list
    # then passes on nothing. The pointed agents are the census this check must find.
    assert set(POINTED_AGENTS) <= set(agents), (
        f"agents missing: {set(POINTED_AGENTS) - set(agents)}"
    )
    offenders = sorted(
        name for name, text in agents.items() if INLINE_LIST.search(text)
    )
    assert not offenders, (
        f"{offenders} carry an inline don't-re-flag list; the list lives once in "
        f"{SHARED.relative_to(REPO)} and the register in {REGISTER}"
    )


@pytest.mark.parametrize(("agent", "domain"), sorted(POINTED_AGENTS.items()))
def test_each_pointed_agent_names_its_domain_in_both_sources(agent, domain):
    text = (AGENTS / agent).read_text()
    assert f"`## {domain}`" in text and SHARED.name in text, (
        f"{agent} does not point at the `## {domain}` section of {SHARED.name}"
    )
    assert f"`### {domain}`" in text and REGISTER in text, (
        f"{agent} does not point at the `### {domain}` table of {REGISTER}"
    )
