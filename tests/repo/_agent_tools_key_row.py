"""The census row holding every agent file's tool allowlist to the `tools:` frontmatter key."""

import re

from _row_table import Census, Subject, tracked
from lib import yaml_fast

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _skill_tools_key_offence(subject: Subject) -> list[str]:
    head = _FRONTMATTER.match(subject.text)
    meta = (yaml_fast.safe_load(head.group(1)) if head else None) or {}
    return ["frontmatter key `allowed-tools`"] if "allowed-tools" in meta else []


AGENTS_LIMIT_TOOLS_WITH_THE_TOOLS_KEY = Census(
    name="agents-limit-tools-with-the-tools-key",
    reason=(
        "An agent's tool allowlist is the `tools:` frontmatter key. `allowed-tools:` is the "
        "skill key, and Claude Code ignores it in an agent file, so the agent gets every tool "
        "and a report-only reviewer can write files (#7979)."
    ),
    files=lambda: tracked(".claude/agents/*.md"),
    offence=_skill_tools_key_offence,
    red=(Subject("a.md", "---\nname: a\nallowed-tools: Read, Grep\n---\nBody.\n"),),
    green=(
        Subject(
            "a.md", "---\nname: a\ntools: Read, Grep\n---\nNever `allowed-tools:`.\n"
        ),
        Subject("a.md", "A guide with no frontmatter.\n"),
    ),
    must_find=frozenset({".claude/agents/skeptic.md"}),
)
