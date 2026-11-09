"""The SessionStart banner a launched brief carries, read from the host it will run on.

A headless agent reads no SessionStart banner, so `place launch` runs the hook on each placed
host and puts the lines a session's model would read into the brief.
"""

import json
import subprocess

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.transport import REPO, Tools

# The SessionStart hook reads `payload["source"]` from stdin, so it needs a JSON payload
# rather than `</dev/null`; --no-python-downloads/--python match the version run-hook.sh
# pins for session-health so this reading is taken by the same interpreter a real session would use.
HEALTH_CMD = (
    f'cd {REPO} && printf \'{{"source":"startup"}}\' | '
    "uv run --no-project --no-python-downloads --python 3.15.2 .claude/hooks/session-health.py"
)


def health_lines(tools: Tools, host: str) -> list[str]:
    """Return `host`'s banner lines, each prefixed `[host]`, or one line saying the read failed."""
    # A failed or timed-out read must not read as "clean" in the launched brief: it's the
    # signal the SessionStart banner exists to surface.
    try:
        proc = tools.run(host, HEALTH_CMD, 40.0, None)
    except subprocess.TimeoutExpired:
        return [
            f"[{host}] session-health read failed (timed out) — banner state unknown"
        ]
    if proc.returncode != 0:
        return [
            f"[{host}] session-health read failed (exit {proc.returncode}) — banner state unknown"
        ]
    context = agent_context(proc.stdout)
    return [f"[{host}] {ln}" for ln in context.splitlines() if ln.strip()]


def agent_context(stdout: str) -> str:
    """Return the part of session-health's stdout a session's model reads.

    The hook prints SessionStart's JSON form, whose `systemMessage` is operator-only and so
    stays out of a brief. Stdout that is not that JSON, from a host whose checkout predates
    the form, is returned whole.
    """
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return stdout
    if not isinstance(parsed, dict):
        return stdout
    return (parsed.get("hookSpecificOutput") or {}).get("additionalContext") or ""
