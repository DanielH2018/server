"""Read every batch on one host in one call, and stop one — spec 2026-09-06 §4-5."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.manifest import Batch

STATUS_TIMEOUT_S = 30.0
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
RESULT_TYPE = "result"


@dataclass(frozen=True)
class BatchStatus:
    """One batch's verdict, read from its unit's properties and its result JSON.

    Attributes:
        batch: the batch id.
        state: running, done, or failed.
        pr_url: the PR the final text names, or empty.
        final_text: the session's own `result` string, or empty.
        exit_code: the unit's ExecMainStatus, or None when it could not be read.
        stderr_tail: the last of the unit's stderr log.
        is_error: the session's own `is_error` flag — true means the agent failed even
            though the unit may have exited 0.
        permission_denials: how many tool calls the session was denied.
        terminal_reason: why the session ended, when it says.
    """

    batch: str
    state: str  # running | done | failed
    pr_url: str
    final_text: str
    exit_code: int | None
    stderr_tail: str
    is_error: bool = False
    permission_denials: int = 0
    terminal_reason: str = ""


def _one(b: Batch) -> str:
    return (
        f"echo '=== {b.batch}'; "
        f"systemctl --user show {b.unit} -p ActiveState -p Result -p ExecMainStatus; "
        f"echo '--- stderr'; tail -c 2000 {b.worktree}/.fanout/stderr.log 2>/dev/null; "
        f"echo; echo '--- report'; cat {b.worktree}/.fanout/report.json 2>/dev/null"
    )


def status_command(batches: Sequence[Batch]) -> str:
    return "; ".join(_one(b) for b in batches)


def stop_command(unit: str) -> str:
    return f"systemctl --user stop {unit}"


_SECTION_NAMES = ("stderr", "report")


def _section(block: str, name: str) -> str:
    # Bound the section only at a KNOWN marker ("--- stderr" / "--- report"), never at
    # any line starting "--- " — the section's own content (a stderr line, report prose)
    # can start that way without ending the section early.
    marker = f"--- {name}\n"
    if marker not in block:
        return ""
    rest = block.split(marker, 1)[1]
    ends = [
        idx
        for other in _SECTION_NAMES
        if other != name
        for idx in [rest.find(f"\n--- {other}\n")]
        if idx != -1
    ]
    return rest[: min(ends)] if ends else rest


def _result(report: str) -> dict:
    """Return the last `{"type": "result"}` object on the report stream, or {}.

    The fields read here are the ones renovate_agent's `agent_logic._outcome` reads from the
    same `claude -p --output-format json` stream: `result`, `is_error`, `terminal_reason`
    and `permission_denials`.
    """
    for line in reversed(report.strip().splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            data = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == RESULT_TYPE:
            return data
    return {}


def parse_status(batches: Sequence[Batch], stdout: str) -> list[BatchStatus]:
    known = {b.batch for b in batches}
    blocks: dict[str, str] = {}
    order: list[str] = []
    # Split only at a genuine block header (start of line). A block's own content — a
    # report's result prose, a stderr line — can still start with "=== "; when the header
    # name that produces isn't one of ours, it's not a new block, so fold it back into the
    # block already open.
    for chunk in re.split(r"(?m)^=== ", stdout)[1:]:
        name, _, body = chunk.partition("\n")
        name = name.strip()
        if name in known:
            blocks[name] = body
            order.append(name)
        elif order:
            blocks[order[-1]] += "=== " + chunk
    out = []
    for b in batches:
        body = blocks.get(b.batch, "")
        # Anchor the props/sections boundary at the start of a line, like _section does: a
        # property VALUE carrying "--- " mid-line (e.g. Description=fanout --- stderr sink)
        # would otherwise truncate the props block at that "--- ", not at the real marker.
        props = dict(
            ln.split("=", 1)
            for ln in re.split(r"(?m)^--- ", body, maxsplit=1)[0].splitlines()
            if "=" in ln
        )
        stderr_tail = _section(body, "stderr").strip()
        result = _result(_section(body, "report"))
        final = str(result.get("result") or "")
        # A session that failed still exits 0 through systemd — the failure is in the result
        # object, not the unit, which is why `Result=success` alone cannot mean done.
        is_error = bool(result.get("is_error"))
        code_text = props.get("ExecMainStatus", "")
        code = int(code_text) if code_text.isdigit() else None
        if props.get("ActiveState") in ("active", "activating"):
            state = "running"
        elif props.get("Result") == "success" and final and not is_error:
            state = "done"
        else:
            state = "failed"  # a unit that vanished, exited non-zero, errored, or left no report
        m = PR_URL.search(final)
        out.append(
            BatchStatus(
                b.batch,
                state,
                m.group(0) if m else "",
                final,
                code,
                stderr_tail,
                is_error=is_error,
                permission_denials=len(result.get("permission_denials") or []),
                terminal_reason=str(result.get("terminal_reason") or ""),
            )
        )
    return out
