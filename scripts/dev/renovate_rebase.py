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

A PR still inside its `minimumReleaseAge` is refused (exit 3) rather than ticked. Renovate
skips updating a soaking branch, including a requested rebase: it answers the tick with a
"Rebase not applied" comment and LEAVES THE BOX TICKED. A ticked box is then spent — a run
after the soak ends reads `already-ticked`, writes nothing, and there is no way left to ask
for the rebase. #2335 sat CONFLICTING for 38 hours in exactly that state and an agent read
the stall as a broken rebase (#2368, #2630). Leaving the box unticked keeps the post-soak
request working.

The soak status lags the soak itself, so "still PENDING" is not "still soaking". Renovate
writes `renovate/stability-days` when it processes the branch and never between runs, and
`renovate.json`'s `schedule` holds it to one run a day: #2335's status still read PENDING from
2026-09-24T01:31 on 2026-09-26T14:00, hours after its digest cleared its 3-day age at
2026-09-26T01:53, because that day's run fired 00:09-01:04 UTC — before the expiry. A refusal
here therefore lasts until Renovate's next run, and a branch that reads conflicted in that
window is waiting for Renovate rather than failing to rebase.

Usage:
    uv run python scripts/dev/renovate_rebase.py <pr-number>

Exit codes: 0 ticked (or already ticked); 1 the body carries no rebase checkbox;
2 `gh` failed (its stderr is printed); 3 the PR is still soaking, so nothing was ticked;
64 the argument is not one PR number.
"""

import json
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

# Renovate posts this commit status per branch: PENDING while the update is inside its
# `minimumReleaseAge`, SUCCESS once the soak is over. It is a `StatusContext` in the rollup,
# not a `CheckRun`, so it carries `context`/`state` rather than `name`/`conclusion`. The name
# lives GitHub-side rather than in `renovate.json`, so nothing here can pin it: a rename by
# Renovate returns this script to ticking through a soak, and no test would catch it.
SOAK_CONTEXT = "renovate/stability-days"

# The one seam: `gh(*args) -> CompletedProcess`, the signature of `lib.gh.gh`.
Gh = Callable[..., subprocess.CompletedProcess[str]]


def tick_rebase_box(body: str) -> tuple[str | None, str]:
    """(new body or None, what happened): `ticked`, `already-ticked` or `no-box`."""
    if UNTICKED in body:
        return body.replace(UNTICKED, TICKED, 1), "ticked"
    if TICKED in body:
        return None, "already-ticked"
    return None, "no-box"


def soaking(rollup: list[dict]) -> bool:
    """Is this PR's `renovate/stability-days` status pending?

    A PR with no such status is not soaking — Renovate posts it only where a
    `minimumReleaseAge` rule matched the dependency. `state` arrives uppercase from
    `gh pr view --json statusCheckRollup` and lowercase from the commit-status API.
    """
    return any(
        entry.get("context") == SOAK_CONTEXT
        and (entry.get("state") or "").upper() == "PENDING"
        for entry in rollup
    )


def main(argv: list[str] | None = None, gh: Gh = _gh, out=sys.stdout) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not argv[0].isdigit():
        print(__doc__, file=sys.stderr)
        return 64
    number = argv[0]
    try:
        # One read for both: the body carries the box, the rollup carries the soak status.
        view = json.loads(
            gh("pr", "view", number, "--json", "body,statusCheckRollup").stdout
        )
    except subprocess.CalledProcessError as exc:
        print(f"gh pr view {number} failed: {exc.stderr.strip()}", file=out)
        return 2
    body = view.get("body") or ""
    soak = soaking(view.get("statusCheckRollup") or [])
    new_body, outcome = tick_rebase_box(body)
    if outcome == "no-box":
        print(
            f"PR #{number}: body carries no `{UNTICKED}` — not a Renovate PR, or the box was edited away",
            file=out,
        )
        return 1
    if soak and outcome == "already-ticked":
        print(
            f"PR #{number}: rebase box already ticked AND `{SOAK_CONTEXT}` is pending — "
            "Renovate answered the tick with 'Rebase not applied' and will not rebase until "
            "the soak ends. Do not tick it again; wait, or force it with the branch's "
            "`unpend-branch` checkbox on the Dependency Dashboard (which bypasses the soak).",
            file=out,
        )
        return 3
    if soak:
        print(
            f"PR #{number}: `{SOAK_CONTEXT}` is pending — the update is still inside its "
            "`minimumReleaseAge`, so a rebase would not apply. Left the box unticked: a tick "
            "Renovate skips stays ticked and cannot be re-used after the soak. Wait, or force "
            "it with the branch's `unpend-branch` checkbox on the Dependency Dashboard.",
            file=out,
        )
        return 3
    if new_body is None:
        print(
            f"PR #{number}: rebase box already ticked; Renovate refreshes it on its next run",
            file=out,
        )
        return 0
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
