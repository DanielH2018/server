"""The model and effort a fan-out batch's implementer runs at, one rung of a fixed ladder.

The orchestrator names a rung per batch with `place launch --size`, from its reading of the
work; a batch it names nothing for runs at `DEFAULT`. `size` then raises the choice to the
floor the batch's labels set, so a batch whose last fix round failed never reruns lower.

The rung reaches the unit only on its command line, fixed at launch. A rung read from the
worktree at run time would let the implementer promote itself. Every phase that resumes the
implementer session keeps its rung: a model switch drops the prompt cache, and Fable's
reasoning with it.

Typical usage example:

    rung = size({"claude", "review-leftover"}, by_name("sonnet-medium"))  # opus-high
    argv = ["claude", "-p", "--model", rung.model, "--effort", rung.effort]
"""

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Rung:
    """One model and effort pair, with the spend bound its sessions run under.

    Attributes:
        model: the `claude --model` alias.
        effort: the `claude --effort` level.
        budget_usd: the session's `--max-budget-usd`, a runaway bound, not a forecast.
    """

    model: str
    effort: str
    budget_usd: int

    @property
    def name(self) -> str:
        return f"{self.model}-{self.effort}"


# DECIDED: the operator ruled on 2026-11-11 that sizing may pick Fable and reach `xhigh`, and
# never `max`. The order follows "Which Claude model should you build with?" (claude.dev,
# 2026-11-09): raise effort one level, then move up a model at a lower effort, and past `high`
# move up a model instead. Budgets scale opus-medium's $40 (84 sessions peaked at $18) by
# price (Sonnet half of Opus, Fable twice) and effort (medium costs ~70% of high; xhigh 2.5x).
LADDER = (
    Rung("sonnet", "medium", 20),
    Rung("opus", "medium", 40),
    Rung("opus", "high", 60),
    Rung("fable", "medium", 80),
    Rung("fable", "high", 120),
    Rung("fable", "xhigh", 300),
)
DEFAULT = LADDER[1]
# A batch carrying this label reruns work whose last fix round did not finish it.
LEFTOVER_LABEL = "review-leftover"
LEFTOVER_FLOOR = LADDER[2]
# The red-test author and the reviewer, whatever the batch's rung.
REVIEWER = Rung("opus", "medium", 15)
SIZE_HELP = (
    f"run this batch's implementer at RUNG, one of {', '.join(r.name for r in LADDER)};"
    f" repeatable. A batch named by none runs at {DEFAULT.name}, and a {LEFTOVER_LABEL}"
    f" batch at no less than {LEFTOVER_FLOOR.name}"
)


def by_name(name: str) -> Rung:
    """The rung called `name`, such as `opus-high`.

    Raises:
        ValueError: no rung has that name; the message lists the names there are.
    """
    for rung in LADDER:
        if rung.name == name:
            return rung
    names = ", ".join(r.name for r in LADDER)
    raise ValueError(f"no rung {name!r}; the rungs are {names}")


def parse_sizes(specs: list[str], batches: Iterable[str]) -> dict[str, Rung] | str:
    """Map each `--size BATCH=RUNG` spec to its batch id (the issue numbers joined by `-`).

    Returns:
        The map, or the error to print when a spec names no batch in `batches` or no rung.
    """
    sizes = {}
    for spec in specs:
        numbers, _, name = spec.partition("=")
        if numbers.replace(",", "-") not in batches:
            return f"--size {spec}: {numbers!r} is no --batch of this launch"
        try:
            sizes[numbers.replace(",", "-")] = by_name(name)
        except ValueError as exc:
            return f"--size {spec}: {exc}"
    return sizes


def floor(labels: Iterable[str]) -> Rung:
    """The lowest rung a batch carrying `labels` may run at."""
    return LEFTOVER_FLOOR if LEFTOVER_LABEL in set(labels) else LADDER[0]


def size(labels: Iterable[str], chosen: Rung | None = None) -> Rung:
    """The rung a batch runs at: `chosen` (or `DEFAULT`), raised to the floor its labels set.

    Args:
        labels: every label on every issue in the batch.
        chosen: the orchestrator's rung for the batch, or None for `DEFAULT`.
    """
    return max(chosen or DEFAULT, floor(labels), key=LADDER.index)
