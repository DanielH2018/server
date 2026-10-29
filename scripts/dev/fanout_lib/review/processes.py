"""The review pipeline's process boundary, and the reap that ends a red phase.

`run_process` is the one call every `claude`, `git` and `gh` invocation goes through.

`reaping` kills whatever the red phase left running (#3852). The red author runs with
`--permission-mode auto`, so it could start
`nohup sh -c 'sleep 120; echo ... > .claude/settings.local.json' &` and have it write after
the worktree is reset. While the block is open the pipeline is a child subreaper, so an
orphaned descendant reparents to it rather than to init, `setsid` or not, and the block's exit
kills each one. The red author's tests can do the same when a later gate runs them, so the
green gate, the hunk check and the base check are reaped as well (#4508).

DECIDED: the red phase and every test run the pipeline starts are reaped, and nothing else.
The landing runs `land.sh --detach`, which double-forks so the landing outlives the call
(`land_lib/detach.py`). Under a subreaper the landing would reparent to the pipeline and be
killed. The implement and fix phases run the red tests as well, and are not reaped: the
implementer may leave a process on purpose, and #3865 covers what a session starts.

A process started outside this tree, such as through `systemd-run --user`, is not reached.
"""

import ctypes
import json
import os
import signal
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

PR_SET_CHILD_SUBREAPER = 36


def otel_attributes(batch: str, phase: str) -> str:
    """The `OTEL_RESOURCE_ATTRIBUTES=` assignment one phase's `claude -p` runs under (#5673).

    Claude Code's telemetry reaches Prometheus with each resource attribute as a label, so
    `fanout_batch` and `fanout_phase` make spend, tokens and tool failures readable per batch
    and phase. Before this, those lived only in the review records, split across both hosts.
    An attribute the unit's own environment or the user's settings already carries is kept
    ahead of them. The settings value matters because Claude Code applies a settings `env`
    over the process environment and replaces the whole string (measured 2026-10-29, #5684):
    the agent user's `process.owner=claude` would otherwise displace the batch labels. The
    caller therefore passes this same value through the phase's `--settings` as well.
    """
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        from_settings = json.loads((config / "settings.json").read_text())["env"][
            "OTEL_RESOURCE_ATTRIBUTES"
        ]
    except OSError, ValueError, KeyError, TypeError:
        from_settings = ""
    attributes: dict[str, str] = {}
    for pair in (
        *str(from_settings).split(","),
        *os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","),
        f"fanout.batch={batch}",
        f"fanout.phase={phase}",
    ):
        key, _, value = pair.partition("=")
        if key.strip():
            attributes[key.strip()] = value
    return "OTEL_RESOURCE_ATTRIBUTES=" + ",".join(
        f"{k}={v}" for k, v in attributes.items()
    )


def run_process(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, input=stdin, capture_output=True, text=True, check=False
    )


def _set_subreaper(on: bool) -> None:
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, int(on), 0, 0, 0)


def _children() -> list[int]:
    me = str(os.getpid())
    found = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            # The command name can hold spaces and parentheses; the ppid follows the last ")".
            fields = stat.read_text().rpartition(")")[2].split()
        except OSError:
            continue
        if fields and fields[1] == me:
            found.append(int(stat.parent.name))
    return found


def _reap_descendants() -> None:
    """SIGKILL and reap every child of this process, and every orphan that reparents to it."""
    for _ in range(100):
        children = _children()
        if not children:
            return
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in children:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


@contextmanager
def reaping() -> Generator[None]:
    """Kill every process started inside the block once it exits, orphans included.

    The caller must start no process inside the block that should outlive it. On exit this
    process is a subreaper no longer.
    """
    _set_subreaper(True)
    try:
        yield
    finally:
        _reap_descendants()
        _set_subreaper(False)
