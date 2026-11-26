"""The ablation budget: the $10 cap, the runner's pre-launch stop, and pricing a plan.

ablate.py checks Budget before every engine call. Before a run starts it also plays the
whole plan through a Budget at prices taken from past engine reports (#4308), so a plan
whose two fixed arms, and first section arm if it has sections, cannot all finish is
refused before it spends.
"""

import json
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

# Each ablation run stays under $10.
CAP_USD = 10.0
PROJECTION_MARGIN = 1.5
# The most one engine call can bill: the agent's and the judge's --max-budget-usd ($0.75 and
# $0.50, in invoke-agent.mjs and judge.mjs), each tried up to three times. A run launches only
# when this much still fits under the cap, so no run can carry the total past it.
WORST_RUN_USD = 3 * 0.75 + 3 * 0.50
# What one run of a case no past report has priced is assumed to cost. The weekly sweep bills
# about $7 for its 90 runs, about $0.078 a run, averaged over cases that are not this
# one; this is that times PROJECTION_MARGIN.
UNPRICED_RUN_USD = 0.12
# Where past engine reports live: every ablation run keeps each run's report under
# <stamp>/runs/, and eval-run.sh.j2 archives each sweep's per-agent reports under SWEEP_ARCHIVE.
ABLATION_DIR = Path.home() / ".cache" / "homelab-evals" / "ablation"
SWEEP_ARCHIVE = Path("/var/lib/homelab/eval-run.d/sweeps")


class Budget:
    """Spend so far against the cap, and the projection the pre-launch check uses."""

    def __init__(
        self,
        cap: float,
        margin: float = PROJECTION_MARGIN,
        floor: float = WORST_RUN_USD,
    ):
        self.cap = cap
        self.margin = margin
        self.floor = floor
        self.spent = 0.0
        self.costliest = 0.0

    def projected(self) -> float:
        return max(self.costliest * self.margin, self.floor)

    def next_run_fits(self) -> bool:
        return self.spent + self.projected() <= self.cap

    def add(self, cost: float) -> None:
        self.spent += cost
        self.costliest = max(self.costliest, cost)

    def crossed(self) -> bool:
        return self.spent > self.cap


def report_entries(dirs: list[Path]) -> list[dict]:
    """Every engine --json case entry under `dirs`; files of any other shape are skipped."""
    entries = []
    for d in dirs:
        for path in sorted(d.rglob("*.json")) if d.is_dir() else []:
            try:
                data = json.loads(path.read_text())
            except OSError, json.JSONDecodeError:
                continue
            if isinstance(data, list):
                entries += [e for e in data if isinstance(e, dict) and "id" in e]
    return entries


def run_costs(entries: list[dict]) -> dict[str, float]:
    """The costliest single run seen per case id.

    An entry's costUsd covers all k of its runs, unhealthy ones included (the engine's
    report.mjs), so one run cost costUsd / k. The costliest rather than the mean, because an
    estimate that is low half the time accepts a plan that stops inside its baseline half the
    time.
    """
    costs: dict[str, float] = {}
    for e in entries:
        cost, k = e.get("costUsd"), e.get("k")
        if not isinstance(cost, (int, float)) or not isinstance(k, int) or k < 1:
            continue
        costs[e["id"]] = max(costs.get(e["id"], 0.0), cost / k)
    return costs


def complete_arms(per_run: list[float], k: int, n_arms: int, cap: float) -> int:
    """How many arms finish before the runner's own budget stop, at these per-run costs.

    `per_run` holds one estimated cost per case, in run order. The plan is played through
    the same Budget the runner uses, so the estimate and the live stop cannot disagree on
    where the cap falls.
    """
    budget = Budget(cap)
    for arm in range(n_arms):
        for cost in per_run:
            for _ in range(k):
                if not budget.next_run_fits():
                    return arm
                budget.add(cost)
                if budget.crossed():
                    return arm
    return n_arms


FIXED_ARMS = 2


def min_arms(sections: list[str]) -> int:
    """The arms a plan must finish to measure anything: both fixed arms and one section."""
    return FIXED_ARMS + min(1, len(sections))


def fitting_selection(
    case_ids: list[str],
    per_run: dict[str, float],
    k: int,
    sections: list[str],
    cap: float,
) -> tuple[list[str], list[str]]:
    """The largest selection whose two fixed arms, and its first section arm, all finish.

    Every plan opens with two fixed arms: baseline and -doc, or baseline and -skill. Cases
    are taken cheapest first while those, plus the first section arm when `sections` is not
    empty, still fit; then sections in the given order while their arms still fit. k stays
    fixed, because the case thresholds assume it. Both lists are empty when not even one
    case fits. The cases come back in `case_ids` order, the order the runner plays them in.
    """
    needed = min_arms(sections)

    def in_run_order(chosen: set[str]) -> list[float]:
        # Whether an arm's last run launches depends on what the runs before it spent, so
        # a selection played cheapest first can fit where the runner's own order does not.
        return [per_run[c] for c in case_ids if c in chosen]

    picked: set[str] = set()
    for cid in sorted(case_ids, key=lambda c: (per_run[c], c)):
        if complete_arms(in_run_order(picked | {cid}), k, needed, cap) < needed:
            break
        picked.add(cid)
    if not picked:
        return [], []
    arms = complete_arms(in_run_order(picked), k, FIXED_ARMS + len(sections), cap)
    picked_ids = [c for c in case_ids if c in picked]
    return picked_ids, sections[: arms - FIXED_ARMS]


def plan_fits(
    case_ids: list[str],
    k: int,
    n_arms: int,
    sections: list[str],
    cap: float,
    reports: list[Path],
    rerun_flags: Callable[[list[str], list[str]], str],
) -> bool:
    """Print what a plan costs, and whether it finishes the arms a measurement needs.

    Each case is priced at its costliest past run in `reports`, else UNPRICED_RUN_USD. A
    plan that cannot finish both fixed arms, and one section arm when `sections` is not
    empty, is refused on stderr with `rerun_flags(cases, sections)` for the largest
    selection that fits.
    """
    priced = run_costs(report_entries(reports))
    per_run = {c: priced.get(c, UNPRICED_RUN_USD) for c in case_ids}
    arm_usd = k * sum(per_run.values())
    finished = complete_arms(list(per_run.values()), k, n_arms, cap)
    needed = min_arms(sections)
    print(
        f"estimate: {sum(c in priced for c in per_run)} of {len(case_ids)} case(s) priced "
        f"from past reports, the rest at ${UNPRICED_RUN_USD:.2f} a run; ${arm_usd:.2f} an "
        f"arm; {finished} of {n_arms} arm(s) expected to finish"
    )
    if finished >= needed:
        return True
    print(
        f"refusing: the {needed} arms a measurement needs cost ${needed * arm_usd:.2f}, "
        f"and the cap leaves ${cap - WORST_RUN_USD:.2f} once a run's "
        f"${WORST_RUN_USD:.2f} worst case is held back",
        file=sys.stderr,
    )
    fit_cases, fit_sections = fitting_selection(case_ids, per_run, k, sections, cap)
    print(
        f"largest selection that fits: {rerun_flags(fit_cases, fit_sections)}"
        if fit_cases
        else f"no selection fits: one case's {needed} arms exceed the cap",
        file=sys.stderr,
    )
    return False


def selection_flags(
    plan: dict, defaults: dict, case_ids: list[str], sections: list[str]
) -> str:
    """The `run` flags for a selection, ready to paste back.

    `plan` maps option dests (doc, k, cap_usd) to the values the selection was priced at.
    Each one that differs from `defaults` is printed too, because a command without it
    would price, or fail to find, a different plan.
    """
    flags = [
        f"--{name.replace('_', '-')} {shlex.quote(str(value))}"
        for name, value in plan.items()
        if value != defaults[name]
    ]
    flags += [f"--case {c}" for c in case_ids]
    flags += [f"--section {shlex.quote(h)}" for h in sections]
    return " ".join(flags)
