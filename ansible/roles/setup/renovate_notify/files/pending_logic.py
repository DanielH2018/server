"""The Pending-Status-Checks dwell arm — pure logic, no I/O (issue #886, #1526).

Split out of `notify_logic.py`, which was at its length cap. The seam is the dashboard's
Pending Status Checks section: everything that parses an ITEM out of it, times that item's
dwell, or renders a message about one lives here, while the dashboard-level parsing it sits
inside (which sections exist, which body is the dashboard's) stays next door.

Imports nothing from `notify_logic`, which imports `PENDING_HEADER` from here — one way, so
the pair cannot cycle. `renovate_notify.py` imports from both.
"""

from __future__ import annotations

from datetime import datetime, timezone

# --- Pending Status Checks: updates that soak forever and never get a PR ---------------
#
# The section lists updates Renovate detected and holds until their `minimumReleaseAge` soak
# elapses, after which an item is supposed to leave it as a branch and a PR. Measured 2026-09-02
# (issue #886), seven had not — grafana/promtail 3.3.0 -> 3.6.11 sat 111 days against a 7-day
# soak — and the only signal was a checkbox in an issue nobody reads line by line.
#
# The mechanism is undetermined: Renovate runs here as the Mend hosted app, whose debug log is
# on developer.mend.io and unreadable from this host. So this measures the SYMPTOM — an item's
# continuous dwell in the section — rather than the cause.
PENDING_HEADER = "## Pending Status Checks"

# Both markers Renovate has used on a pending item's checkbox: the name changed once already.
PENDING_MARKERS = ("unpend-branch=", "approvePr-branch=")


# The two `minimumReleaseAge` values renovate.json sets for the updates that land here: 3 days
# for a k8s-plane digest bump (a re-push of the same mutable tag), 7 for everything else
# non-major. `test_soak_constants_match_renovate_json` asserts both against renovate.json.
DIGEST_SOAK_DAYS = 3
VERSION_SOAK_DAYS = 7

# Grace added on top of an item's own soak before it counts as stuck. It covers the two
# legitimate reasons an item outlives its soak by a little: the `before 6am` schedule means
# Renovate acts once a day, and `prHourlyLimit: 4` can defer a PR across several of those
# windows. Seven days is 28 PR slots — past any honest backlog, under half of promtail's 111.
# Deliberately NOT a flat threshold: the allowance derives from the soak that applies.
PENDING_GRACE_DAYS = 7


def parse_pending(body: str) -> dict[str, str]:
    """Parse the dashboard's Pending Status Checks section into {branch: item description}.

    An item renders as ` - [ ] <!-- unpend-branch=<branch> -->Update foo to v1.2.3`. Renovate
    renamed that marker from `approvePr-branch=` between 2026-09-02 and 2026-09-09, so both are
    matched: the older name alone read 26 live items as zero. Keyed on the BRANCH, not the
    description — nine of the 22 items live on 2026-09-02 were mutable-tag digest bumps whose
    description changes on every upstream re-push, which would reset their clock forever.
    Absent section -> empty dict, the healthy common case.
    """
    if PENDING_HEADER not in (body or ""):
        return {}
    section = body.split(PENDING_HEADER, 1)[1].split("\n## ", 1)[0]
    out: dict[str, str] = {}
    for line in section.splitlines():
        line = line.strip()
        marker = next((m for m in PENDING_MARKERS if m in line), None)
        if marker is None:
            continue
        branch = line.split(marker, 1)[1].split("-->", 1)[0].strip()
        desc = line.split("-->", 1)[1].strip() if "-->" in line else ""
        if branch:
            out[branch] = desc
    return out


def pending_section_unreadable(body: str) -> bool:
    """True when the Pending section is present but yields no items.

    One level below `dashboard_headers_unrecognized`, which reads the HEADER and stays silent
    when only the item marker is renamed. Renovate omits the header when nothing is pending, so
    header-present-and-nothing-parsed is always a parse failure.
    """
    return PENDING_HEADER in (body or "") and not parse_pending(body)


def item_soak_days(description: str) -> int:
    """The `minimumReleaseAge` that applies to one pending item, read from its description.

    Renovate writes "Docker digest to <sha>" for a digest bump and "... tag to vX" / "dependency
    X to vY" for a version bump, and renovate.json soaks those for 3 and 7 days respectively.
    Anything unrecognised gets the LONGER soak: a misread must delay the alert, never invent one.

    The 1-day `vulnerabilityAlerts` soak is deliberately not modelled: nothing in the item text
    distinguishes a CVE-driven bump, and those are scheduled "at any time" so they should never
    linger here. A CVE bump that does get stuck waits the 7+7 version allowance like any other.
    """
    return (
        DIGEST_SOAK_DAYS
        if "digest to" in (description or "").lower()
        else VERSION_SOAK_DAYS
    )


def update_pending_seen(
    prev: dict[str, float], current: dict[str, str], now_epoch: float
) -> dict[str, float]:
    """Carry each still-pending item's first-seen epoch forward; stamp new ones; drop departed ones.

    Pruning is what makes the dwell continuous rather than cumulative: an item that leaves the
    section (its PR was finally raised, or the update stopped being offered) and comes back later
    starts a fresh clock, so a resolved stall cannot re-page off its old timestamp.
    """
    return {branch: prev.get(branch, now_epoch) for branch in current}


def stale_pending(
    seen: dict[str, float],
    current: dict[str, str],
    now_epoch: float,
    grace_days: int = PENDING_GRACE_DAYS,
) -> list[tuple[str, str, int]]:
    """(branch, description, whole days pending) for every item past its soak + `grace_days`.

    Sorted longest-pending first, so the digest names the worst offender before any truncation.
    An item with no first-seen entry is treated as first seen now (dwell 0), not as infinitely
    old — the first run after this ships seeds an empty state file and must not page for all of
    them at once.
    """
    out = []
    for branch, desc in current.items():
        days = (now_epoch - seen.get(branch, now_epoch)) / 86400
        if days > item_soak_days(desc) + grace_days:
            out.append((branch, desc, int(days)))
    return sorted(out, key=lambda item: (-item[2], item[0]))


def pending_fingerprint(items: list[tuple[str, str, int]]) -> str:
    """Dedupe key for the stuck-pending set.

    Carries each item's whole-week dwell so a still-stuck item re-pages weekly, the same
    escalation `_stuck_age_bucket` gives a stuck PR. Keyed on branch for `parse_pending`'s reason.
    """
    return ",".join(
        sorted("%s:%dw" % (branch, days // 7) for branch, _desc, days in items)
    )


PENDING_HEADER_MSG = (
    "\U0001f6d1 Renovate — update(s) stuck in Pending Status Checks (soaked, no PR):"
)


PENDING_REMEDY = (
    "   Tick its box on the Dependency Dashboard to force the PR: the update is "
    "detected but Renovate is not raising it."
)


def render_pending(items: list[tuple[str, str, int]], limit: int = 1500) -> str:
    """Render the stuck-pending list into a Discord message, truncated to `limit` characters.

    Bounded for the same reason `render_digest` is, and more urgently: 22 items at ~145
    characters each render to ~3,200, past Discord's 2,000-character cap. An over-long post is
    rejected, `discord()` returns False, the fingerprint never advances, and the run re-posts
    the same oversized message daily — failing to deliver in the state it exists to report.

    `limit` is lower than render_digest's 1900 because both can be joined into one message.
    `stale_pending` sorts worst-offender-first, so the worst item survives the trim.
    """
    out = [PENDING_HEADER_MSG]
    shown = 0
    for branch, desc, days in items:
        line = " • %s — pending %d days (%s)" % (desc or branch, days, branch)
        # Leave room for the "…and N more" tail and the remedy line.
        if len("\n".join(out + [line, PENDING_REMEDY])) > limit - 30:
            break
        out.append(line)
        shown += 1
    if shown < len(items):
        out.append("…and %d more" % (len(items) - shown))
    out.append(PENDING_REMEDY)
    return "\n".join(out)


# --- Dwell-state loss (issue #1526) -----------------------------------------------------
#
# `stale_pending` treats an item with no first-seen entry as first seen NOW, so a lost or
# unparseable `pending_seen.json` is indistinguishable from the intended first-run bootstrap:
# every clock restarts at zero and the check can find nothing for soak + grace — 14 days for a
# version bump, 10 for a digest one. Measured on daniel-box 2026-09-10: all 27 entries carried
# the identical stamp 1788990155.26 (2026-09-09 21:42), the first run after #1472 fixed
# `parse_pending`, and that window passed with the run reporting healthy throughout.
#
# A check that cannot fire is reporting nothing, not reporting healthy — so the run says so.
PENDING_RESET_MSG = (
    "⚠️ Renovate — the pending-item dwell state was lost, so every stuck-pending "
    "clock restarted at zero. The stuck-pending check can find nothing until %s for a "
    "version bump (%s for a digest bump), whatever an item's real dwell was. Anything "
    "already overdue will not page in that window — check it by hand on "
    "https://github.com/%s/issues/3"
)


def pending_clock_ready(
    now_epoch: float, soak_days: int, grace_days: int = PENDING_GRACE_DAYS
) -> str:
    """The UTC date (YYYY-MM-DD) a clock restarted at `now_epoch` can first report an item stuck.

    Named as a date rather than a duration because the operator's question after a reset is
    "when is this check trustworthy again", and a date survives being read a week later.
    """
    return datetime.fromtimestamp(
        now_epoch + (soak_days + grace_days) * 86400, tz=timezone.utc
    ).strftime("%Y-%m-%d")


def render_pending_reset(now_epoch: float, repo: str) -> str:
    """The Discord line for a dwell-state loss, naming both dates the clocks become usable."""
    return PENDING_RESET_MSG % (
        pending_clock_ready(now_epoch, VERSION_SOAK_DAYS),
        pending_clock_ready(now_epoch, DIGEST_SOAK_DAYS),
        repo,
    )


def pending_reset_fingerprint(now_epoch: float) -> str:
    """Dedupe key for a dwell-state loss: the date the version clocks become usable again.

    Keyed on the date rather than a constant so a SECOND loss during the blind window re-pages
    (it pushes the date out), while the same loss cannot page twice on one day.
    """
    return pending_clock_ready(now_epoch, VERSION_SOAK_DAYS)
