"""Every `CLAUDE.md` section lints clean: `lint_sections` reports no error rule over the tree.

It reads one commit and stores nothing. `vanished-identifier` needs a range, so the
`facts-lint-changed` prek hook runs that rule instead.
"""

from _helpers import REPO
from lib.facts.citations import repo_citations
from lib.facts.lint import lint_sections
from lib.git_testing import git, init_repo


def _errors(repo):
    return [f for f in lint_sections(repo, None) if not f.warn]


def test_every_section_has_no_lint_errors():
    errors = _errors(REPO)
    assert not errors, "\n".join(f"{f.unit}: {f.rule} — {f.detail}" for f in errors)


def test_the_walk_reaches_every_doc_it_must():
    keys = repo_citations(REPO)
    assert len(keys) >= 400
    assert any(k.startswith("CLAUDE.md#") for k in keys)
    assert any(k.startswith("k8s/roles/traefik/CLAUDE.md#") for k in keys)


def _repo_citing(tmp_path, atom):
    init_repo(tmp_path)
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "m.py").write_text("LIMIT = 85\n")
    (tmp_path / "CLAUDE.md").write_text(f"## Gate\n`{atom}` bounds it.\n")
    git(tmp_path, "add", "-A")
    return tmp_path


def test_a_section_citing_a_missing_symbol_is_flagged(tmp_path):
    errors = _errors(_repo_citing(tmp_path, "t/m.py:GONE"))
    assert [(f.unit, f.rule) for f in errors] == [("CLAUDE.md#Gate", "unresolved-atom")]


def test_a_section_citing_a_symbol_that_exists_is_clean(tmp_path):
    assert not _errors(_repo_citing(tmp_path, "t/m.py:LIMIT"))
