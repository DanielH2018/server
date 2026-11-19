"""Whether a commit's CI is green, and which ancestor a gate may choose when the tip is not.

Every reader parses GitHub's check-runs into `CheckRun` where it fetches them. `ci_verdict`
reduces ONE commit's runs to a `CIVerdict` over the required contexts, and `CheckRun.state` is
the one rule for what a single run says. `ci_walk_candidates` bounds the ancestor walk that runs
when the tip is not green. Stdlib only and importing nothing first-party, because the host
packages' builder grades the same rule and `homelab-checkout-sync` grades the fetched tip with it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

# ── CI gate ───────────────────────────────────────────────────────────────────────────────────
# A GitHub check-run conclusion that counts as "this commit is good". `skipped` and `neutral` are
# passes on purpose: ci.yml's renovate-config job runs unconditionally but gates its real work on a
# per-PR step condition, so it legitimately reports a non-`success` completion.
_CI_PASS_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
# Deliberately NOT failures — these mean "no verdict for this SHA", not "this SHA is bad".
# ci.yml groups master pushes by SHA and cancels in progress only on a pull_request, so a later
# master push never cancels an earlier run. A commit still reads `cancelled` when a person cancels
# its run, when a job passes its `timeout-minutes`, or on a PR head a force-push superseded, and a
# push GitHub never ran CI for has no conclusion at all (`None`). None of these says the SHA is bad.
_CI_NO_VERDICT_CONCLUSIONS = frozenset(
    {"cancelled", "stale", "skipped_by_concurrency", None}
)

# DECIDED: the verdict readers apply the gate job's own rule to the jobs' check-runs when the gate
# job has not concluded. The gate aggregates its `needs` in about 3 seconds but needs a
# runner of its own: it can sit queued for minutes after every real job finished, so landings
# give up with `ci-timeout`. GitHub has no queue priority, so creating the gate job earlier
# would not help. The gate job stays, for branch protection, and a concluded gate is believed,
# with one exception: a gate that failed only because its own
# run was cancelled. The gate is `if: always()`, so a cancelled run still runs it, and it fails on
# the cancelled needs, so `land.sh` ends `pr-ci-red` on a head that no job failed on.
# `ci_verdict` leaves that gate out once its `read_suites` reader shows
# the gate's check suite concluded `cancelled`. A cancelled need alone does not prove it: GitHub
# reports a job past its `timeout-minutes` as `cancelled` too, on a run nobody cancelled. Both
# readers pass `read_suites`: `await_ci.verdict_for` and `CheckoutSync.verdict`.
# The rule lives in THIS module because it is the one module both runtimes import: the
# checkout-sync component ships `hosts/lib/` modules only, so a rule under `scripts/` would not
# exist on the host, while `scripts/deploy_tools/await_ci.py` already imports this file.
# ENFORCED: `tests/repo/test_ci_jobs_verdict_matches_gate.py` holds the names below equal
# to the gate's `needs` in .github/workflows/ci.yml and runs the gate's own script against
# `jobs_verdict`.
CI_GATE_NAME = "prek (lint + validate + tests + secrets)"
# The check-run name of the `prove` job, whose `proven` output the gate reads.
CI_PROVE_JOB = "prove the pushed tree was tested"
# The check-run name of every other job in the gate's `needs`. GitHub names a matrix leg's
# check-run after the leg, so each leg is listed.
CI_GATE_NEEDS_JOBS = frozenset(
    {
        "hooks (lint + validate + secrets + docs)",
        "PR gates (cross-plane order + cluster pins)",
        "pytest (shard 1 of 6)",
        "pytest (shard 2 of 6)",
        "pytest (shard 3 of 6)",
        "pytest (shard 4 of 6)",
        "pytest (shard 5 of 6)",
        "pytest (shard 6 of 6)",
        "actions lockfile (gh actions-lock --verify)",
        "flux round trip (placeholder render against stub render)",
        "sops-manifest-type-proof (an all-digit stringData value stays a string)",
        "build the host packages (render, nfpm, lint)",
        "host_packages (24.04, 3.12)",
        "host_packages (26.04, 3.14)",
        "host_packages_bootstrap (24.04, 3.12)",
        "host_packages_bootstrap (26.04, 3.14)",
        "host_tools_arm64",
    }
)
# The check-run name of the job that builds the host packages and says whether the jobs below
# have anything to run.
CI_BUILD_JOB = "build the host packages (render, nfpm, lint)"
# The gate needs that read the build's artifacts. Each may skip at job level when the build
# succeeded and had no work for it, and the gate's script passes that skip from the build's
# outputs. A check-run does not carry a job output, so `jobs_verdict` leans on the workflow:
# beside a build that succeeded, such a job skips only when the build had no work for it. A run
# cancelled in the instant between the build's last step and these jobs' `if:` would read the
# same, and the gate job's own conclusion then decides once it exists.
CI_BUILD_READER_JOBS = frozenset(
    {
        "host_packages (24.04, 3.12)",
        "host_packages (26.04, 3.14)",
        "host_packages_bootstrap (24.04, 3.12)",
        "host_packages_bootstrap (26.04, 3.14)",
        "host_tools_arm64",
    }
)


def _legs(prefix: str) -> frozenset[str]:
    return frozenset(n for n in CI_GATE_NEEDS_JOBS if n.startswith(prefix))


# GitHub gives a matrix job that skipped at job level ONE check-run, named after the job and not
# after its legs: the `name:` template with its expressions unexpanded, or the job id when it has
# no `name:`. A run can report `host_packages` and the bootstrap template below as skipped, and
# no leg of either. Each name stands for every leg it maps to.
CI_SKIPPED_MATRIX_JOBS = {
    "pytest (shard ${{ matrix.shard }} of ${{ strategy.job-total }})": _legs(
        "pytest (shard "
    ),
    "host_packages": _legs("host_packages ("),
    "host_packages_bootstrap (${{ matrix.release }}, ${{ matrix.python }})": _legs(
        "host_packages_bootstrap ("
    ),
}


class CIVerdict(StrEnum):
    """One commit's CI verdict. The values are the words the CLIs print and ci-cache.json holds."""

    PASS = "pass"
    PENDING = "pending"
    FAIL = "fail"


class RunState(StrEnum):
    """What one check-run says about its commit. `ci_verdict` counts SKIPPED as a pass."""

    PASS = "pass"
    SKIPPED = "skipped"
    PENDING = "pending"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckRun:
    """One check-run, parsed from GitHub's REST listing or from a PR's status rollup.

    Attributes:
        name: the check-run name, which branch protection calls a context.
        status: lowercase; `completed` once the run finished.
        conclusion: lowercase, or None while the run has none.
        suite_id: the check suite's id, or None when the source carries none.
    """

    name: str
    status: str
    conclusion: str | None
    suite_id: int | None = None

    @classmethod
    def parse(cls, raw: object) -> CheckRun:
        """One entry of the REST `check_runs` listing.

        Raises:
            ValueError: the entry is not a JSON object.
        """
        if not isinstance(raw, dict):
            raise ValueError(f"a check-run is not an object: {raw!r}")
        suite = raw.get("check_suite")
        suite_id = suite.get("id") if isinstance(suite, dict) else None
        return cls(
            name=str(raw.get("name") or ""),
            status=str(raw.get("status") or "").lower(),
            conclusion=str(raw.get("conclusion") or "").lower() or None,
            suite_id=suite_id if type(suite_id) is int else None,
        )

    @classmethod
    def from_rollup(cls, node: object) -> CheckRun:
        """One `statusCheckRollup` node of `gh pr view`.

        A CheckRun node carries an uppercase `conclusion`, empty until it completes. A
        StatusContext node carries only `context` and `state`, where PENDING and EXPECTED
        mean it has not finished.

        Raises:
            ValueError: the node is not a JSON object.
        """
        if not isinstance(node, dict):
            raise ValueError(f"a rollup check is not an object: {node!r}")
        conclusion = str(node.get("conclusion") or "").lower() or None
        if conclusion is None and node.get("state"):
            state = str(node["state"]).lower()
            conclusion = None if state in ("pending", "expected") else state
        status = "completed" if conclusion else str(node.get("status") or "").lower()
        return cls(
            str(node.get("name") or node.get("context") or ""), status, conclusion
        )

    @property
    def state(self) -> RunState:
        """The run's state. A conclusion no set names, such as `action_required`, is FAIL."""
        if self.status != "completed" or self.conclusion in _CI_NO_VERDICT_CONCLUSIONS:
            return RunState.PENDING
        if self.conclusion == "skipped":
            return RunState.SKIPPED
        if self.conclusion in _CI_PASS_CONCLUSIONS:
            return RunState.PASS
        return RunState.FAIL

    @property
    def no_verdict(self) -> bool:
        """Whether the run completed in a conclusion that says nothing about the SHA."""
        return (
            self.status == "completed" and self.conclusion in _CI_NO_VERDICT_CONCLUSIONS
        )

    @property
    def passed(self) -> bool:
        """Whether the run finished in a conclusion that counts as green, a skip included."""
        return self.state in (RunState.PASS, RunState.SKIPPED)


def parse_check_runs(raw: list) -> list[CheckRun]:
    """Every entry of a REST `check_runs` listing, parsed; a non-object entry raises ValueError."""
    return [CheckRun.parse(entry) for entry in raw]


def jobs_verdict(check_runs: list[CheckRun]) -> CIVerdict:
    """The gate job's own pass condition, applied to the check-runs of the jobs it aggregates.

    Mirrors the `run:` script of the `prek` job in ci.yml. A job passes when it succeeded. A
    skipped job passes only when `prove` succeeded and every dependent skipped: each dependent
    skips only on `needs.prove.outputs.proven == 'true'` or a cancelled run, so a skip means
    `proven` was true, and a check-run cannot show that output itself. `prove` is skipped on a
    run that is neither a push nor a labelled pull request. A cancelled or failed `prove` (a runner fault) runs every dependent
    whatever `proven` says, so they must all pass.
    A `CI_BUILD_READER_JOBS` job that skipped beside a build that succeeded also passes: the
    build had no work for it. A skipped matrix job arrives as one check-run under its
    `CI_SKIPPED_MATRIX_JOBS` name, and counts as each of its legs skipped.
    Fail-closed: any failure is `fail`, a missing or unfinished job is
    `pending`, and a skipped job without that proof is `pending`, so the gate job's own
    conclusion decides it.
    """
    states: dict[str, set[str]] = {}
    for run in check_runs:
        name = run.name
        if name in CI_SKIPPED_MATRIX_JOBS and run.state is RunState.SKIPPED:
            for leg in CI_SKIPPED_MATRIX_JOBS[name]:
                states.setdefault(leg, set()).add(RunState.SKIPPED)
        if name == CI_PROVE_JOB or name in CI_GATE_NEEDS_JOBS:
            # A cancelled or failed `prove` broke on the runner, and every dependent ran.
            unproven = name == CI_PROVE_JOB and run.conclusion in (
                "cancelled",
                "failure",
            )
            states.setdefault(name, set()).add("unproven" if unproven else run.state)
    if any(RunState.FAIL in s for s in states.values()):
        return CIVerdict.FAIL
    prove = states.get(CI_PROVE_JOB)
    if prove is None or RunState.PENDING in prove:
        return CIVerdict.PENDING
    built = RunState.PASS in states.get(CI_BUILD_JOB, set())
    skipped = 0
    for name in CI_GATE_NEEDS_JOBS:
        job = states.get(name)
        if job is None or RunState.PENDING in job:
            return CIVerdict.PENDING
        # A draft run's skip beside the ready run's pass on the same head is a pass.
        if RunState.SKIPPED in job and RunState.PASS not in job:
            if built and name in CI_BUILD_READER_JOBS:
                continue
            skipped += 1
    if not skipped:
        return CIVerdict.PASS
    # The proven path skips EVERY dependent. A lone skip beside a success is not a state the
    # workflow produces, so it is not read as proof.
    if prove == {RunState.PASS} and skipped == len(CI_GATE_NEEDS_JOBS):
        return CIVerdict.PASS
    return CIVerdict.PENDING


def cancellation_suspects(check_runs: list[CheckRun]) -> set[int | None]:
    """The check-suite ids of the failed gates that a cancelled run may explain.

    A suspect gate concluded `failure`, its own suite holds a cancelled need, and no need in
    that suite failed outright. The last clause keeps fail-fast red: one pytest shard fails and
    the matrix cancels its siblings. A suspect is not yet excused. A job past its
    `timeout-minutes` also reads `cancelled`, so only the suite's own `cancelled` conclusion,
    which the check-suites API carries and a check-run does not, confirms the run was cancelled.
    `ci_verdict` reads it through its `read_suites` argument.
    """
    suspects = set()
    for gate in check_runs:
        if (
            gate.name != CI_GATE_NAME
            or gate.status != "completed"
            or gate.conclusion != "failure"
        ):
            continue
        needs = [
            r
            for r in check_runs
            if r.name in CI_GATE_NEEDS_JOBS and r.suite_id == gate.suite_id
        ]
        if any(r.conclusion == "cancelled" for r in needs) and not any(
            r.state is RunState.FAIL for r in needs
        ):
            suspects.add(gate.suite_id)
    return suspects


def ci_verdict(
    check_runs: list[CheckRun],
    required: frozenset[str] | set[str],
    read_suites: Callable[[], list[dict]] | None = None,
) -> CIVerdict:
    """Reduce GitHub's check-runs for ONE commit to PASS / PENDING / FAIL.

    `required` is the set of check-run NAMES that must be green — the same strings GitHub reports
    as branch-protection contexts (`prek (lint + validate + tests + secrets)`). An empty set means
    the gate is disarmed, which returns `pass` so a host that hasn't re-templated config.env keeps
    behaving exactly as it does today.

    A name can carry SEVERAL runs (a re-run, or the same workflow triggered by both `push` and
    `pull_request`), so each name is reduced over all of its runs and the worst outcome wins: any
    outright failure makes the whole verdict `fail`, and a name that is missing, still running, or
    has only no-verdict conclusions holds the verdict at `pending`. Pending is the safe direction —
    the caller defers the tick and retries in 30 minutes.

    The gate job's own name is the exception to "missing or still running is pending": while it
    has not concluded, `jobs_verdict` decides from the jobs it aggregates. A skipped gate
    has not concluded either: only a draft PR's run skips it. Nor has a gate that failed
    only because its own run was cancelled: a `cancellation_suspects` gate whose suite
    `read_suites()` reports `cancelled`. It is called only when a suspect exists, so an ordinary
    verdict costs no check-suites read, and a read that fails is `pending`. A caller that passes
    no reader keeps such a gate red.
    """
    if not required:
        return CIVerdict.PASS
    cancelled = set()
    suspects = cancellation_suspects(check_runs) if read_suites else set()
    if suspects and read_suites:
        try:
            suites = read_suites()
        except (OSError, ValueError):
            return CIVerdict.PENDING
        cancelled = {s.get("id") for s in suites if s.get("conclusion") == "cancelled"}
        cancelled &= suspects
    states: dict[str, set[CIVerdict]] = {}
    for run in check_runs:
        if run.name not in required:
            continue
        # The gate skips only on a draft PR, and a draft run tested nothing. Its gate is
        # left out, so a head whose only gate is a draft's reads as the jobs decide: pending.
        if run.name == CI_GATE_NAME and run.conclusion == "skipped":
            continue
        if run.name == CI_GATE_NAME and run.suite_id in cancelled:
            continue
        if run.passed:
            state = CIVerdict.PASS
        elif run.state is RunState.PENDING:
            state = CIVerdict.PENDING
        else:
            state = CIVerdict.FAIL
        states.setdefault(run.name, set()).add(state)
    gate = states.get(CI_GATE_NAME, {CIVerdict.PENDING})
    if (
        CI_GATE_NAME in required
        and CIVerdict.FAIL not in gate
        and CIVerdict.PENDING in gate
    ):
        states[CI_GATE_NAME] = {jobs_verdict(check_runs)}
    if any(CIVerdict.FAIL in s for s in states.values()):
        return CIVerdict.FAIL
    # A name with no runs at all is a freshly-pushed SHA whose workflow hasn't registered yet.
    if any(
        CIVerdict.PENDING in states.get(name, {CIVerdict.PENDING}) for name in required
    ):
        return CIVerdict.PENDING
    return CIVerdict.PASS


def ci_walk_candidates(
    rev_list: list[str], hold_sha: str | None, max_walk: int
) -> list[tuple[int, str]]:
    """The ancestors below the tip the gate may spend a CI request on, newest first.

    DECIDED: the gate chooses the NEWEST GREEN ANCESTOR, the walk is bounded, and a red
    ancestor is never chosen. `rev_list` is `git rev-list --first-parent <local>..<origin>`,
    newest first, so every SHA in it is `--ff-only` reachable from `local` and checkout-sync
    can still only fast-forward. The tip is `rev_list[0]` and is NOT offered: its verdict is
    already in hand, and re-querying it would spend a second request on an answer the caller
    has. `max_walk` bounds GitHub requests per tick INCLUDING that one, so a walk of 10 is
    ten requests in the worst case and the gate's share of the 5000/hour token limit stays
    one bounded burst per tick. `hold_sha` is dropped: a SHA a previous deploy failed on is
    not a tree to converge on, whether it arrives as the tip or as an ancestor of one.
    The caller stops at the first `pass`, so a `fail` ancestor is skipped and never chosen.

    Args:
        rev_list: the incoming commits, newest first, tip included.
        hold_sha: the SHA this host refuses to redeploy, or None.
        max_walk: most CI verdicts one tick may fetch, the tip's included. 0 disarms the
            walk, and so does a negative value — clamped here rather than trusted, because
            `rev_list[:-1]` is "everything but the oldest commit", the opposite of a bound.

    Returns:
        `(distance below the tip, sha)` pairs — the distance is what the journal line prints
        as `<n> behind the tip`, and it counts positions in `rev_list` rather than positions
        in this list, so dropping the held SHA does not renumber the rest.
    """
    return [
        (distance, sha)
        for distance, sha in enumerate(rev_list[: max(0, max_walk)])
        if distance > 0 and sha != hold_sha
    ]
