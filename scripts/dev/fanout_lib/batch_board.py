"""`fanout.py board`: each standing batch read from its host alone, for deploy-ui.

The deploy UI's change view polls this, so it makes one read per host and no forge call. It
reuses `status.parse_status` for each batch's state, and adds what a page needs that `status`
does not read: the pipeline phase in `.fanout/phase`, when that phase began, and the `next`
field of the batch's newest `LAND-RESULT:` line. It prints one JSON list, a `BoardRow` per
batch of this repo with no `removed_at`, across every run manifest on this host.
"""

import argparse
import dataclasses
import json
from pathlib import Path
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from deploy_tools.land_lib.detach import result_fields
from dev.fanout_lib import manifest as manifest_mod
from dev.fanout_lib.manifest import Batch
from dev.fanout_lib.status import (
    STATUS_TIMEOUT_S,
    UNREAD,
    BatchStatus,
    parse_status,
    status_command,
)
from dev.fanout_lib.target import SERVER
from dev.fanout_lib.transport import Tools
from lib.cli_help import ArgumentParser

MARKER = "--- board"


@dataclass(frozen=True)
class BoardRow:
    """One batch as the board prints it.

    Attributes:
        state: `status.parse_status`'s state, or `unread` when its host did not answer.
        running: whether its unit is active; None when its host did not answer.
        phase: the pipeline phase `.fanout/phase` names, or "" without one.
        launched_at, phase_since, ended_at: epoch seconds, or None when unknown.
        pr_url: the first PR url its final text names, unreconciled.
        next: the `next` field of its newest `LAND-RESULT:` line, or "".
    """

    batch: str
    host: str
    branch: str
    issues: list[int]
    launched_at: float | None
    state: str
    running: bool | None
    phase: str
    phase_since: float | None
    ended_at: float | None
    pr_url: str
    next: str


def board_command(batches: Sequence[Batch]) -> str:
    """Each batch's phase, when it began and its newest `LAND-RESULT:` line, then `status_command`.

    The board blocks come before the first `=== ` header, which `parse_status` never reads.
    """
    blocks = []
    for b in batches:
        phase, logs = f"{b.worktree}/.fanout/phase", f"{b.worktree}/.fanout/land*.log"
        blocks.append(
            f"echo '{MARKER} {b.batch}'; "
            f'echo "phase $(cat {phase} 2>/dev/null)"; '
            f'echo "phase_since $(stat -c %Y {phase} 2>/dev/null)"; '
            f"echo \"result $(grep -h '^LAND-RESULT:' {logs} 2>/dev/null | tail -n 1)\""
        )
    return "; ".join([*blocks, status_command(batches)])


def _facts(stdout: str) -> dict[str, dict[str, str]]:
    """`{batch id: {"phase", "phase_since", "result"}}` from the blocks `board_command` prints."""
    head = re.split(r"(?m)^=== ", stdout, maxsplit=1)[0]
    facts: dict[str, dict[str, str]] = {}
    for block in head.split(f"{MARKER} ")[1:]:
        name, _, body = block.partition("\n")
        pairs = (ln.partition(" ") for ln in body.splitlines())
        facts[name.strip()] = {k: v.strip() for k, _, v in pairs if k}
    return facts


def board(batches: Sequence[Batch], tools: Tools) -> list[BoardRow]:
    """One row per batch, read with one call per host.

    A host that times out, or whose reply carries no board block, gives each of its batches
    `unread`.
    """
    rows = []
    for host in sorted({b.host for b in batches}):
        mine = [b for b in batches if b.host == host]
        try:
            stdout = tools.run(host, board_command(mine), STATUS_TIMEOUT_S, None).stdout
        except subprocess.TimeoutExpired:
            stdout = ""
        facts = _facts(stdout)
        if not facts:
            rows.extend(_row(b, None, {}) for b in mine)
            continue
        parsed = parse_status(mine, stdout)
        rows.extend(
            _row(b, s, facts.get(b.batch, {}))
            for b, s in zip(mine, parsed, strict=True)
        )
    return rows


def _row(b: Batch, s: BatchStatus | None, facts: dict[str, str]) -> BoardRow:
    since = facts.get("phase_since", "")
    result = result_fields(facts.get("result", "")) or {}
    try:
        launched = datetime.fromisoformat(b.launched_at).timestamp()
    except ValueError:
        launched = None
    return BoardRow(
        batch=b.batch,
        host=b.host,
        branch=b.branch,
        issues=b.issues,
        launched_at=launched,
        state=s.state if s else UNREAD,
        running=None if s is None else s.state == "running",
        phase=facts.get("phase", ""),
        phase_since=float(since) if since.isdigit() else None,
        ended_at=s.ended_at if s else None,
        pr_url=s.pr_url if s else "",
        next=result.get("next", ""),
    )


def main(argv: list[str] | None = None, tools: Tools | None = None) -> int:
    parser = ArgumentParser(
        prog="fanout.py board", description=(__doc__ or "").partition("\n")[0]
    )
    parser.add_argument(
        "--manifest-root",
        type=Path,
        default=manifest_mod.MANIFEST_DIR,
        help=argparse.SUPPRESS,
    )
    ns = parser.parse_args(argv)
    standing = manifest_mod.standing_batches(SERVER, ns.manifest_root)
    rows = board([b for _, b in standing], tools or Tools())
    print(json.dumps([dataclasses.asdict(r) for r in rows]))
    return 0
