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


@dataclass(frozen=True)
class BatchStatus:
    batch: str
    state: str  # running | done | failed
    pr_url: str
    final_text: str
    exit_code: int | None
    stderr_tail: str


def _one(b: Batch) -> str:
    return (
        f"echo '=== {b.batch}'; "
        f"systemctl --user show {b.unit} -p ActiveState -p Result -p ExecMainStatus; "
        f"echo '--- stderr'; tail -c 2000 {b.worktree}/.fanout/stderr.log 2>/dev/null; "
        f"echo; echo '--- report'; cat {b.worktree}/.fanout/report.json 2>/dev/null"
    )


def status_command(batches: Sequence[Batch]) -> str:
    return "; ".join(_one(b) for b in batches)


def stop_command(batch: str) -> str:
    return f"systemctl --user stop fanout-{batch}"


def _section(block: str, name: str) -> str:
    marker = f"--- {name}\n"
    if marker not in block:
        return ""
    rest = block.split(marker, 1)[1]
    return rest.split("\n--- ", 1)[0]


def _final_text(report: str) -> str:
    for line in reversed(report.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            return str(data.get("result", ""))
    return ""


def parse_status(batches: Sequence[Batch], stdout: str) -> list[BatchStatus]:
    blocks = {}
    for chunk in stdout.split("=== ")[1:]:
        name, _, body = chunk.partition("\n")
        blocks[name.strip()] = body
    out = []
    for b in batches:
        body = blocks.get(b.batch, "")
        props = dict(
            ln.split("=", 1)
            for ln in body.split("--- ", 1)[0].splitlines()
            if "=" in ln
        )
        stderr_tail = _section(body, "stderr").strip()
        final = _final_text(_section(body, "report"))
        code_text = props.get("ExecMainStatus", "")
        code = int(code_text) if code_text.isdigit() else None
        if props.get("ActiveState") in ("active", "activating"):
            state = "running"
        elif props.get("Result") == "success" and final:
            state = "done"
        else:
            state = (
                "failed"  # a unit that vanished, or exited non-zero, or left no report
            )
        m = PR_URL.search(final)
        out.append(
            BatchStatus(
                b.batch, state, m.group(0) if m else "", final, code, stderr_tail
            )
        )
    return out
