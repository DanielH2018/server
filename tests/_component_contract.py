"""The `## Autonomous-role contract` sections of a host component's `CLAUDE.md`.

The census row in `tests/repo/_component_contract_row.py` and each component's own test in
`hosts/lib/tests/` read a contract through this module.
"""

import re

from lib.repo_paths import REPO

HEADING = "Autonomous-role contract"


def sections(doc: str) -> list[str]:
    """Each `## Autonomous-role contract` section of `doc`, up to the next `## ` heading."""
    return re.findall(rf"^## {HEADING}.*?(?=^## |\Z)", doc, re.MULTILINE | re.DOTALL)


def component_contract(component: str) -> str:
    """Every contract section of `hosts/components/<component>/CLAUDE.md`, "" when it has none."""
    doc = REPO / "hosts" / "components" / component / "CLAUDE.md"
    return "\n".join(sections(doc.read_text())) if doc.is_file() else ""
