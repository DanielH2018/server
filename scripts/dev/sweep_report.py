#!/usr/bin/env python3
"""Report the nightly pytest sweep's result as one GitHub issue.

`.github/workflows/pytest-sweep.yml` runs every test module on master's head once a day. No
deploy gate reads that run, so a red one reaches the operator through the issue this tool
keeps: it opens the issue on the first red sweep, comments on it while the sweep stays red,
and closes it on the next green one. At most one such issue is open at a time, found by its
exact title.

The issue carries no `claude` label, so the findings register never lists it: the register
drops an issue a bot account opened (`findings_lib/gh_calls.py`).

Usage:
    python scripts/dev/sweep_report.py --repo OWNER/NAME --result failure \
        --run-url URL --modules "tests/a/test_x.py tests/b/test_y.py"

`--result` is the sweep job's result. Anything but `success` and `failure` gave no verdict,
so the tool changes nothing for it.

Exit codes: 0 the issue is in the state the result calls for; 1 a `gh` call failed.
"""

import sys as _sys


import subprocess
from collections.abc import Callable

from lib.cli_help import ArgumentParser
from lib.gh import gh, gh_json
from lib.json_types import as_object_list

TITLE = "Nightly pytest sweep is red"


def open_issue(repo: str, read: Callable = gh_json) -> int | None:
    """The number of the open sweep issue in `repo`, or None."""
    found = read(
        "issue", "list", "--repo", repo, "--state", "open",
        "--search", f'in:title "{TITLE}"', "--json", "number,title",
    )  # fmt: skip
    issues = as_object_list(found, "gh issue list")
    numbers = [
        n
        for i in issues
        if i.get("title") == TITLE and isinstance(n := i.get("number"), int)
    ]
    return min(numbers) if numbers else None


def red_body(run_url: str, modules: list[str]) -> str:
    """What a red sweep writes: the run, and each module pytest reported failed."""
    named = "\n".join(f"- `{m}`" for m in modules) or (
        "- no module named: the run failed outside a test, so read its log"
    )
    return (
        f"The nightly sweep failed on master: {run_url}\n\n{named}\n\n"
        "A push to master runs only the modules its diff selects, so nothing else reports "
        "these. `docs/ci-throughput.md` has what the sweep covers."
    )


def report(
    repo: str,
    result: str,
    run_url: str,
    modules: list[str],
    *,
    run: Callable = gh,
    read: Callable = gh_json,
) -> str:
    """Bring the sweep issue in `repo` to the state `result` calls for; return what was done."""
    if result not in ("success", "failure"):
        return f"sweep result {result}: no verdict, so the issue is unchanged"
    number = open_issue(repo, read)
    if result == "success":
        if number is None:
            return "sweep green, no issue open"
        run(
            "issue", "close", str(number), "--repo", repo,
            "--comment", f"The sweep is green again: {run_url}",
        )  # fmt: skip
        return f"sweep green, closed #{number}"
    body = red_body(run_url, modules)
    if number is None:
        run("issue", "create", "--repo", repo, "--title", TITLE, "--body", body)
        return f"sweep red on {len(modules)} module(s), opened an issue"
    run("issue", "comment", str(number), "--repo", repo, "--body", body)
    return f"sweep red on {len(modules)} module(s), commented on #{number}"


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--modules", default="", help="space-separated module paths")
    args = parser.parse_args(argv)
    try:
        print(report(args.repo, args.result, args.run_url, args.modules.split()))
    except subprocess.CalledProcessError as exc:
        print(f"gh failed: {(exc.stderr or '').strip()[:300]}", file=_sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
