"""Every shared macro named in the operator docs must still exist in ansible/templates/.

`healthcheck.yml.j2` was deleted and its jittered-interval body inlined into the one compose
file that still used it, but three places went on naming it: the repo CLAUDE.md's macro list,
the `/new-container` skill's macro list, and -- the one that actually breaks -- that skill's
canonical skeleton, which opens with `{% from 'healthcheck.yml.j2' import healthcheck %}`.
Copying the skeleton produced a template that could not render (2026-08-25 review M-5).

Nothing caught it because the skeleton is prose: it lives inside a fenced block in a Markdown
file, so no renderer, linter or template validator ever reads it.

The original guard checked only two files (the repo CLAUDE.md and the `/new-container` skill),
which is why it stayed green while `.claude/agents/homelab-container-reviewer.md` went on
naming the same deleted macro in a live agent brief (2026-08-27 review). DOCS is now every
`CLAUDE.md` in the tree plus every `*.md` under `.claude/`, excluding retired trees whose docs
describe code that no longer runs.
"""

import re
from pathlib import Path

from _helpers import discover_docs

REPO = Path(__file__).resolve().parent.parent.parent
MACROS = REPO / "ansible" / "templates"

DOCS = discover_docs()

# A floor, not the real count -- catches the walk silently shrinking (a renamed root, a
# tightened exclude) without hardcoding a number that drifts every time a doc is added.
_MIN_DOCS = 50

# A `<name>.yml.j2`, whether bare (a `{% from %}` line or an inline bullet mention) or with
# one directory level in front of it -- widening DOCS to every CLAUDE.md pulled in role-local
# app config the roles happen to name with the same extension (`templates/config/config.yml.j2`
# in configarr, `templates/config/application.yml.j2` in janitorr). Capturing the directory
# lets the loop below tell those apart from a shared-macro reference, which this repo's docs
# always give bare or as `templates/<macro>.yml.j2` -- never `templates/config/...`.
NAMED = re.compile(r"\b((?:[a-z0-9_-]+/)?[a-z0-9_-]+\.yml\.j2)\b")


def test_the_corpus_covers_the_whole_doc_tree():
    """Coverage is asserted, not assumed -- a two-file DOCS list already let one leak."""
    assert len(DOCS) >= _MIN_DOCS, (
        f"only found {len(DOCS)} docs, expected at least {_MIN_DOCS} -- the walk shrank. "
        "A floor far below the real count cannot tell 'the walk broke' from 'docs were "
        "deleted'."
    )


def test_every_macro_named_in_the_docs_exists():
    available = {p.name for p in MACROS.glob("*.yml.j2")}
    assert available, "no macros found; has ansible/templates/ moved?"

    missing = []
    for doc in DOCS:
        if not doc.is_file():
            continue
        lines = doc.read_text().splitlines()

        # First pass: names a `config/`-qualified mention already ties to a role's own
        # rendered app config (configarr's, janitorr's) -- a later bare mention of the
        # same name in the same doc is that same file, not a fresh shared-macro claim.
        local_names = {
            bare
            for line in lines
            for raw in NAMED.findall(line)
            for prefix, _, bare in [raw.rpartition("/")]
            if prefix == "config"
        }

        for line_no, line in enumerate(lines, 1):
            for raw in NAMED.findall(line):
                _, _, bare = raw.rpartition("/")
                if bare in local_names:
                    continue
                # Compose/manifest templates a role owns, not shared macros.
                if bare in ("docker-compose.yml.j2",):
                    continue
                if bare not in available:
                    rel = doc.relative_to(REPO)
                    missing.append(f"{rel}:{line_no} names {bare}")

    assert not missing, (
        "the docs name a shared macro that no longer exists in ansible/templates/; a "
        "skeleton copied from there cannot render: " + "; ".join(missing)
    )
