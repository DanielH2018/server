"""One way to run the GitHub CLI from a script, with no prompt and no notifier.

WHY. `lib.git` exists because five scripts each carried a private `_git()` that disagreed on
the two things that matter. `gh` is heading the same way: the docs-refresh and secret-rotate
crons call it inline, `land.sh` calls it inline, and `findings.py` is the first Python caller
that WRITES through it. A `gh` that prompts (`GH_PROMPT_DISABLED` unset) hangs a cron or a
background job forever, and the update notifier writes to stderr on the first call of the
day, which a caller that reads stderr as an error message then reports as one.

This is for the authenticated CLI on this machine. It does not take a token: `gh` reads
`~/.config/gh/hosts.yml` for the invoking user, which is how every cron here already works.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any


def gh(
    *args: str,
    check: bool = True,
    timeout: float | None = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``gh <args>`` and return the completed process.

    ``check=True`` raises ``CalledProcessError`` with ``gh``'s stderr attached, which is the
    message a caller wants to show ("not logged in", "HTTP 404"). Pass ``check=False`` to read
    ``returncode`` yourself.
    """
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
    return subprocess.run(
        ["gh", *args],
        env=env,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def gh_json(*args: str, **kwargs: Any) -> Any:
    """``gh(...)`` with stdout parsed as JSON; ``None`` for empty output."""
    out = gh(*args, **kwargs).stdout.strip()
    return json.loads(out) if out else None
