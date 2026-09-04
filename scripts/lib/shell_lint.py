#!/usr/bin/env python3
"""Render a Jinja-templated shell script, then lint the output with `bash -n` and shellcheck.

The render half builds a vanilla Jinja2 environment carrying Ansible's `search` test and
`bool` filter (`scripts/lib/ansible_jinja_compat.py`), because a template written for Ansible
reaches for both. The lint half wraps the two external linters and returns error strings
rather than raising, so a caller can report every failing template in one pass.

`scripts/validate/shell_templates.py` is the entry point that sweeps the tree with these.
"""

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from jinja2 import Environment

# A directly-invoked script gets only its own directory on sys.path, and pyproject's
# `pythonpath` is a pytest setting — so the cross-directory imports below need the
# scripts/ root here, the same way its siblings reach it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.ansible_jinja_compat import ansible_bool, ansible_search
from lib.render_guard import SHARED_TPL, make_env, render_or_error


def find_shellcheck(which: Callable[[str], str | None] = shutil.which) -> str | None:
    """Resolve the shellcheck binary, or None when it is not on PATH.

    `which` is a parameter so a caller can prove the fail-closed branch without patching
    `shutil` on some module — the seam the repo's monkeypatch ratchet exists to remove.
    """
    return which("shellcheck")


def build_env(template_dir: Path) -> Environment:
    env = make_env([template_dir, SHARED_TPL])
    env.tests["search"] = ansible_search
    env.filters["bool"] = ansible_bool
    return env


def render_template(path: Path, ctx: dict) -> str:
    """Render one template and return the text, RAISING RuntimeError if it will not render.

    The raising form is for a caller checking one template. A sweep over the tree wants every
    failure rather than the first, so it uses `render_or_error` directly and reports the string.
    """
    env = build_env(path.parent)
    rendered, err = render_or_error(env, path.name, ctx)
    if rendered is None:
        raise RuntimeError(err)
    return rendered


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
