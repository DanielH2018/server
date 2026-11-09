"""Progress lines for a polling loop that print only when they change.

A wait that polls every few seconds and prints its state each time fills an agent's context
with identical lines: one landing printed `waiting for hosts to apply: …` 42 times (#7892).
Route the loop's line through `OnChange` and it prints once per distinct state.

Typical usage example:

    progress = OnChange(say)
    while pending:
        progress(f"waiting on {', '.join(pending)}")
"""

from collections.abc import Callable


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
