"""The `findings` source for cc-wait: whether the register gained a free issue since a baseline.

`cc-wait findings [--repo OWNER/NAME] [--after N]` runs this through
`.claude/wait-sources/findings`. It answers the question an orchestrator with an empty queue
asks, "has anything new been filed that I could pick up?", without re-running
`findings.py next` in a loop.

    findings.py new-issues --describe              # terminal states, as JSON
    findings.py new-issues [--repo R] [--after N]  # the state now, one JSON line

THE STATE.

    none                (open)  no free issue numbered above the baseline
    new                 0       one or more; the detail is JSON naming each one
    gh-unauthenticated  4       gh exited 4: no login is configured, which no wait fixes

cc-wait refuses a source whose terminal states all exit 0, since it could never report a
failure, so `gh-unauthenticated` is the one failure that ends the wait.

"Free" is `claim.free_issues`, the definition `next` prints, so the wait ends exactly when
`next` would list an issue above the baseline. A new issue that is claimed, deferred,
blocked, `manual` or named by an open PR keeps the wait open.

THE BASELINE. `--after N` is the baseline, and survives a resume unchanged. With no
`--after`, the baseline is the highest open issue number when the wait starts. cc-wait keeps
no state between reads, so a file under `$XDG_CACHE_HOME/findings-wait/`, keyed on the
register and the working directory, holds it: `--describe`, which cc-wait runs once per
wait, deletes it, and the first read that reaches GitHub records it. A resumed wait with no
`--after` therefore re-baselines, and an issue filed between the two runs is missed. Every
`none` detail names the baseline so a resume can pass it as `--after`.

ANY OTHER FAILED READ EXITS 1, through `findings.main`'s gh-failure handler, and prints
nothing on stdout, so cc-wait retries it. It is never read as "nothing new".

ONE REST REQUEST A QUIET READ. The newest open issues come from one `gh api` call. A new
issue costs one more call for open PR bodies, plus one per thread it carries (comments,
open blockers). The interval is 300s; see `gh_calls.REST_TIMEOUT` for why REST.
"""

import argparse
import hashlib
import json
import os
import subprocess
from datetime import date
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.claim import free_issues
from dev.findings_lib.gh_calls import (
    newest_open_issues,
    rest_open_pr_refs,
    threaded_issues,
)
from dev.findings_lib.issue_model import PR_REPO, today_utc

TERMINAL = {"new": 0, "gh-unauthenticated": 4}

# gh's documented exit code for "authentication required".
GH_AUTH_REQUIRED = 4

# The fields an orchestrator groups and launches on without another read.
REPORTED = (
    "number",
    "title",
    "severity",
    "paths",
    "no_vetted_remediation",
    "solo_only",
)


def baseline_file(repo: str | None, cwd: Path) -> Path:
    """Where a wait with no `--after` on ``repo``'s register, run from ``cwd``, keeps its baseline."""
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    key = hashlib.sha256(f"{repo or PR_REPO}\0{cwd}".encode()).hexdigest()[:16]
    return cache / "findings-wait" / f"{key}.json"


def describe(baseline: Path | None) -> dict:
    """cc-wait's description, after forgetting any baseline a previous wait left at ``baseline``."""
    if baseline is not None:
        baseline.unlink(missing_ok=True)
    # DECIDED: not `remote`. cc-wait would serve another wait's cached `none` on this wait's
    # first tick, so the baseline would be recorded up to 270s late, and an issue filed in
    # that gap would sit under it. One REST call per 300s is cheap enough to never share.
    return {"terminal": TERMINAL, "interval_s": 300}


def _recorded(baseline: Path) -> int | None:
    try:
        return int(json.loads(baseline.read_text())["after"])
    except OSError, ValueError, KeyError, TypeError:
        return None


def read(tools: FindingsTools, after: int | None, baseline: Path, today: date) -> dict:
    """The wait's state, as cc-wait's state and detail.

    Args:
        tools: the boundaries every gh call goes through, aimed at the register.
        after: the `--after` baseline, or None to use the one recorded at ``baseline``.
        baseline: the file a wait with no `--after` records its baseline in.
        today: the date a `not-before:` label is compared to.

    Raises:
        subprocess.SubprocessError, OSError: a gh read failed; there is no verdict.
    """
    raw = newest_open_issues(tools)
    if after is None:
        after = _recorded(baseline)
    if after is None:
        after = max((i["number"] for i in raw), default=0)
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps({"after": after}))
    newer = threaded_issues([i for i in raw if i["number"] > after], tools)
    rows = (
        free_issues(newer, tools, pr_refs=rest_open_pr_refs(tools), today=today)[0]
        if newer
        else []
    )
    if not rows:
        held = f"; {len(newer)} newer withheld" if newer else ""
        return {"state": "none", "detail": f"no new free issue above #{after}{held}"}
    issues = [{k: r[k] for k in REPORTED} for r in rows]
    return {"state": "new", "detail": json.dumps({"after": after, "issues": issues})}


def cmd_new_issues(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``new-issues`` subcommand: one cc-wait reading, or the description."""
    baseline = baseline_file(tools.repo, Path.cwd())
    if args.describe:
        print(json.dumps(describe(None if args.after is not None else baseline)))
        return 0
    try:
        reading = read(tools, args.after, baseline, today_utc())
    except subprocess.CalledProcessError as exc:
        if exc.returncode != GH_AUTH_REQUIRED:
            raise
        detail = (exc.stderr or "").strip().splitlines()[-1:] or ["gh is not logged in"]
        reading = {"state": "gh-unauthenticated", "detail": detail[0]}
    print(json.dumps(reading))
    return 0
