"""The renderer's non-test line count stays at or under the ceiling the operator accepted.

R4 exit 6 bounded the renderer at 2,600 lines and it read 13,549. The operator accepted the
overrun on 2026-11-07 ("accept the cap overrun"). The ceiling is the size measured when R4.9
merged, 13,546 lines, so the renderer may shrink but not grow. A PR that grows it raises LINE_CEILING in its own diff,
which puts the growth in front of a reviewer.

Run: uv run pytest scripts/renderer/tests/test_renderer_size_ceiling.py
"""

import subprocess

from lib.repo_paths import REPO

# 13574 once H7.34 named the drill script and daniel-server's k3s inputs to the host renderer.
LINE_CEILING = 13574
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


def over_ceiling(names: list[str], ceiling: int) -> str | None:
    """The refusal when the non-test files among `names` hold more than `ceiling` lines."""
    lines = sum(
        (REPO / name).read_bytes().count(b"\n")
        for name in names
        if "/tests/" not in name
    )
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
    refusal = over_ceiling(tracked(), LINE_CEILING - 1)
    assert (
        refusal
        == f"the renderer reads {LINE_CEILING} lines against the ceiling of {LINE_CEILING - 1}"
    )
