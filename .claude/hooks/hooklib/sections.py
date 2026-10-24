"""Run the SessionStart banner's probes at once, under one total deadline.

Each probe reads something slow and independent: `probe.py targets` through `uv run`, the
stale-release journal, the primary checkout's `git status`, a diff per live worktree, and
`prune_worktrees.py --brief`, which reaches GitHub. Run one after another, their individual
caps added up to more than the 15s the harness gives the hook, and a kill at that budget
discards every section still buffered (#6153). Run together, the slowest probe sets the cost.

Package name is `hooklib`, not `lib`, so it never shares a namespace-package name with
`scripts/lib` (see session-health.py's own import comment).
"""

import threading
import time
from collections.abc import Callable


def run_sections(
    probes: list[tuple[str, Callable[[], list[str]]]], deadline: float
) -> list[list[str]]:
    """Run every probe in its own thread and return their lines in the order given.

    Args:
        probes: `(name, probe)` pairs. `name` appears only in the line for a probe that did
            not finish; `probe` takes no arguments and returns banner lines.
        deadline: seconds, from the call, that the whole set may take.

    Returns:
        One list of lines per probe. A probe that raised gives [], the banner's usual
        degrade. A probe still running at the deadline gives one `⚠` line naming it rather
        than [], because an empty list reads the same as "nothing to report".
    """
    results: list[list[str] | None] = [None] * len(probes)

    def work(i, probe):
        try:
            results[i] = probe()
        except Exception:
            results[i] = []

    # Daemon threads, not a ThreadPoolExecutor: the executor joins its workers at interpreter
    # exit, so a probe hung past the deadline would hold the hook until the harness kills it
    # and the printed sections with it.
    threads = [
        threading.Thread(target=work, args=(i, probe), daemon=True)
        for i, (_name, probe) in enumerate(probes)
    ]
    for thread in threads:
        thread.start()
    end = time.monotonic() + deadline
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    out = []
    for (name, _probe), lines in zip(probes, results, strict=True):
        if lines is None:
            lines = [f"  ⚠ {name} did not finish within {deadline:g}s, skipped"]
        out.append(lines)
    return out
