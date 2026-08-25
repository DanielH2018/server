"""Every shared macro named in the operator docs must still exist in ansible/templates/.

`healthcheck.yml.j2` was deleted and its jittered-interval body inlined into the one compose
file that still used it, but three places went on naming it: the repo CLAUDE.md's macro list,
the `/new-container` skill's macro list, and -- the one that actually breaks -- that skill's
canonical skeleton, which opens with `{% from 'healthcheck.yml.j2' import healthcheck %}`.
Copying the skeleton produced a template that could not render (2026-08-25 review M-5).

Nothing caught it because the skeleton is prose: it lives inside a fenced block in a Markdown
file, so no renderer, linter or template validator ever reads it.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
MACROS = REPO / "ansible" / "templates"
DOCS = [
    REPO / "CLAUDE.md",
    REPO / ".claude" / "skills" / "new-container" / "SKILL.md",
]

# Bare `<name>.yml.j2`, whether in a `{% from %}` line or an inline mention in a bullet.
NAMED = re.compile(r"\b([a-z0-9_-]+\.yml\.j2)\b")


def test_every_macro_named_in_the_docs_exists():
    available = {p.name for p in MACROS.glob("*.yml.j2")}
    assert available, "no macros found; has ansible/templates/ moved?"

    missing = []
    for doc in DOCS:
        if not doc.is_file():
            continue
        for line_no, line in enumerate(doc.read_text().splitlines(), 1):
            for name in NAMED.findall(line):
                # Compose/manifest templates a role owns, not shared macros.
                if name in ("docker-compose.yml.j2",):
                    continue
                if name not in available:
                    rel = doc.relative_to(REPO)
                    missing.append(f"{rel}:{line_no} names {name}")

    assert not missing, (
        "the docs name a shared macro that no longer exists in ansible/templates/; a "
        "skeleton copied from there cannot render: " + "; ".join(missing)
    )
