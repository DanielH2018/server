"""The Vale hook's `files:` regex must cover everything `.vale.ini` puts in scope.

Vale exits 0 on a file it has no section for, so the two registries fail in opposite
directions and only one of them is visible. A path in `.vale.ini` but not in the hook's
`files:` is never handed to Vale at all: the hook reports "no files to check", the commit
goes green, and the page is ungated forever. That is not hypothetical — `docs/deploying.md`
and `docs/gitops-pipeline.md` were added to `.vale.ini` in PR #418 and were never added to
the regex, so they went unlinted from the day they were written.

The duplication itself is deliberate (see the comment above the hook in prek.toml); this
test is what makes it safe.
"""

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VALE_INI = REPO / ".vale.ini"
PREK_TOML = REPO / "prek.toml"

_SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$", re.MULTILINE)
_BRACE_RE = re.compile(r"\{([^}]*)\}")


def _expand(pattern: str) -> list[str]:
    """Expand a single `{a,b}` alternation, and turn a trailing `*.md` into a sample name.

    Vale section headers are globs. The regex under test matches concrete paths, so a
    glob has to become at least one path that a real file could have.
    """
    match = _BRACE_RE.search(pattern)
    if match:
        return [
            expanded
            for alt in match.group(1).split(",")
            for expanded in _expand(
                pattern[: match.start()] + alt + pattern[match.end() :]
            )
        ]
    return [pattern.replace("*.md", "sample.md")]


def _clears_all_styles(body: str) -> bool:
    """True for an opt-out section -- one whose first setting empties BasedOnStyles.

    docs/archive/ and docs/reference/ get a section precisely so every rule is off there.
    They are the opposite of in-scope, so the coverage check below must not demand the hook
    hand them to Vale.
    """
    first = next((line for line in body.splitlines() if line.strip()), "")
    return first.replace(" ", "") == "BasedOnStyles="


def vale_scoped_paths() -> list[str]:
    """Every concrete path `.vale.ini` puts rules in force for."""
    text = VALE_INI.read_text()
    sections = _SECTION_RE.findall(text)
    scoped = []
    for section in sections:
        if _clears_all_styles(text.split(f"[{section}]", 1)[1]):
            continue
        scoped.extend(_expand(section))
    return scoped


def vale_hook_regex() -> str:
    config = tomllib.loads(PREK_TOML.read_text())
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == "vale"
    ]
    assert len(hooks) == 1, f"expected exactly one vale hook, found {len(hooks)}"
    return hooks[0]["files"]


def test_the_hook_regex_covers_every_vale_ini_section():
    pattern = re.compile(vale_hook_regex())
    scoped = vale_scoped_paths()
    missed = [path for path in scoped if not pattern.match(path)]
    assert not missed, (
        f"in .vale.ini but never handed to Vale by the prek hook: {missed}. "
        f"Add them to `files:` in prek.toml, or the pages lint clean by never being linted."
    )


def test_the_scope_is_not_empty():
    """A .vale.ini that parses to nothing would make the test above vacuously true."""
    scoped = vale_scoped_paths()
    assert scoped, "no sections parsed out of .vale.ini — has the syntax moved?"
    assert "docs/sample.md" in scoped, scoped
    assert "docs/adr/sample.md" in scoped, scoped


def test_the_hook_regex_matches_the_documents_on_disk():
    """The section globs could both parse and still cover nothing real.

    Checking against actual files is what catches a scope that is syntactically fine and
    factually empty — a regex matching `docs/*.markdown`, say.
    """
    pattern = re.compile(vale_hook_regex())
    top_level = sorted(p.name for p in (REPO / "docs").glob("*.md"))
    assert len(top_level) >= 15, (
        f"only {len(top_level)} top-level docs found: {top_level}"
    )
    unmatched = [name for name in top_level if not pattern.match(f"docs/{name}")]
    assert not unmatched, f"top-level docs the Vale hook would skip: {unmatched}"

    adrs = sorted(p.name for p in (REPO / "docs" / "adr").glob("*.md"))
    assert len(adrs) >= 14, f"only {len(adrs)} ADRs found"
    assert not [name for name in adrs if not pattern.match(f"docs/adr/{name}")]


def test_the_hook_never_covers_generated_reference_pages():
    """docs/reference/ is written by the docs-refresh cron, which commits with hooks running.

    A style error there aborts that commit and alerts on every run until the generator is
    fixed. The prose lives in the generator anyway, where it is reviewed as code.
    """
    pattern = re.compile(vale_hook_regex())
    assert not pattern.match("docs/reference/services.md")
    assert not pattern.match("docs/reference/hosts.md")


def test_vale_ini_turns_the_styles_off_for_archive_and_reference():
    """The hook regex must not be the only thing excluding those trees.

    Vale's section globs cross directory separators, so `[docs/*.md]` also matches
    docs/archive/README.md and every docs/reference/ page -- measured at 101 findings in
    reference/ before the opt-out sections existed. Without them a bare `vale docs` lints
    generated pages, and the cron's protection lives in prek.toml rather than in the config
    that documents it.
    """
    text = VALE_INI.read_text()
    for tree in ("archive", "reference"):
        section = f"[docs/{tree}/**]"
        assert section in text, f"{section} missing from .vale.ini"
        body = text.split(section, 1)[1]
        first = next(line for line in body.splitlines() if line.strip())
        assert first.replace(" ", "") == "BasedOnStyles=", (
            f"{section} must clear BasedOnStyles to disable every rule, found {first!r}"
        )
