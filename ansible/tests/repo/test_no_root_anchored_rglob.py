"""No tracked Python file walks the filesystem from repo root with `.rglob(`.

Recurring-failure class 7 (docs/failure-classes.md): "the execution context is not the shell
you developed in." Its most recent instance was `_helpers.discover_docs()` and
`test_documented_paths_exist.py`'s corpus walk, both of which used a root-anchored `rglob` and
therefore descended into `.claude/worktrees/<name>/` — a full checkout per live session,
holding OLDER copies of the same docs. The guards then judged this commit against another
session's history. It broke `docs-refresh`'s commit for at least one cycle before anyone
noticed (see `_helpers.discover_docs`'s docstring and `test_documented_paths_exist.py:71` for
the full incident). Both were fixed by switching to `git ls-files`, which answers with what the
commit actually contains — the DECIDED marker at `test_documented_paths_exist.py:71` records
the choice.

This is the regression guard for that fix: a repo-root-anchored `rglob(` is exactly the shape
that walked into sibling worktrees, so a tracked `.py` file must not call `.rglob(` on `REPO`,
`REPO_ROOT`, `repo_root`, `repo`, or `Path(__file__).resolve().parents[N]` directly assigned to
one of those names. Scoping an `rglob` under a narrower subdirectory (`ansible/rglob(...)`,
`ROLES.rglob(...)`) is fine and common in this repo — `.claude/worktrees/` sits at repo root,
not under any of those subtrees, so a scoped rglob cannot reach it. Discovery is via
`git ls-files`, not a filesystem walk, for the same reason this guard exists.

Run: uv run pytest ansible/tests/repo/test_no_root_anchored_rglob.py
"""

import re
import subprocess

from _helpers import REPO

# Matches `<name>.rglob(` where <name> is one of the repo-root variable names this repo uses,
# case-sensitive to the conventions seen in `_helpers.py` and the `ansible/tests/` guards.
ROOT_ANCHORED_RGLOB = re.compile(r"\b(REPO|REPO_ROOT|repo_root|repo)\.rglob\(")

# test_adr_links.py rglobs from REPO but filters every result through a `SKIP_PARTS` set that
# names "worktrees" explicitly, with its own comment on the same hazard this guard exists for
# (its `_source_files` docstring). That is a second, independently-argued mitigation for the
# same class-7 incident, not an unguarded instance — named here rather than left to the regex
# to relearn what its own comment already states.
ALREADY_MITIGATED = {"ansible/tests/repo/test_adr_links.py"}


def _tracked_python_files() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [rel for rel in listed.split("\0") if rel]


def test_the_scan_finds_tracked_python_files():
    """Without this, the test below passes vacuously on an empty file list."""
    assert len(_tracked_python_files()) >= 100


SELF = "ansible/tests/repo/test_no_root_anchored_rglob.py"


def test_no_tracked_file_rglobs_from_repo_root():
    # A scanner must not scan itself: this file's own red-proof fixtures below quote the
    # exact `REPO.rglob(` shape it is looking for, as strings rather than code.
    offenders = []
    for rel in _tracked_python_files():
        if rel in ALREADY_MITIGATED or rel == SELF:
            continue
        text = (REPO / rel).read_text(errors="replace")
        if ROOT_ANCHORED_RGLOB.search(text):
            offenders.append(rel)
    assert not offenders, (
        f"{offenders} call .rglob() directly on a repo-root path. That walks whatever is on "
        f"disk, including other sessions' `.claude/worktrees/<name>/` checkouts, and judges "
        f"this commit against their older copies of the same files. Use `git ls-files` "
        f"(see _helpers.discover_docs) or scope the rglob under a narrower subdirectory that "
        f"cannot contain `.claude/worktrees/`."
    )


def test_the_pattern_rejects_a_root_anchored_rglob_and_accepts_a_scoped_one():
    """Red-proof pair for ROOT_ANCHORED_RGLOB itself."""
    assert ROOT_ANCHORED_RGLOB.search("for p in REPO.rglob('*.md'): ...")
    assert not ROOT_ANCHORED_RGLOB.search("for p in ROLES.rglob('*.md'): ...")
    assert not ROOT_ANCHORED_RGLOB.search(
        "for p in (REPO / 'ansible').rglob('*.md'): ..."
    )
