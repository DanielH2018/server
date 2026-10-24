"""The path root, the logger and the command runners every reconcile module shares."""

import os
import subprocess
import sys
from pathlib import Path

# HOMELAB_ROOT prefixes every path reconcile reads or writes. Only the tests set it.
ROOT = Path(os.environ.get("HOMELAB_ROOT", "/"))


def say(message: str) -> None:
    print(f"homelab-reconcile: {message}", file=sys.stderr)


def under_root(path: Path) -> Path:
    return ROOT / path.relative_to("/")


def run(*argv: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True)
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return run("systemctl", *args)
