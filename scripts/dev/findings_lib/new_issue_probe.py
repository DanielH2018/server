"""The `findings` source for cc-wait: whether the register gained a free issue since a baseline.

`cc-wait findings [--repo OWNER/NAME] [--after N | --milestone "<title>"]` runs this through
`.claude/wait-sources/findings`. It answers the question an orchestrator with an empty queue
asks, "has anything new been filed that I could pick up?", without re-running
`findings.py next` in a loop.

    findings.py new-issues --describe              # terminal states, as JSON
    findings.py new-issues [--repo R] [--after N] [--milestone none]  # the state now, one JSON line
    findings.py new-issues [--repo R] --milestone "<title>"           # the same, for one project

THE STATE.

    none                (open)  no free issue past the baseline
    new                 0       one or more; the detail is JSON naming each one
    gh-unauthenticated  4       gh exited 4: no login is configured, which no wait fixes
    unknown-milestone   64      `--milestone` names a title no milestone has

cc-wait refuses a source whose terminal states all exit 0, since it could never report a
failure, so these two failures end the wait.

"Free" is `claim.free_issues`, the definition `next` prints, so the wait ends exactly when
`next` would list an issue past the baseline. A new issue that is claimed, deferred,
blocked, `manual` or named by an open PR keeps the wait open.

THE NUMBER BASELINE. With no `--milestone`, or `--milestone none`, the wait ends on a free
issue numbered above the baseline. `--milestone none` counts only the issues no open
milestone holds, through `milestone.held_issues`, the filter `next --milestone` applies.
`--after N` is the baseline, and survives a resume unchanged. With no `--after`, the baseline
is the highest open issue number when the wait starts. Every `none` detail names the
baseline so a resume can pass it as `--after`.

THE PROJECT BASELINE. `--milestone "<title>"` waits on one milestone, and its baseline is the
set of free issues that milestone holds when the wait starts. The wait ends on a free issue
in the milestone outside that set: one filed above every number, an older one that
`milestone add` files in the project, or one whose claim was released. A number cannot
express that, so `--after` with a title is refused with exit 64.

WHERE A BASELINE IS KEPT. cc-wait keeps no state between reads, so a file under
`$XDG_CACHE_HOME/findings-wait/`, keyed on the register, the working directory and the
milestone, holds a baseline the wait recorded itself: `--describe`, which cc-wait runs once
per wait, deletes it, and the first read that reaches GitHub records it. A resumed wait
with no `--after` therefore re-baselines, and an issue filed or added between the two runs
is missed.

ANY OTHER FAILED READ EXITS 1, through `findings.main`'s gh-failure handler, and prints
nothing on stdout, so cc-wait retries it. It is never read as "nothing new".

ONE REST REQUEST A QUIET READ. The open issues come from one `gh api` call: the 100 newest,
or the 100 newest a project holds. An issue past the baseline costs one more call for open
PR bodies, plus one per thread it carries (comments, open blockers). A project wait's first
read threads every issue the project holds, and each later read threads every held issue
outside its baseline, claimed ones included. The interval is 300s; see
`gh_calls.REST_TIMEOUT` for why REST.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.


from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.claim import free_issues
from dev.findings_lib.gh_calls import (
    newest_open_issues,
    rest_issue,
    rest_open_pr_refs,
    threaded_issues,
)
from dev.findings_lib.issue_model import PR_REPO, Issue, today_utc
from dev.findings_lib.milestone import (
    NO_MILESTONE,
    UnknownMilestone,
    held_issues,
    titled,
)
from lib.exit_codes import FINDINGS_BAD_ARGS

TERMINAL = {"new": 0, "gh-unauthenticated": 4, "unknown-milestone": FINDINGS_BAD_ARGS}

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


def baseline_file(repo: str | None, cwd: Path, milestone: str | None) -> Path:
    """Where a wait with no `--after` keeps its baseline.

    Keyed on ``repo``'s register, ``cwd`` and ``milestone``, so two waits a session runs
    on different milestones do not share one.
    """
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    waited = f"{repo or PR_REPO}\0{cwd}\0{milestone or ''}"
    key = hashlib.sha256(waited.encode()).hexdigest()[:16]
    return cache / "findings-wait" / f"{key}.json"


def describe(baseline: Path | None) -> dict:
    """cc-wait's description, after forgetting any baseline a previous wait left at ``baseline``."""
    if baseline is not None:
        baseline.unlink(missing_ok=True)
    # DECIDED: not `remote`. cc-wait would serve another wait's cached `none` on this wait's
    # first tick, so the baseline would be recorded up to 270s late, and an issue filed in
    # that gap would sit under it. One REST call per 300s is cheap enough to never share.
    return {"terminal": TERMINAL, "interval_s": 300}


def _recorded(baseline: Path, key: str) -> object:
    try:
        return json.loads(baseline.read_text())[key]
    except OSError, ValueError, KeyError, TypeError:
        return None


def _record(baseline: Path, key: str, value: object) -> None:
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(json.dumps({key: value}))


def _free_rows(
    issues: list[Issue], tools: FindingsTools, today: date, milestone: str | None
) -> list[dict]:
    if not issues:
        return []
    pr_refs = rest_open_pr_refs(tools)
    return free_issues(
        issues, tools, pr_refs=pr_refs, today=today, milestone=milestone
    )[0]


def _new(rows: list[dict], **baseline: object) -> dict:
    issues = [{k: r[k] for k in REPORTED} for r in rows]
    return {"state": "new", "detail": json.dumps({**baseline, "issues": issues})}


def read(
    tools: FindingsTools,
    after: int | None,
    baseline: Path,
    today: date,
    milestone: str | None = None,
) -> dict:
    """The state of a wait on a number baseline, as cc-wait's state and detail.

    Args:
        tools: the boundaries every gh call goes through, aimed at the register.
        after: the `--after` baseline, or None to use the one recorded at ``baseline``.
        baseline: the file a wait with no `--after` records its baseline in.
        today: the date a Not Before date is compared to.
        milestone: None, or `NO_MILESTONE` to count only the issues no open milestone holds.

    Raises:
        subprocess.SubprocessError, OSError: a gh read failed; there is no verdict.
        KeyError: an issue or a blocker came back without its `state`; no verdict either.
    """
    raw = {int(str(data["number"])): data for data in newest_open_issues(tools)}
    if after is None:
        recorded = _recorded(baseline, "after")
        after = recorded if isinstance(recorded, int) else None
    if after is None:
        after = max(raw, default=0)
        _record(baseline, "after", after)
    # Cut before threading: each kept issue costs a read for its comments and blockers.
    kept = held_issues(
        [rest_issue(data) for n, data in raw.items() if n > after],
        milestone,
        tools.owner_name,
        gh_json=tools.gh_json,
    )
    newer = threaded_issues([raw[i.number] for i in kept], tools)
    rows = _free_rows(newer, tools, today, milestone)
    if not rows:
        held = f"; {len(newer)} newer withheld" if newer else ""
        return {"state": "none", "detail": f"no new free issue above #{after}{held}"}
    return _new(rows, after=after)


def read_project(tools: FindingsTools, title: str, baseline: Path, today: date) -> dict:
    """The state of a wait on the milestone titled ``title``, as cc-wait's state and detail.

    The first read records the milestone's free issues at ``baseline`` and reports `none`.
    Each later read reports `new` for a free issue the milestone holds outside that set.

    Raises:
        subprocess.SubprocessError, OSError: a gh read failed; there is no verdict.
        KeyError: an issue or a blocker came back without its `state`; no verdict either.
        UnknownMilestone: no milestone has the title ``title``.
    """
    number = titled(title, tools.owner_name, gh_json=tools.gh_json)["number"]
    raw = newest_open_issues(tools, milestone=number)
    recorded = _recorded(baseline, "free")
    known = frozenset(int(n) for n in recorded) if isinstance(recorded, list) else None
    # Cut before threading: each kept issue costs a read for its comments and blockers.
    outside = [d for d in raw if int(str(d["number"])) not in (known or ())]
    newer = threaded_issues(outside, tools)
    rows = _free_rows(newer, tools, today, title)
    if known is None:
        _record(baseline, "free", sorted(r["number"] for r in rows))
        return {
            "state": "none",
            "detail": f"no new free issue in `{title}`; {len(rows)} free at the start",
        }
    if not rows:
        held = f"; {len(newer)} outside the baseline withheld" if newer else ""
        return {"state": "none", "detail": f"no new free issue in `{title}`{held}"}
    return _new(rows, before=sorted(known))


def cmd_new_issues(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``new-issues`` subcommand: one cc-wait reading, or the description."""
    project = args.milestone not in (None, NO_MILESTONE)
    if project and args.after is not None:
        print(
            'new-issues: --after is a number baseline, and a --milestone "<title>" wait '
            "records the project's free issues instead; drop --after",
            file=sys.stderr,
        )
        return FINDINGS_BAD_ARGS
    baseline = baseline_file(tools.repo, Path.cwd(), args.milestone)
    if args.describe:
        print(json.dumps(describe(None if args.after is not None else baseline)))
        return 0
    try:
        if project:
            reading = read_project(tools, args.milestone, baseline, today_utc())
        else:
            reading = read(tools, args.after, baseline, today_utc(), args.milestone)
    except UnknownMilestone as exc:
        reading = {"state": "unknown-milestone", "detail": str(exc)}
    except subprocess.CalledProcessError as exc:
        if exc.returncode != GH_AUTH_REQUIRED:
            raise
        detail = (exc.stderr or "").strip().splitlines()[-1:] or ["gh is not logged in"]
        reading = {"state": "gh-unauthenticated", "detail": detail[0]}
    print(json.dumps(reading))
    return 0
