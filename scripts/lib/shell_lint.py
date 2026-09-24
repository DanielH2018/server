#!/usr/bin/env python3
"""Render a Jinja-templated shell script, then lint the output with `bash -n` and shellcheck.

The render half is `lib.ansible_jinja_env`, the environment every render guard shares;
`render_template` is re-exported here because this module is where the shell guard's callers
already reach for it. The lint half wraps the two external linters and returns error strings
rather than raising, so a caller can report every failing template in one pass.

`scripts/validate/shell_templates.py` is the entry point that sweeps the tree with these.
"""

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# A directly-invoked script gets only its own directory on sys.path, and pyproject's
# `pythonpath` is a pytest setting — so the cross-directory imports below need the
# scripts/ root here, the same way its siblings reach it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.ansible_jinja_env import render_template

__all__ = [
    "bash_syntax_check",
    "find_shellcheck",
    "render_template",
    "shellcheck_batch",
    "shellcheck_check",
]


def find_shellcheck(which: Callable[[str], str | None] = shutil.which) -> str | None:
    """Resolve the shellcheck binary, or None when it is not on PATH.

    `which` is a parameter so a caller can prove the fail-closed branch without patching
    `shutil` on some module — the seam the repo's monkeypatch ratchet exists to remove.
    """
    return which("shellcheck")


def bash_syntax_check(path: Path) -> str | None:
    """`bash -n` parses (never executes) the rendered script. Return an error string, or None."""
    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        return proc.stderr.strip() or f"bash -n exited {proc.returncode}"
    return None


def shellcheck_check(path: Path, shellcheck_bin: str) -> str | None:
    """Run shellcheck against the rendered script at `path`; return an error string, or None.

    All severities — the repo default, no --severity override, matching the prek shellcheck
    hook.
    """
    proc = subprocess.run([shellcheck_bin, str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        return (
            proc.stdout.strip()
            or proc.stderr.strip()
            or f"shellcheck exited {proc.returncode}"
        )
    return None


def shellcheck_batch(paths: list[Path], shellcheck_bin: str) -> dict[Path, str]:
    """Run shellcheck ONCE over every rendered script; {path: findings} for the ones it flags.

    One process rather than one per file: shellcheck's start-up is ~0.26s on daniel-box, so
    the 20-template sweep spent ~5s launching it and well under a second checking anything
    (measured 2026-09-01). Same severities as `shellcheck_check` — the repo default, matching
    the prek hook. `-f gcc` prints one `path:line:col: level: message [SCnnnn]` per finding, so
    a batch verdict attributes cleanly to the file it belongs to; the default format groups by
    `In <path> line N:` blocks, which would need parsing.
    """
    if not paths:
        return {}
    proc = subprocess.run(
        [shellcheck_bin, "-f", "gcc", *map(str, paths)], capture_output=True, text=True
    )
    if proc.returncode == 0:
        return {}
    by_path: dict[Path, list[str]] = {}
    for line in proc.stdout.splitlines():
        for path in paths:
            if line.startswith(f"{path}:"):
                by_path.setdefault(path, []).append(line[len(str(path)) + 1 :])
                break
    if not by_path:
        # Non-zero with nothing attributable (a bad flag, a crash): blame every file rather
        # than none, so a broken shellcheck cannot read as a clean sweep.
        msg = proc.stderr.strip() or f"shellcheck exited {proc.returncode}"
        return {p: msg for p in paths}
    return {p: "\n".join(lines) for p, lines in by_path.items()}
