"""The runner the red/green gate tests hand the gates, on a scratch repo.

It hands `git` to git under a scrubbed environment and turns a gate's
`uv run --directory <tree> pytest ...` into this interpreter's pytest in that tree, so every
verdict a test reads is one pytest really printed.
"""

import subprocess
import sys

from lib.git_testing import scrubbed_env
from lib.proc_testing import DEFAULT_TIMEOUT


def run(argv, stdin):
    if argv[0] == "uv":
        tree = argv[3]
        argv = [sys.executable, "-m", "pytest", *argv[5:]]
        return subprocess.run(
            argv,
            cwd=tree,
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_TIMEOUT,
        )
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        env=scrubbed_env(),
        timeout=DEFAULT_TIMEOUT,
    )
