"""What a session working an issue would otherwise rediscover: the deploy plane and the tests.

WHY (#3955). Across 204 fan-out implementer transcripts, half of all tool calls were
exploration. `docs/gitops-pipeline.md` was re-read 22 times, mostly to work out whether and how
a change deploys, and 22 full-suite runs took up to 188s. Both answers follow from the paths an
issue cites, and both have a tool that already gives them.

- The deploy plane comes from `land.sh`'s own classifier: `land_tags.derive`, `plane_note` and
  `self_applied`, plus `land_shared.shared_caller_tags`. None of them needs git or the network;
  they read the checkout this module runs from. A second derivation here would drift from the
  one the landing applies.
- The tests come from the `tests/` sibling rule in `.claude/rules/python-layout.md`, checked
  against the files that exist.

Both are predictions from the cited paths. A change that touches other paths deploys
according to those too, which the block says.
"""

import re
import sys as _sys
from collections.abc import Iterable
from pathlib import Path as _Path

_SCRIPTS = _Path(__file__).resolve().parents[2]
# `land_tags` imports its siblings by bare name, as land.sh runs it.
_sys.path.insert(0, str(_SCRIPTS / "deploy_tools"))
_sys.path.insert(0, str(_SCRIPTS))

import land_shared  # noqa: E402
import land_tags  # noqa: E402

REPO = _SCRIPTS.parent


def deploy_line(paths: list[str]) -> str:
    """What `land.sh` would make of a PR that touches exactly `paths`."""
    tags = set(land_tags.derive(paths, len(paths)).tags)
    for callers in land_shared.shared_caller_tags(paths).values():
        tags |= callers
    parts = []
    if tags:
        parts.append(f"deploys tag {', '.join(sorted(tags))} (`deploy.sh --tags`)")
    if land_tags.self_applied(paths):
        parts.append("the GitOps tick applies part of it itself")
    note = land_tags.plane_note(paths)
    if note:
        parts.append(note)
    return "; ".join(parts) if parts else "nothing to deploy expected"


def _imports(stem: str) -> re.Pattern[str]:
    # `import x`, `from x import`, `from pkg.x import`, `import pkg.x`.
    return re.compile(
        rf"^\s*(?:from\s+(?:[\w.]+\.)?{stem}\s+import|import\s+(?:[\w.]+\.)?{stem}\b)",
        re.M,
    )


def covering_tests(paths: Iterable[str], repo: _Path = REPO) -> list[str]:
    """The existing test files that cover each cited module.

    The candidates are the test files under the `tests/` directories the sibling rule names:
    beside the module, beside its package (`scripts/dev/tests/` for
    `scripts/dev/fanout_lib/x.py`), and a role's own `tests/` for its `files/` module. A
    candidate counts when its name carries the module's stem or it imports the module, because
    names here follow the package (`test_fanout_brief.py` covers `fanout_lib/brief.py`). A cited
    test file is its own answer.
    """
    found = set()
    for path in paths:
        p = _Path(path)
        if p.suffix != ".py":
            continue
        if p.name.startswith("test_"):
            if (repo / p).is_file():
                found.add(path)
            continue
        dirs = {p.parent / "tests", p.parent.parent / "tests"}
        if "files" in p.parts:
            role = _Path(*p.parts[: p.parts.index("files")])
            dirs.add(role / "tests")
        pattern = _imports(p.stem)
        for d in dirs:
            for test in (repo / d).rglob("test_*.py"):
                text = test.read_text(errors="replace")
                if p.stem in test.stem or pattern.search(text):
                    found.add(str(test.relative_to(repo)))
    return sorted(found)


def precomputed(paths: list[str], repo: _Path = REPO) -> str:
    """The block, or "" when the issues cite no repo path."""
    if not paths:
        return ""
    tests = covering_tests(paths, repo)
    run = (
        f"`uv run pytest {' '.join(tests)}`"
        if tests
        else "none found by the `tests/` sibling rule"
    )
    return f"""## Precomputed from the cited paths
Derived from the paths the issues cite, by the tools that decide them; a change that touches
other paths deploys and tests by those too.
- Deploy, as `land.sh` classifies these paths: {deploy_line(paths)}.
- Tests covering the cited modules: {run}.
"""
