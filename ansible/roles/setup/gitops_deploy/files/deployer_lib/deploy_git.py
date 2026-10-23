# ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_git.py
"""What a tick should do given the two HEADs, the hold, and the CI verdict.

`next_action` is the decision; `ci_verdict` feeds it the gate, reducing the check-runs that
deploy_toolbox reads through host_lib.github_get; `is_diverged` and `behind_marker` are the two watchdog signals a parked host raises; the
`dirty_*` helpers throttle the dirty-tree page.
"""

from __future__ import annotations

from collections.abc import Callable

# ── CI gate ───────────────────────────────────────────────────────────────────────────────────
# A GitHub check-run conclusion that counts as "this commit is good". `skipped` and `neutral` are
# passes on purpose: ci.yml's renovate-config job runs unconditionally but gates its real work on a
# per-PR step condition, so it legitimately reports a non-`success` completion.
_CI_PASS_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
# Deliberately NOT failures — these mean "no verdict for this SHA", not "this SHA is bad".
# ci.yml sets `concurrency: cancel-in-progress` keyed on github.ref, so two pushes to master in
# quick succession CANCEL the first run. Mapping `cancelled` to a failure would page on an
# ordinary back-to-back push; the tip's own run is the one that supplies the verdict.
_CI_NO_VERDICT_CONCLUSIONS = frozenset(
    {"cancelled", "stale", "skipped_by_concurrency", None}
)

# DECIDED: the verdict readers apply the gate job's own rule to the jobs' check-runs when the gate
# job has not concluded (#5507). The gate aggregates its `needs` in about 3 seconds but needs a
# runner of its own: master run 39074978426 finished every real job at 22:39:45Z, and the gate then
# sat queued past 22:51, so two landings gave up with `ci-timeout`. GitHub has no queue priority,
# so creating the gate job earlier would not help. The gate job stays, for branch protection, and
# a concluded gate is believed, with one exception (#5931): a gate that failed only because its own
# run was cancelled. The gate is `if: always()`, so a cancelled run still runs it, and it fails on
# the cancelled needs. PR #5922's run 39791122833 attempt 1 did that: its jobs were cancelled at
# 13:57Z, gate check-run 119868712075 concluded `failure`, and `land.sh` ended `pr-ci-red` on a head
# that no job had failed on. `ci_verdict` leaves that gate out once its `read_suites` reader shows
# the gate's check suite concluded `cancelled`. A cancelled need alone does not prove it: GitHub
# reports a job past its `timeout-minutes` as `cancelled` too, on a run nobody cancelled. The
# deployer's reader passes no `read_suites`, so its gate keeps such a gate red. The rule lives in
# THIS module because it is the one module both runtimes import: the deployer ships only its role's `files/`, so a rule under `scripts/`
# would not exist on the host, while `scripts/deploy_tools/await_ci.py` already imports this file.
# ENFORCED: `ansible/tests/repo/test_ci_jobs_verdict_matches_gate.py` holds the names below equal
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
        "pytest (shard 1 of 6)",
        "pytest (shard 2 of 6)",
        "pytest (shard 3 of 6)",
        "pytest (shard 4 of 6)",
        "pytest (shard 5 of 6)",
        "pytest (shard 6 of 6)",
        "ansible-lint",
        "deck mod (claude plugin validate + test)",
        "flux round trip (placeholder render against stub render)",
        "sops-manifest-type-proof (an all-digit stringData value stays a string)",
        "host_packages (24.04, 3.12)",
        "host_packages (26.04, 3.14)",
        "host_tools_arm64",
    }
)


def _run_state(run: dict) -> str:
    """`pass` / `skipped` / `pending` / `fail` for one check-run."""
    if run.get("status") != "completed":
        return "pending"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "pass"
    if conclusion == "skipped":
        return "skipped"
    if conclusion in _CI_NO_VERDICT_CONCLUSIONS:
        return "pending"
    return "fail"


def jobs_verdict(check_runs: list[dict]) -> str:
    """The gate job's own pass condition, applied to the check-runs of the jobs it aggregates.

    Mirrors the `run:` script of the `prek` job in ci.yml. A job passes when it succeeded. A
    skipped job passes only when `prove` succeeded and every dependent skipped: each dependent
    skips only on `needs.prove.outputs.proven == 'true'` or a cancelled run, so a skip means
    `proven` was true, and a check-run cannot show that output itself. `prove` is skipped on a
    run that is not a push. A cancelled or failed `prove` (a runner fault) runs every dependent
    whatever `proven` says, so they must all pass.
    Fail-closed: any failure is `fail`, a missing or unfinished job is
    `pending`, and a skipped job without that proof is `pending`, so the gate job's own
    conclusion decides it.
    """
    states: dict[str, set[str]] = {}
    for run in check_runs:
        name = run.get("name")
        if name == CI_PROVE_JOB or name in CI_GATE_NEEDS_JOBS:
            # A cancelled or failed `prove` broke on the runner, and every dependent ran (#5783).
            unproven = name == CI_PROVE_JOB and run.get("conclusion") in (
                "cancelled",
                "failure",
            )
            states.setdefault(name, set()).add(
                "unproven" if unproven else _run_state(run)
            )
    if any("fail" in s for s in states.values()):
        return "fail"
    prove = states.get(CI_PROVE_JOB)
    if prove is None or "pending" in prove:
        return "pending"
    skipped = 0
    for name in CI_GATE_NEEDS_JOBS:
        job = states.get(name)
        if job is None or "pending" in job:
            return "pending"
        # A draft run's skip beside the ready run's pass on the same head is a pass (#5563).
        if "skipped" in job and "pass" not in job:
            skipped += 1
    if not skipped:
        return "pass"
    # The proven path skips EVERY dependent. A lone skip beside a success is not a state the
    # workflow produces, so it is not read as proof.
    if prove == {"pass"} and skipped == len(CI_GATE_NEEDS_JOBS):
        return "pass"
    return "pending"


# A commit's check-runs and check-suites, 100 to a page. One page hid the runs past the 100th, so
# a failing re-run on page 2 read as a pass (#5977); promote's forge reader pages the same way.
CHECK_PAGE_SIZE, CHECK_PAGES_MAX = 100, 20


def read_check_pages(fetch_page: Callable[[str], object], key: str) -> list[dict]:
    """Every entry of `key` across a check-runs or check-suites listing, page after page.

    `fetch_page(query)` returns the parsed body of the listing's URL with `query` appended, a
    `?per_page=...&page=...` string this walk builds. The walk stops on a short page or once it holds the body's `total_count`. A read short of
    `total_count` is refused rather than returned as complete. A run created between two page
    reads can push the count past `total_count`, so only a shortfall is refused.

    Raises:
        ValueError: a body without a `key` list or an integer `total_count`, a short read, or
            more than CHECK_PAGES_MAX pages. Both callers read a ValueError as `pending`.
    """
    found: list[dict] = []
    for page in range(1, CHECK_PAGES_MAX + 1):
        body = fetch_page(f"?per_page={CHECK_PAGE_SIZE}&page={page}")
        entries = body.get(key) if isinstance(body, dict) else None
        total = body.get("total_count") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            raise ValueError(f"{key} page {page}: the body has no {key} list")
        if not isinstance(total, int) or isinstance(total, bool):
            raise ValueError(f"{key} page {page}: the body has no total_count")
        found.extend(entries)
        if len(entries) < CHECK_PAGE_SIZE or len(found) >= total:
            if len(found) < total:
                raise ValueError(f"{key}: read {len(found)} of {total}")
            return found
    raise ValueError(f"{key}: more than {CHECK_PAGES_MAX} pages")


def cancellation_suspects(check_runs: list[dict]) -> set:
    """The check-suite ids of the failed gates that a cancelled run may explain (#5931).

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
            gate.get("name") != CI_GATE_NAME
            or gate.get("status") != "completed"
            or gate.get("conclusion") != "failure"
        ):
            continue
        suite = (gate.get("check_suite") or {}).get("id")
        needs = [
            r
            for r in check_runs
            if r.get("name") in CI_GATE_NEEDS_JOBS
            and (r.get("check_suite") or {}).get("id") == suite
        ]
        if any(r.get("conclusion") == "cancelled" for r in needs) and not any(
            _run_state(r) == "fail" for r in needs
        ):
            suspects.add(suite)
    return suspects


def ci_verdict(
    check_runs: list[dict],
    required: frozenset[str] | set[str],
    read_suites: Callable[[], list[dict]] | None = None,
) -> str:
    """Reduce GitHub's check-runs for ONE commit to `pass` / `pending` / `fail`.

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
    has not concluded, `jobs_verdict` decides from the jobs it aggregates (#5507). A skipped gate
    has not concluded either: only a draft PR's run skips it (#5563). Nor has a gate that failed
    only because its own run was cancelled (#5931): a `cancellation_suspects` gate whose suite
    `read_suites()` reports `cancelled`. It is called only when a suspect exists, so an ordinary
    verdict costs no check-suites read, and a read that fails is `pending`. A caller that passes
    no reader keeps such a gate red.
    """
    if not required:
        return "pass"
    cancelled = set()
    suspects = cancellation_suspects(check_runs) if read_suites else set()
    if suspects and read_suites:
        try:
            suites = read_suites()
        except OSError, ValueError:
            return "pending"
        cancelled = {s.get("id") for s in suites if s.get("conclusion") == "cancelled"}
        cancelled &= suspects
    states: dict[str, set[str]] = {}
    for run in check_runs:
        name = run.get("name")
        if name not in required:
            continue
        # The gate skips only on a draft PR (#5563), and a draft run tested nothing. Its gate is
        # left out, so a head whose only gate is a draft's reads as the jobs decide: pending.
        if name == CI_GATE_NAME and run.get("conclusion") == "skipped":
            continue
        if (
            name == CI_GATE_NAME
            and (run.get("check_suite") or {}).get("id") in cancelled
        ):
            continue
        if run.get("status") != "completed":
            state = "pending"
        elif run.get("conclusion") in _CI_PASS_CONCLUSIONS:
            state = "pass"
        elif run.get("conclusion") in _CI_NO_VERDICT_CONCLUSIONS:
            state = "pending"
        else:
            state = "fail"
        states.setdefault(name, set()).add(state)
    gate = states.get(CI_GATE_NAME, {"pending"})
    if CI_GATE_NAME in required and "fail" not in gate and "pending" in gate:
        states[CI_GATE_NAME] = {jobs_verdict(check_runs)}
    if any("fail" in s for s in states.values()):
        return "fail"
    # A name with no runs at all is a freshly-pushed SHA whose workflow hasn't registered yet.
    if any("pending" in states.get(name, {"pending"}) for name in required):
        return "pending"
    return "pass"


def ci_walk_candidates(
    rev_list: list[str], hold_sha: str | None, max_walk: int
) -> list[tuple[int, str]]:
    """The ancestors below the tip the gate may spend a CI request on, newest first.

    DECIDED: the gate chooses the NEWEST GREEN ANCESTOR, the walk is bounded, and a red
    ancestor is never chosen. `rev_list` is `git rev-list --first-parent <local>..<origin>`,
    newest first, so every SHA in it is `--ff-only` reachable from `local` and the deployer
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


def next_action(
    local_head: str,
    origin_head: str,
    hold_sha: str | None,
    dirty: bool = False,
    origin_ahead: bool = True,
    ci: str = "pass",
) -> str:
    """Decide what one tick does, given the two HEADs, the hold, and the CI verdict.

    Args:
        local_head: this host's current git SHA.
        origin_head: `origin/master`'s SHA as of this tick's fetch.
        hold_sha: the SHA a previous failed deploy pinned, or None if nothing is held.
        dirty: whether the working tree has uncommitted local changes.
        origin_ahead: whether `local_head` is an ancestor of `origin_head` (a normal
            fast-forward is possible). False also covers the diverged case.
        ci: `ci_verdict`'s reduction for `origin_head` — "pass", "pending", or "fail".

    Returns:
        One of "dirty", "noop", "skip_hold", "ci_failed", "ci_pending", or "deploy".
    """
    # A dirty working tree (operator mid-edit) is a healthy skip, not an outage,
    # and must never be deployed from — so it short-circuits every other outcome.
    if dirty:
        return "dirty"
    if origin_head == local_head:
        return "noop"
    if hold_sha is not None and origin_head == hold_sha:
        return "skip_hold"
    # The deployer is pull-based and only fast-forwards, so it must act ONLY when
    # origin is strictly ahead of local. `origin_ahead=False` means origin is an
    # ancestor of local (the operator committed locally but hasn't pushed) or the
    # two diverged — either way there is nothing to fast-forward. Deploying here
    # would diff local..origin (the *reverse* of the un-pushed commits) and
    # mis-fire a redeploy + false rollback, so treat it as a no-op.
    if not origin_ahead:
        return "noop"
    # CI gate. `origin_head` is the SHA this tick would `--ff-only` onto, and `ci` is that
    # SHA's own check-run verdict — not necessarily `origin/master`'s tip. When the tip is
    # pending or red, `deploy_phases.assess` walks back to the newest green ancestor and
    # passes THAT SHA here (see `ci_walk_candidates`), so the property this gate holds is:
    # the tree the host ends up running always has its own green CI verdict. A tick can span
    # local..origin_head — several commits — and the intermediate ones are NOT individually
    # gated; that is intentional, because the last of them is the tree the host runs.
    # Commits ABOVE origin_head are not deployed at all this tick.
    # Both outcomes below return WITHOUT ff-merging, so the host stays parked on `local` and
    # `behind_marker` records it — a persistently red master therefore pages through the
    # existing behind-origin watchdog rather than needing its own escalation. That watchdog
    # still fires after an ancestor fast-forward too: `gitops_deploy.entrypoint` re-resolves
    # the REAL origin after the tick, so `behind_since` keeps naming the tip.
    if ci == "fail":
        return "ci_failed"
    if ci == "pending":
        return "ci_pending"
    return "deploy"


def is_diverged(
    origin_head: str, local_head: str, origin_ahead: bool, local_ahead: bool
) -> bool:
    """True when origin and local have DIVERGED — they differ, neither an ancestor of the other.

    The deployer can neither fast-forward (`origin_ahead`) nor is this the healthy
    committed-but-unpushed local state (`local_ahead`, which secret-rotate owns and which stays
    a plain noop). A diverged tree noops forever while origin's new commits — a
    Renovate/security bump — never deploy, and both GitOps monitors stay green (last_run keeps
    ticking, no hold). The deployer records this so GitOps Status surfaces it instead of
    camouflaging it as a healthy noop (2026-07-15 review L3).
    """
    return origin_head != local_head and not origin_ahead and not local_ahead


def behind_marker(
    behind: bool,
    origin_head: str,
    existing: str | None,
    now: float,
    *,
    fast_forwarded: bool,
) -> str | None:
    """Next value of the `behind_since` marker.

    Either "<origin_sha> <unix_ts_first_seen>", or None to clear it.

    `behind` means origin is strictly ahead of local at the END of a tick — we saw new commits and
    did not converge on them. Unlike is_diverged this is not a broken state on its own: a routine
    push is behind for exactly one tick, and the dirty-tree path is behind for as long as the
    operator is editing (deliberately healthy). What is NOT healthy is the deployer making no
    progress, which is why the marker carries a first-seen timestamp and monitor-bridge pages on
    AGE, not on presence.

    The stamp therefore measures TIME WITHOUT A FAST-FORWARD, not time behind the tip. A tick
    that moved the tree re-stamps; a tick that moved nothing keeps what is there. Before the
    ancestor walk those were the same thing — the tick either crossed the whole range or parked —
    and the stamp reset only on convergence, so that a trickle of pushes to a permanently-stuck
    host could not restart the clock. It still cannot: a stuck host does not fast-forward. But a
    deployer landing every merge at the newest green ancestor is behind the tip on EVERY tick,
    and under the old rule its stamp aged past six hours while it was working normally.

    Args:
        behind: whether origin is strictly ahead of local at the end of the tick.
        origin_head: the REAL `origin/<branch>`, so the marker names where origin actually is
            even on a tick that fast-forwarded to an ancestor of it.
        existing: the marker as this tick found it, or None.
        now: the current time, in `time.time()` terms.
        fast_forwarded: whether this tick moved the tree at all. The caller compares HEAD
            before and after, so a rollback — which resets back to where it started — counts
            as no progress, which is what it is.
    """
    if not behind:
        return None
    if existing and not fast_forwarded:
        parts = existing.split()
        if len(parts) == 2:
            return f"{origin_head} {parts[1]}"
    return f"{origin_head} {now}"


def dirty_summary(porcelain: str, limit: int = 12) -> str:
    """The paths making the tree dirty, with their porcelain status codes, for one log line.

    Names the paths rather than reporting the state, because the state alone sends the reader
    to `git status` on a host they may not be on. `??` is the code worth seeing: it means an
    untracked file, and `git status --porcelain` counts those — so the tree can be dirty with
    nothing modified, which is the case that is genuinely surprising.

    Porcelain v1 is two status characters, a space, then the path, so the path starts at
    index 3. A rename arrives as `R  old -> new` and is left whole: both halves are the fact.
    """
    entries = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        code, path = line[:2].strip() or "??", line[3:].strip()
        entries.append(f"{code} {path}" if path else code)
    if not entries:
        return "(no entries — the tree changed under us)"
    if len(entries) > limit:
        extra = len(entries) - limit
        return ", ".join(entries[:limit]) + f", +{extra} more"
    return ", ".join(entries)


def dirty_alert_slot(now, morning_hour: int = 8, evening_hour: int = 20) -> str | None:
    """The dirty-alert time slot `now` falls in, or None before the morning slot.

    The day has two slots — morning (`morning_hour` <= h < `evening_hour`) and
    evening (h >= `evening_hour`) — so a persistently dirty tree pages at most
    twice per America/Chicago day (~08:00 and ~20:00 CT). Before `morning_hour`
    there is no slot, so an overnight-dirty tree stays quiet until the morning.
    Returned as `YYYY-MM-DD:am|pm` so the caller can store it as the throttle key
    and compare the next tick against it.
    """
    if now.hour < morning_hour:
        return None
    slot = "am" if now.hour < evening_hour else "pm"
    return f"{now.date().isoformat()}:{slot}"


def should_alert_dirty(
    now,
    last_alert_slot: str | None,
    morning_hour: int = 8,
    evening_hour: int = 20,
) -> bool:
    """Whether this tick should send the dirty-working-tree Discord alert.

    The deploy timer fires every 30 min, so an unthrottled dirty alert pages the
    webhook through every long edit session. This caps it to at most once per slot
    (see `dirty_alert_slot`) — once in the morning (~08:00 CT) and once at night
    (~20:00 CT) — and stays silent before the morning slot, so an overnight-dirty
    tree pages twice a day instead of all night.

    `now` is the current time already in the target timezone (America/Chicago);
    `last_alert_slot` is the slot key (`YYYY-MM-DD:am|pm`) we last alerted on, or
    None. The caller records `dirty_alert_slot(now, ...)` whenever this returns True.
    """
    slot = dirty_alert_slot(now, morning_hour, evening_hour)
    if slot is None:
        return False
    return last_alert_slot != slot
