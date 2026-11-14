"""One way to run the GitHub CLI from a script, with no prompt and no notifier.

WHY. `lib.git` exists because five scripts each carried a private `_git()` that disagreed on
the two things that matter. `gh` is heading the same way: the docs-refresh and eval-run
crons call it inline, `land.sh` calls it inline, and `findings.py` is the first Python caller
that WRITES through it. A `gh` that prompts (`GH_PROMPT_DISABLED` unset) hangs a cron or a
background job forever, and the update notifier writes to stderr on the first call of the
day, which a caller that reads stderr as an error message then reports as one.

This is for the authenticated CLI on this machine. It does not take a token: `gh` reads
`~/.config/gh/hosts.yml` for the invoking user, which is how every cron here already works.

Every call is counted against the session that made it, in the ledger `gh-quota status` reads
(docs/github-quota.md). The count costs nothing on a host that does not record.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
_sys.path.insert(
    0, str(_Path(__file__).resolve().parents[2] / "hosts/components/gh-quota/files")
)

import json
import os
import subprocess
from pathlib import Path

import gh_quota
from lib.json_types import JsonValue


def gh(
    *args: str,
    check: bool = True,
    timeout: float | None = 60.0,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``gh <args>`` and return the completed process.

    ``check=True`` raises ``CalledProcessError`` with ``gh``'s stderr attached, which is the
    message a caller wants to show ("not logged in", "HTTP 404"). Pass ``check=False`` to read
    ``returncode`` yourself. ``cwd`` is for a call that resolves the repository from the
    working tree (``gh pr list --head`` with no ``--repo``); most calls need none.
    """
    # DECIDED: the budget lives in the wrappers that already run gh, not in a shim ahead of gh
    # on PATH: GitHub has one shim, ghread, and a second is a second thing to understand.
    # (ADR-0034)
    gh_quota.record(args)
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
    return subprocess.run(
        ["gh", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def gh_json(
    *args: str,
    check: bool = True,
    timeout: float | None = 60.0,
    cwd: str | Path | None = None,
) -> JsonValue:
    """``gh(...)`` with stdout parsed as JSON; ``None`` for empty output.

    The top level is an array for ``issue list`` and an object for ``pr view``, so the caller
    narrows it (``lib.json_types.as_list`` / ``as_object``) before reading.
    """
    out = gh(*args, check=check, timeout=timeout, cwd=cwd).stdout.strip()
    return json.loads(out) if out else None
