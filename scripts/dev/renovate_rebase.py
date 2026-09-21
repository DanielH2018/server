#!/usr/bin/env python3
"""Tick the rebase checkbox in a Renovate PR's body so Renovate refreshes the branch.

A Renovate branch pins what was current when it was cut — the base commit and the image
digest. When either has moved, the refresh is Renovate's job, not a hand edit: the next run
would rewrite a hand-bumped digest anyway. Renovate watches for its own checkbox in the PR
body, `- [ ] <!-- rebase-check -->`, and rebases within a cycle once it reads `[x]`. The
`renovate-prs` skill (§5) did this as `mktemp` → `sed` → `gh pr edit --body-file`; this is
that sequence as one command (#2163).

Idempotent: a box already ticked writes nothing and exits 0. A body with no box at all is
an error, not a no-op — Renovate did not author that PR, or the box was edited away, and
either way a `gh pr edit` would change nothing and report success.

Usage:
    uv run python scripts/dev/renovate_rebase.py <pr-number>

Exit codes: 0 ticked (or already ticked); 1 the body carries no rebase checkbox;
2 `gh` failed (its stderr is printed); 64 the argument is not one PR number.
"""

import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path as _Path

# Reach `lib`: a directly-invoked script gets only its own directory on sys.path, and
# pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.gh import gh as _gh

UNTICKED = "- [ ] <!-- rebase-check -->"
TICKED = "- [x] <!-- rebase-check -->"

# The one seam: `gh(*args) -> CompletedProcess`, the signature of `lib.gh.gh`.
Gh = Callable[..., subprocess.CompletedProcess[str]]


def tick_rebase_box(body: str) -> tuple[str | None, str]:
    """(new body or None, what happened): `ticked`, `already-ticked` or `no-box`."""
    if UNTICKED in body:
        return body.replace(UNTICKED, TICKED, 1), "ticked"
    if TICKED in body:
        return None, "already-ticked"
    return None, "no-box"


def main(argv: list[str] | None = None, gh: Gh = _gh, out=sys.stdout) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not argv[0].isdigit():
        print(__doc__, file=sys.stderr)
        return 64
    number = argv[0]
    try:
        body = gh("pr", "view", number, "--json", "body", "-q", ".body").stdout
    except subprocess.CalledProcessError as exc:
        print(f"gh pr view {number} failed: {exc.stderr.strip()}", file=out)
        return 2
    new_body, outcome = tick_rebase_box(body)
    if new_body is None:
        if outcome == "already-ticked":
            print(
                f"PR #{number}: rebase box already ticked; Renovate refreshes it on its next run",
                file=out,
            )
            return 0
        print(
            f"PR #{number}: body carries no `{UNTICKED}` — not a Renovate PR, or the box was edited away",
            file=out,
        )
        return 1
    # `--body-file` rather than `--body`: the body is markdown with backticks and newlines,
    # and a file is the one shape no shell quoting can mangle.
    with tempfile.TemporaryDirectory() as tmp:
        body_file = _Path(tmp) / "body.md"
        body_file.write_text(new_body)
        try:
            gh("pr", "edit", number, "--body-file", str(body_file))
        except subprocess.CalledProcessError as exc:
            print(f"gh pr edit {number} failed: {exc.stderr.strip()}", file=out)
            return 2
    print(
        f"PR #{number}: rebase box ticked; Renovate refreshes the branch within a cycle",
        file=out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
