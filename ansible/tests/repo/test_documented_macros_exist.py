"""Every shared macro named in the operator docs must still exist in ansible/templates/.

A deleted macro can stay named in several places: the repo CLAUDE.md's macro list, the
`/new-container` skill's macro list, a live agent brief, and -- the one that actually breaks --
that skill's canonical skeleton, which opens with a `{% from '<macro>.yml.j2' import ... %}`.
Copying the skeleton then produces a template that cannot render.

Nothing else catches it because the skeleton is prose: it lives inside a fenced block in a
Markdown file, so no renderer, linter or template validator ever reads it.

DOCS is every `CLAUDE.md` in the tree plus every `*.md` under `.claude/`, excluding retired
trees whose docs describe code that no longer runs, and excluding the generated findings
register (see `GENERATED_REGISTER` below).
"""

import re

from _helpers import REPO, discover_docs

MACROS = REPO / "ansible" / "templates"

# `docs/reference/backlog.md` renders the open-findings register, so its rows are GitHub issue
# titles rather than prose this repo wrote. An issue that proposes deleting a macro names that
# macro in its title, which would redden this guard until the issue closes. Nothing is copyable from a register
# row, which is the harm this guard exists to catch. Same shape and same reason as
# `test_documented_paths_exist.py::test_node_corpus_excludes_the_generated_decisions_page`.
GENERATED_REGISTER = "docs/reference/backlog.md"

DOCS = [d for d in discover_docs() if not d.as_posix().endswith(GENERATED_REGISTER)]

# THE CORPUS FLOOR LIVES IN test_documented_paths_exist.py, not here.
# `discover_docs()` is shared, and that module's `test_the_corpus_covers_the_whole_doc_tree`
# floors it at 100. A second floor here could only fire in a run where the stronger one already
# had, and two floors on one corpus invite them to drift apart. One corpus, one floor.

# A `<name>.yml.j2`, whether bare (a `{% from %}` line or an inline bullet mention) or with
# one directory level in front of it -- role-local app config shares the extension
# (`templates/config/config.yml.j2` in configarr, `templates/config/application.yml.j2` in
# janitorr). Capturing the directory
# lets the loop below tell those apart from a shared-macro reference, which this repo's docs
# always give bare or as `templates/<macro>.yml.j2` -- never `templates/config/...`.
NAMED = re.compile(r"\b((?:[a-z0-9_-]+/)?[a-z0-9_-]+\.yml\.j2)\b")


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


def test_the_corpus_skips_the_generated_register_and_nothing_else():
    """The register is skipped; an ordinary doc still is not.

    Without the positive control, a corpus that stopped being built at all would pass the
    first assertion.
    """
    posix = [d.as_posix() for d in DOCS]
    assert not any(p.endswith(GENERATED_REGISTER) for p in posix)
    assert any(p.endswith("docs/adr/index.md") for p in posix)
