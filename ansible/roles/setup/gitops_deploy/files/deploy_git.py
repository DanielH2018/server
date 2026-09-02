# ansible/roles/setup/gitops_deploy/files/deploy_git.py
"""What a tick should do given the two HEADs, the hold, and the CI verdict.

`next_action` is the decision; `ci_verdict` and `github_token` feed it the gate;
`is_diverged` and `behind_marker` are the two watchdog signals a parked host raises; the
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


def github_token(environ: dict[str, str], run: Callable) -> str | None:
    """A GitHub token for the check-runs gate, or None to query anonymously.

    `GH_TOKEN` / `GITHUB_TOKEN` in the environment win, then `gh auth token` — the gh CLI on
    daniel-box is logged in as the repo owner, and the deployer runs as that same user. The
    lookup is best-effort: a missing gh, an expired login, or a slow keyring all return None,
    and the caller queries anonymously exactly as it did before this existed.

    Why authenticate a read of a public repo. The anonymous limit is 60 requests/hour PER
    SOURCE IP, and every GitHub call from this host shares it: the tick's gate, `await_ci.py`
    polling every 20s for up to 900s during a landing (45 requests per run), renovate_notify,
    the ruleset-drift cron. Two `land.sh` runs in an hour exhaust it, after which the tick's
    gate reads `HTTP Error 403: rate limit exceeded` and defers as `CI not finished` — which
    is correct fail-closed behaviour and also a deploy outage nobody asked for. Measured
    2026-09-01: two landings and a manual tick, three 403 deferrals. Authenticated, the
    limit is 5000/hour per token.
    """
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = environ.get(name, "").strip()
        if value:
            return value
    try:
        proc = run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    token = (proc.stdout or "").strip()
    return token or None


def github_auth_headers(token: str | None) -> dict[str, str]:
    """The `Authorization` header for `token`, or nothing for an anonymous request."""
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def ci_verdict(check_runs: list[dict], required: frozenset[str] | set[str]) -> str:
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
    """
    if not required:
        return "pass"
    states: dict[str, set[str]] = {}
    for run in check_runs:
        name = run.get("name")
        if name not in required:
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
    if any("fail" in s for s in states.values()):
        return "fail"
    # A name with no runs at all is a freshly-pushed SHA whose workflow hasn't registered yet.
    if any("pending" in states.get(name, {"pending"}) for name in required):
        return "pending"
    return "pass"


def next_action(
    local_head: str,
    origin_head: str,
    hold_sha: str | None,
    dirty: bool = False,
    origin_ahead: bool = True,
    ci: str = "pass",
) -> str:
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
    # CI gate. `origin` is the tip we would `--ff-only` onto, and the tip is the SHA whose
    # check-runs decide the tick. A tick can span local..origin — several commits — and the
    # intermediate ones are NOT individually gated; that is intentional, because the tip is the
    # tree the host actually ends up running. Both outcomes return WITHOUT ff-merging, so the host
    # stays parked on `local` and `behind_marker` records it — a persistently red master therefore
    # pages through the existing behind-origin watchdog rather than needing its own escalation.
    if ci == "fail":
        return "ci_failed"
    if ci == "pending":
        return "ci_pending"
    return "deploy"


def is_diverged(
    origin_head: str, local_head: str, origin_ahead: bool, local_ahead: bool
) -> bool:
    """True when origin and local have DIVERGED — they differ yet neither is an ancestor of the
    other, so the deployer can neither fast-forward (`origin_ahead`) nor is this the healthy
    committed-but-unpushed local state (`local_ahead`, which secret-rotate owns and which stays a
    plain noop). A diverged tree noops forever while origin's new commits — a Renovate/security bump
    — never deploy, and both GitOps monitors stay green (last_run keeps ticking, no hold). The
    deployer records this so GitOps Status surfaces it instead of camouflaging it as a healthy noop
    (2026-07-15 review L3)."""
    return origin_head != local_head and not origin_ahead and not local_ahead


def behind_marker(
    behind: bool, origin_head: str, existing: str | None, now: float
) -> str | None:
    """Next value of the `behind_since` marker: "<origin_sha> <unix_ts_first_seen>", or None to
    clear it.

    `behind` means origin is strictly ahead of local at the END of a tick — we saw new commits and
    did not converge on them. Unlike is_diverged this is not a broken state on its own: a routine
    push is behind for exactly one tick, and the dirty-tree path is behind for as long as the
    operator is editing (deliberately healthy). What is NOT healthy is staying behind, which is why
    the marker carries a first-seen timestamp and monitor-bridge pages on AGE, not on presence.

    The timestamp survives across ticks and resets only when we converge — deliberately not per-SHA.
    Re-stamping on each new origin SHA would let a steady trickle of pushes to a permanently-stuck
    host restart the clock forever, which is the exact failure this is meant to catch. The SHA is
    refreshed each tick so the alert names where origin actually is.
    """
    if not behind:
        return None
    if existing:
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
