"""The gh reads and writes `findings.py` makes, and the one place a plan is executed.

Everything here takes a `FindingsTools`, so a test answers gh from a fake rather than from
the network. `load_issues` defaults its own, which is how `scripts/docs/reference/backlog.py`
calls it with nothing to inject.

`run` is the whole write surface: it prints a plan under `--dry-run` and calls `tools.gh`
otherwise, so no command has to remember which mode it is in.
"""

import subprocess
import sys

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from dev.findings_model import _LIST_FIELDS, PROJECT_TITLE
from dev.findings_plans import is_project_failure, without_project
from dev.findings_tools import FindingsTools


def load_issues(state: str = "all", tools: FindingsTools | None = None) -> list[dict]:
    """Fetches every ``claude``-labeled issue from gh, up to 1000.

    Args:
        state: issue state to filter by (``open``, ``closed`` or ``all``).
        tools: the boundaries to reach gh through; the real ones when omitted, which is how
            `scripts/docs/reference/backlog.py` calls it.
    """
    argv = ("issue", "list", "--label", "claude", "--state", state, "--limit", "1000")
    return (tools or FindingsTools()).gh_json(*argv, "--json", _LIST_FIELDS) or []


def _existing_labels(tools: FindingsTools) -> set[str]:
    labels = tools.gh_json("label", "list", "--limit", "200", "--json", "name")
    return {lab["name"] for lab in labels or []}


def _load_issue(number: int, tools: FindingsTools) -> dict:
    return tools.gh_json("issue", "view", str(number), "--json", _LIST_FIELDS)


def run(plans: list[list[str]], dry_run: bool, tools: FindingsTools) -> None:
    for argv in plans:
        if dry_run:
            print("gh " + " ".join(argv))
        else:
            tools.gh(*argv)


def _create_with_optional_project(argv: list[str], tools: FindingsTools) -> str:
    """Run the create argv, retrying without ``--project`` if the board is the only problem.

    Returns the created issue's URL. The board is a view; losing it must not lose the
    finding, so a Project failure warns and the issue is created anyway.
    """
    try:
        return tools.gh(*argv).stdout.strip()
    except subprocess.CalledProcessError as exc:
        if not is_project_failure(exc.stderr):
            raise
        first_line = (exc.stderr or "").strip().partition("\n")[0]
        url = tools.gh(*without_project(argv)).stdout.strip()
        sys.stderr.write(
            f'warning: not added to Project "{PROJECT_TITLE}": {first_line}\n'
        )
        return url
