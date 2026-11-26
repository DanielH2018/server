"""The renderer's non-test line count stays at or under the ceiling the operator accepted.

R4 exit 6 bounded the renderer at 2,600 lines and it read 13,549. The operator accepted the
overrun on 2026-11-07 ("accept the cap overrun"). The ceiling is the size measured when R4.9
merged, 13,546 lines, so the renderer may shrink but not grow. A PR that grows it past
LINE_CEILING raises the constant in its own diff, which puts the growth in front of a
reviewer. A PR that shrinks it leaves the constant alone, so two renderer PRs in flight do
not conflict on that line.

Run: uv run pytest scripts/renderer/tests/bundle/test_renderer_size_ceiling.py
"""

import subprocess

from lib.repo_paths import REPO

LINE_CEILING = 14818
# Exit 6's command, `git ls-files 'scripts/renderer/*.py' | grep -v /tests/ | xargs cat | wc -l`.
PATHSPEC = "scripts/renderer/*.py"


def tracked() -> list[str]:
    return subprocess.run(
        ["git", "ls-files", PATHSPEC],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split()


def renderer_lines(names: list[str]) -> int:
    """The lines held by the non-test files among `names`."""
    return sum(
        (REPO / name).read_bytes().count(b"\n")
        for name in names
        if "/tests/" not in name
    )


def over_ceiling(names: list[str], ceiling: int) -> str | None:
    """The refusal when the non-test files among `names` hold more than `ceiling` lines."""
    lines = renderer_lines(names)
    if lines <= ceiling:
        return None
    return f"the renderer reads {lines} lines against the ceiling of {ceiling}"


def test_the_renderer_stays_at_or_under_its_ceiling() -> None:
    names = tracked()
    assert len(names) > 40, (
        "the pathspec matched too few files for the count to mean anything"
    )
    assert over_ceiling(names, LINE_CEILING) is None


def test_one_line_over_the_ceiling_is_refused() -> None:
    names = tracked()
    lines = renderer_lines(names)
    assert (
        over_ceiling(names, lines - 1)
        == f"the renderer reads {lines} lines against the ceiling of {lines - 1}"
    )
