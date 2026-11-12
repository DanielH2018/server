"""Progress lines for a polling loop and a listing, kept short for an agent to read.

A wait that polls every few seconds and prints its state each time fills an agent's context
with identical lines: one landing printed `waiting for hosts to apply: …` 42 times (#7892).
Route the loop's line through `OnChange` and it prints once per distinct state.

A listing that prints every item on every call has the same cost (#7897). `count_line` gives
the one-line summary such a listing opens with, before the items that need attention.

Typical usage example:

    progress = OnChange(say)
    while pending:
        progress(f"waiting on {', '.join(pending)}")

    print(count_line(["running", "done", "running"]))  # 2 running, 1 done
"""

from collections import Counter
from collections.abc import Callable, Iterable


class OnChange:
    """A progress emitter that passes a line on only when it differs from the last one.

    Args:
        emit: Where a changed line goes, such as `print` or a landing's `say`.
    """

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._last: str | None = None

    def __call__(self, line: str) -> None:
        if line != self._last:
            self._emit(line)
            self._last = line


def count_line(states: Iterable[str]) -> str:
    """The count of each state in first-seen order, such as `3 running, 2 done, 1 needs-input`.

    Returns:
        The comma-separated counts, or `none` when `states` is empty.
    """
    counts = Counter(states)
    return ", ".join(f"{n} {state}" for state, n in counts.items()) or "none"
