"""The read of a batch's systemd unit state, which rides a connection another read opened.

`place read` makes one ssh call per host for headroom, and the ssh budget (`ufw limit ssh`
allows 5 per 30s: two reads plus `MAX_BATCHES_PER_REMOTE_HOST` launches) leaves no room for
a call of its own. `transport.read_host` therefore appends this read to the headroom command
when a host holds uncleaned batches, behind `MARKER` so the headroom parse never sees it.

This module holds only the command and the marker. Interpreting the reply stays with
`status.parse_status`, which is what `place status` uses, so the two cannot disagree about
whether a unit has stopped.
"""

from collections.abc import Sequence

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.manifest import Batch

# A line of its own between the headroom reply and the unit blocks. It cannot start with
# `=== `, which opens each batch's block, and no signing-key line begins with `--- `.
MARKER = "--- unit states"


def unit_props_command(b: Batch) -> str:
    """The block for one batch: its id header, then the three unit properties."""
    return (
        f"echo '=== {b.batch}'; "
        f"systemctl --user show {b.unit} -p ActiveState -p Result -p ExecMainStatus"
    )


def appended_command(batches: Sequence[Batch]) -> str:
    """What follows the headroom command to read `batches`' units, or "" for none.

    Ends in `true` because `read_host` accepts only exit status 0 or 1 from the whole command,
    and a failed `systemctl` must not turn a good headroom reading into an unreadable host.
    """
    if not batches:
        return ""
    reads = "; ".join(unit_props_command(b) for b in batches)
    return f"; echo '{MARKER}'; {reads}; true"


def split_reply(stdout: str) -> tuple[str, str]:
    """`stdout` as (headroom reply, unit blocks); the blocks are empty when no marker came."""
    head, _, units = stdout.partition(f"\n{MARKER}\n")
    return head, units
