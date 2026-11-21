#!/usr/bin/env python3
"""Report a scheduled workflow's result on master as one GitHub issue per workflow.

No deploy gate reads the scheduled runs of `.github/workflows/pytest-sweep.yml` or
`.github/workflows/prune-artifacts.yml`, so a red one reaches the operator through the issue
this tool keeps: it opens the issue on the first red run, comments on it while the workflow
stays red, and closes it on the next green run. At most one such issue per workflow is open
at a time, found by its exact title in `REPORTS`.

The issue carries no `claude` label, so the findings register never lists it: the register
drops an issue a bot account opened (`findings_lib/gh_calls.py`).

Usage:
    python scripts/dev/scheduled_report.py --workflow pytest-sweep --repo OWNER/NAME \
        --result failure --run-url URL --modules "tests/a/test_x.py tests/b/test_y.py"

`--workflow` is the workflow's `name:`. `--result` is the reported job's result. Anything but
`success` and `failure` gave no verdict, so the tool changes nothing for it.

Exit codes: 0 the issue is in the state the result calls for; 1 a `gh` call failed.
"""

import sys as _sys


import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from lib.cli_help import ArgumentParser
from lib.gh import gh, gh_json
from lib.json_types import as_object_list


@dataclass(frozen=True)
class Report:
    """One workflow's issue: the exact title it is found by, and what a red run adds."""

    title: str
    note: str


REPORTS = {
    "pytest-sweep": Report(
        title="Nightly pytest sweep is red",
        note=(
            "A push to master runs only the modules its diff selects, so nothing else "
            "reports these. `docs/ci-throughput.md` has what the sweep covers."
        ),
    ),
    "prune-artifacts": Report(
        title="Scheduled prune-artifacts run is red",
        note=(
            "Old manifest artifact versions and their signatures stay in GHCR until a run "
            "is green. `docs/flux-deploy-plane.md` has what the prune deletes."
        ),
    ),
}


def open_issue(repo: str, title: str, read: Callable = gh_json) -> int | None:
    """The number of the open issue titled exactly `title` in `repo`, or None."""
    found = read(
        "issue", "list", "--repo", repo, "--state", "open",
        "--search", f'in:title "{title}"', "--json", "number,title",
    )  # fmt: skip
    issues = as_object_list(found, "gh issue list")
    numbers = [
        n
        for i in issues
        if i.get("title") == title and isinstance(n := i.get("number"), int)
    ]
    return min(numbers) if numbers else None


def red_body(workflow: str, run_url: str, modules: list[str]) -> str:
    """What a red run writes: the run, each module pytest reported failed, and the note."""
    named = "\n".join(f"- `{m}`" for m in modules) or (
        "No test module is named, so read the run's log for the step that failed."
    )
    return f"`{workflow}` failed on master: {run_url}\n\n{named}\n\n{REPORTS[workflow].note}"


def report(
    repo: str,
    workflow: str,
    result: str,
    run_url: str,
    modules: list[str],
    *,
    run: Callable = gh,
    read: Callable = gh_json,
) -> str:
    """Bring `workflow`'s issue in `repo` to the state `result` calls for; return what was done."""
    if result not in ("success", "failure"):
        return f"{workflow} result {result}: no verdict, so the issue is unchanged"
    title = REPORTS[workflow].title
    number = open_issue(repo, title, read)
    if result == "success":
        if number is None:
            return f"{workflow} green, no issue open"
        run(
            "issue", "close", str(number), "--repo", repo,
            "--comment", f"`{workflow}` is green again: {run_url}",
        )  # fmt: skip
        return f"{workflow} green, closed #{number}"
    body = red_body(workflow, run_url, modules)
    if number is None:
        run("issue", "create", "--repo", repo, "--title", title, "--body", body)
        return f"{workflow} red, opened an issue"
    run("issue", "comment", str(number), "--repo", repo, "--body", body)
    return f"{workflow} red, commented on #{number}"


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workflow", required=True, choices=sorted(REPORTS))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--modules", default="", help="space-separated module paths")
    args = parser.parse_args(argv)
    try:
        print(
            report(
                args.repo,
                args.workflow,
                args.result,
                args.run_url,
                args.modules.split(),
            )
        )
    except subprocess.CalledProcessError as exc:
        print(f"gh failed: {(exc.stderr or '').strip()[:300]}", file=_sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
