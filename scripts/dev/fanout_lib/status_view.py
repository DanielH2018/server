"""The text view of `place status` and `place runs`: a count line, then what needs attention.

An orchestrator polls `place status` hundreds of times, and a batch that landed or was
cleaned reads the same on every poll (#7897). The default view counts those and lists the
rest. `--all` restores one line per batch.
"""

import sys as _sys
from collections.abc import Iterable
from pathlib import Path as _Path

# Reach `lib` (scripts/): a directly-invoked script gets only its own directory on sys.path.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.progress import count_line

# Counted, never listed, unless `--all` asks: nothing is left for the orchestrator to do.
SETTLED = frozenset({"landed", "cleaned"})


def one_line(text: str, limit: int = 300) -> str:
    # Collapse the whitespace before truncating: a cut that lands mid-line would put a
    # newline inside a status line that is meant to be one line per batch.
    return " ".join(text.split())[:limit]


def _without_final_text(row: dict) -> str:
    """A `done` row's line cut after the PR URL, keeping a trailing `permission_denials=N`."""
    line, url = row["line"], row["pr_url"]
    if not url or url not in line:
        return line
    head, _, tail = line.partition(url)
    denials = [w for w in tail.split()[:1] if w.startswith("permission_denials=")]
    return " ".join([head + url, *denials])


def status_lines(rows: Iterable[dict], show_all: bool) -> list[str]:
    """The count line, then the batches `place status` prints by default.

    Args:
        rows: the rows `status.collect` returned.
        show_all: print every batch's full line.

    Returns:
        The lines to print. A `done` batch shows as its PR URL without the agent's final
        text. Every other unsettled state keeps its full line, which the orchestrator
        acts on.
    """
    rows = list(rows)
    lines = [count_line(r["state"] for r in rows)]
    for r in rows:
        if show_all:
            lines.append(r["line"])
        elif r["state"] == "done":
            lines.append(_without_final_text(r))
        elif r["state"] not in SETTLED:
            lines.append(r["line"])
    return lines


def runs_lines(runs: list, show_all: bool) -> list[str]:
    """The count line, then the runs `place runs` prints by default.

    Args:
        runs: the `Manifest`s to show.
        show_all: print every run and every batch, cleaned ones included.

    Returns:
        The lines to print. A run appears with its standing batches only, and a run whose
        batches are all cleaned is counted in the last line instead.
    """

    def state(b) -> str:
        return "cleaned" if b.removed_at else "standing"

    lines = [count_line(state(b) for r in runs for b in r.batches)]
    hidden = 0
    for r in runs:
        shown = [b for b in r.batches if show_all or not b.removed_at]
        if not shown and not show_all:
            hidden += 1
            continue
        lines.append(f"{r.run_id}  orchestrator {r.orchestrator_branch}")
        for b in shown:
            when = f"cleaned {b.removed_at}" if b.removed_at else "standing"
            issues = ",".join(f"#{n}" for n in b.issues)
            lines.append(f"  {b.batch} on {b.host}: {b.branch} {issues} {when}")
    if hidden:
        lines.append(f"{hidden} run(s) with no standing batch; --all lists them")
    return lines
