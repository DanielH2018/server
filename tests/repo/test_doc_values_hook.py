"""The `regen-doc-values` prek hook fires on every file a value row reads.

A row whose doc or source the hook's `files` pattern misses is checked only in CI, after
the commit that made it stale. The pattern is written by hand in `prek.toml`, so this holds
it against `VALUES`.

Run: uv run pytest tests/repo/test_doc_values_hook.py
"""

import re
import tomllib

from _helpers import REPO

import doc_values

HOOK_ID = "regen-doc-values"
CRON = "hosts/components/docs-refresh/templates/docs-refresh.sh.j2"


def _hooks() -> list[dict]:
    config = tomllib.loads((REPO / "prek.toml").read_text())
    return [hook for repo in config["repos"] for hook in repo.get("hooks", [])]


def _hook() -> dict:
    [hook] = [hook for hook in _hooks() if hook.get("id") == HOOK_ID]
    return hook


def _unmatched(paths: set[str], pattern: str) -> list[str]:
    return sorted(p for p in paths if not re.search(pattern, p))


def test_the_hook_runs_the_fixer_before_the_tests_that_read_the_docs():
    ids = [hook.get("id") for hook in _hooks()]
    assert _hook()["entry"].endswith("python scripts/docs/doc_values.py")
    assert ids.index(HOOK_ID) < ids.index("static-ratchet-tests")


def test_the_gate_matches_every_doc_and_source_a_row_names():
    paths = {p for row in doc_values.VALUES for p in (row.doc, row.source)}
    assert ".claude/rules/generated-docs.md" in paths
    assert _unmatched(paths, _hook()["files"]) == []


def test_a_row_the_gate_misses_is_flagged():
    assert _unmatched({"docs/landing.md"}, _hook()["files"]) == ["docs/landing.md"]


def test_the_gate_skips_every_path_the_docs_refresh_cron_stages():
    """A doc the hook rewrote in the cron's commit would be left unstaged."""
    staged = doc_values.staged_paths((REPO / CRON).read_text())
    assert "docs/reference" in staged
    # A staged directory stands for the files under it.
    fired = [
        path
        for path in staged
        if re.search(_hook()["files"], path)
        or re.search(_hook()["files"], f"{path}/page.md")
    ]
    assert fired == []
