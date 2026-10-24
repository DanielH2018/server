# ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_git.py
"""What a tick should do given the two HEADs, the hold, and the CI verdict.

`next_action` is the decision; `ci_verdict` feeds it the gate, reducing the check-runs that
deploy_toolbox reads through host_lib.github_get; `is_diverged` and `behind_marker` are the two watchdog signals a parked host raises; the
`dirty_*` helpers throttle the dirty-tree page.

The CI gate itself, `ci_verdict` with its `jobs_verdict` and `ci_walk_candidates`, lives in
`hosts/lib/ci_verdict.py` since H7.2e, because the host packages' builder and
`homelab-checkout-sync` grade the same rule. The deployer ships that file beside its own modules
(`tasks/code.yml`), so the bare import below resolves on the host. This module re-exports the
names until H7.19a retires the deployer, so every importer of `deploy_git` and `deploy_logic`
keeps working.
"""

from __future__ import annotations

from collections.abc import Callable

from ci_verdict import (  # noqa: F401
    _CI_NO_VERDICT_CONCLUSIONS,
    _CI_PASS_CONCLUSIONS,
    CI_GATE_NAME,
    CI_GATE_NEEDS_JOBS,
    CI_PROVE_JOB,
    cancellation_suspects,
    ci_verdict,
    ci_walk_candidates,
    jobs_verdict,
)

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
