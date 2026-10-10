"""The ablation budget: the $10 cap, the runner's pre-launch stop, and pricing a plan.

ablate.py checks Budget before every engine call. Before a run starts it also plays the
whole plan through a Budget at prices taken from past engine reports (#4308), so a plan
whose baseline, -doc and first section arm cannot all finish is refused before it spends.
"""

import json
from pathlib import Path

# The operator's ruling on #4259 (2026-10-10): each ablation run stays under $10.
CAP_USD = 10.0
PROJECTION_MARGIN = 1.5
# The most one engine call can bill: the agent's and the judge's --max-budget-usd ($0.75 and
# $0.50, in invoke-agent.mjs and judge.mjs), each tried up to three times. A run launches only
# when this much still fits under the cap, so no run can carry the total past it.
WORST_RUN_USD = 3 * 0.75 + 3 * 0.50
# What one run of a case no past report has priced is assumed to cost. The weekly sweep bills
# about $7 for its 90 runs (#4308), about $0.078 a run, averaged over cases that are not this
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


def fitting_selection(
    case_ids: list[str],
    per_run: dict[str, float],
    k: int,
    sections: list[str],
    cap: float,
) -> tuple[list[str], list[str]]:
    """The largest selection whose baseline, -doc and first section arm all finish.

    Cases are taken cheapest first while those three arms still fit, then sections in the
    given order while their arms still fit. k stays fixed, because the case thresholds
    assume it. Both lists are empty when not even one case fits.
    """
    picked: list[str] = []
    for cid in sorted(case_ids, key=lambda c: (per_run[c], c)):
        trial = picked + [cid]
        if complete_arms([per_run[c] for c in trial], k, 3, cap) < 3:
            break
        picked = trial
    if not picked:
        return [], []
    arms = complete_arms([per_run[c] for c in picked], k, 2 + len(sections), cap)
    return picked, sections[: arms - 2]
