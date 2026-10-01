"""The Pending-Status-Checks dwell arm — pure logic, no I/O (issue #886, #1526).

Split out of `notify_logic.py`, which was at its length cap. The seam is the dashboard's
Pending Status Checks section: everything that parses an ITEM out of it, times that item's
dwell, or renders a message about one lives here, while the dashboard-level parsing it sits
inside (which sections exist, which body is the dashboard's) stays next door.

Imports nothing from `notify_logic`, which imports `PENDING_HEADER` from here — one way, so
the pair cannot cycle. `renovate_notify.py` imports from both.
"""

from __future__ import annotations

import hashlib
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

# The packages renovate.json soaks for LESS than DIGEST_SOAK_DAYS, as {title token: days}.
# `nginx:alpine` and `nginxinc/nginx-unprivileged` are re-pushed about every 3.6 days, so a
# 3-day soak restarted on every re-push left an automerge window the once-daily Renovate run
# usually missed, and both pins only ever landed by hand (#2886). The rule there soaks them
# 1 day. Keyed on the image reference as a pending row spells it, because the title is all this
# module gets. `test_soak_constants_match_renovate_json` asserts this map against renovate.json,
# so a new exception there with no entry here fails rather than silently measuring 3 days.
FAST_DIGEST_SOAK_DAYS = {"nginx:alpine": 1, "nginxinc/nginx-unprivileged:": 1}

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


# The parenthetical every `groupSingleUpdates: true` rule in renovate.json opens its group name
# with. Such a row's title is the GROUP name, so it carries no update type at all — see
# `item_soak_days`. `test_grouped_marker_appears_in_renovate_json` asserts this literal still
# matches a real grouped rule, so a reword of those group names fails here rather than silently
# restoring the four-day-late alert this marker exists to fix (issue #2885).
GROUPED_TITLE_MARKER = "(manual "


def item_soak_days(description: str) -> int:
    """The `minimumReleaseAge` that applies to one pending item, read from its description.

    Renovate writes "Docker digest to <sha>" for a digest bump and "... tag to vX" / "dependency
    X to vY" for a version bump, and renovate.json soaks those for 3 and 7 days respectively.
    Anything unrecognised gets the LONGER soak: a misread must delay the alert, never invent one.

    A GROUPED row is the third shape, and it carries no update type to read. `groupSingleUpdates:
    true` (#2646) makes Renovate title a single-dependency update with its group name, so the row
    reads `Update k8s image ghcr.io/haveagitgat/tdarr (manual — k8s_autodeploy: false, ...)` where
    an ungrouped digest row reads `... Docker digest to df221db`. Those rows took the version soak
    and alerted four days late; five sat 10.3 days on 2026-09-28 against a 10-day threshold with
    the digest naming none of them (#2885).

    # DECIDED: a grouped row takes the DIGEST soak. It is not the unrecognised default breaking —
    # the shape is recognised, and only its type is erased. The cost is that a grouped VERSION
    # bump (`code-server build pins` is a live one) pages four days early, against every grouped
    # digest bump paging four days late today; a grouped bump is manual-merge by construction, so
    # four days early nudges someone who already owes the merge. Rejected alternative: put
    # `{{updateType}}` into each group's `commitMessageTopic` so the title carries the type again
    # — it cannot be verified from this host (Renovate runs as the Mend hosted app), and it edits
    # the title/branch machinery #2620/#2641/#2646 settled.

    A package in `FAST_DIGEST_SOAK_DAYS` takes its own shorter soak, matched on the image
    reference in the title. A GROUPED row for one of those packages names no reference, so it
    falls back to `DIGEST_SOAK_DAYS` and pages two days late — the safe direction, and neither
    pin is grouped today because both roles are `k8s_autodeploy: true`.

    The 1-day `vulnerabilityAlerts` soak is deliberately not modelled: nothing in the item text
    distinguishes a CVE-driven bump, and those are scheduled "at any time" so they should never
    linger here. A CVE bump that does get stuck waits the 7+7 version allowance like any other.
    """
    text = (description or "").lower()
    if "digest to" in text or GROUPED_TITLE_MARKER in text:
        for token, days in FAST_DIGEST_SOAK_DAYS.items():
            if token in text:
                return days
        return DIGEST_SOAK_DAYS
    return VERSION_SOAK_DAYS


# --- Two clocks per pending item (issue #3076) ------------------------------------------
#
# A mutable tag's branch outlives any one digest: Renovate reuses `renovate/k8s-image-nginx`
# across every re-push, so a dwell keyed on the branch alone measures how long SOME digest has
# been pending. renovate-notify reported the FreshRSS cache digest stuck 18.8 days when no
# single digest had been pending more than about 3 (#2886).
#
# So each item carries two first-seen stamps in the one flat `{key: epoch}` state file, in two
# key namespaces:
#
#   "<branch>"                  the branch clock — when the item entered the section, never
#                               reset by a re-push. `churning_pending` reads it.
#   "<branch>#<content token>"  the digest clock — when the update now on the branch first
#                               appeared. A re-push writes a new token, so this resets.
#                               `stale_pending` reads it.
#
# Composite keys rather than a richer value: `read_pending_seen` filters values to int/float,
# so a dict-valued schema would be dropped silently on read and every clock would restart at
# zero on every run, which no check can see.
CONTENT_KEY_SEP = "#"


def content_key(branch: str, description: str) -> str:
    """The digest-clock key for one pending item: its branch plus a hash of its description.

    sha256 and NOT the builtin `hash()`, which is salted per interpreter process: substituting
    it would write a different key on every run, reset the digest clock every run, and leave
    `stale_pending` permanently unable to fire — behind a green `pending_state_lost`, which only
    sees a lost or malformed file, never a self-rotating key. Truncated to 12 hex characters to
    keep the state file readable; a collision only merges two clocks on one branch.

    Hashed rather than carrying the digest literally because the description is all this module
    gets, and the token that changes on a re-push is not always a digest (a version bump, a
    grouped row's rendered name). Any change to the description is a new clock, which is the
    rule the branch key exists to soften.
    """
    digest = hashlib.sha256((description or "").encode("utf-8")).hexdigest()[:12]
    return branch + CONTENT_KEY_SEP + digest


def _content_keys_for(prev: dict[str, float], branch: str) -> list[str]:
    """Every digest-clock key `prev` holds for `branch`, newest-first order not implied."""
    prefix = branch + CONTENT_KEY_SEP
    return [k for k in prev if k.startswith(prefix)]


def _digest_first_seen(
    seen: dict[str, float], branch: str, description: str, now_epoch: float
) -> float:
    """The digest clock for one item, falling back to its branch clock, then to now.

    The branch-clock fallback covers the one run where a legacy state file has not been
    rewritten yet (`update_pending_seen` seeds the digest key, but only the run after a read of
    the old file writes it), and reading a state file written by the pre-#3076 version at all.
    Falling back to the branch clock over-reports, which is the behaviour this fix narrows;
    falling back to `now_epoch` would under-report to zero, which is a check that cannot fire.
    """
    ckey = content_key(branch, description)
    if ckey in seen:
        return seen[ckey]
    return seen.get(branch, now_epoch)


def update_pending_seen(
    prev: dict[str, float], current: dict[str, str], now_epoch: float
) -> dict[str, float]:
    """Carry both clocks forward for each still-pending item; stamp new ones; drop departed ones.

    Rebuilt from `current` rather than edited in place, so pruning covers both namespaces: an
    item that leaves the section (its PR was finally raised, or the update stopped being
    offered) and comes back later starts a fresh clock in both, and a superseded digest key
    goes rather than waiting to resurrect an old clock when a tag oscillates A -> B -> A.

    A legacy entry — a branch key with no digest key beside it, which is every entry written
    before #3076 — seeds its digest clock from the branch clock instead of from now. The
    alternative restarts all 20-odd clocks on the upgrade run and blinds the arm for up to 14
    days, the exact failure `pending_state_lost` exists to report. The cost is that a churning
    branch keeps over-reporting until its next re-push, one cycle at most.
    """
    out: dict[str, float] = {}
    for branch, desc in current.items():
        branch_first = prev.get(branch, now_epoch)
        ckey = content_key(branch, desc)
        if ckey in prev:
            out[ckey] = prev[ckey]
        elif branch in prev and not _content_keys_for(prev, branch):
            out[ckey] = branch_first
        else:
            out[ckey] = now_epoch
        out[branch] = branch_first
    return out


def stale_pending(
    seen: dict[str, float],
    current: dict[str, str],
    now_epoch: float,
    grace_days: int = PENDING_GRACE_DAYS,
) -> list[tuple[str, str, int]]:
    """(branch, description, whole days pending) for every item past its soak + `grace_days`.

    The dwell is the DIGEST clock's — the age of the update now on the branch, not the age of
    the branch (#3076). A re-push restarts it, which is what the soak it is measured against
    means: `minimumReleaseAge` applies to the version on the branch. The branch that churns so
    fast that no digest ever ages out is `churning_pending`'s subject, not this one's.

    Sorted longest-pending first, so the digest names the worst offender before any truncation.
    An item with no first-seen entry is treated as first seen now (dwell 0), not as infinitely
    old — the first run after this ships seeds an empty state file and must not page for all of
    them at once.
    """
    out = []
    for branch, desc in current.items():
        days = (now_epoch - _digest_first_seen(seen, branch, desc, now_epoch)) / 86400
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
    "version bump (%s for a digest bump), and the churn arm nothing until %s, whatever an "
    "item's real dwell was. Anything already overdue will not page in that window — check it "
    "by hand on https://github.com/%s/issues/3"
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
    """The Discord line for a dwell-state loss, naming every date a clock becomes usable.

    A loss restarts the BRANCH clocks too, so the churn arm is blind for its own, longer window
    — `PENDING_CHURN_MULTIPLIER` times soak plus grace (#3076). The third date is the version
    row's, the latest of the three, so it bounds the whole blind window rather than one shape of
    row. Reporting only the dwell arm's two dates would say the check is trustworthy again weeks
    before the churn arm can fire, which is the claim this message exists to refuse.
    """
    return PENDING_RESET_MSG % (
        pending_clock_ready(now_epoch, VERSION_SOAK_DAYS),
        pending_clock_ready(now_epoch, DIGEST_SOAK_DAYS),
        pending_clock_ready(
            now_epoch,
            VERSION_SOAK_DAYS * PENDING_CHURN_MULTIPLIER,
            PENDING_GRACE_DAYS * PENDING_CHURN_MULTIPLIER,
        ),
        repo,
    )


def pending_reset_fingerprint(now_epoch: float) -> str:
    """Dedupe key for a dwell-state loss: the date the version clocks become usable again.

    Keyed on the date rather than a constant so a SECOND loss during the blind window re-pages
    (it pushes the date out), while the same loss cannot page twice on one day.
    """
    return pending_clock_ready(now_epoch, VERSION_SOAK_DAYS)


# --- The branch that churns indefinitely (issue #3076) ----------------------------------
#
# Splitting the dwell onto the digest clock closes the over-report and opens a hole: a tag
# re-pushed faster than its own soak + grace never lets any one digest age out, so
# `stale_pending` can never fire for it however long the branch sits there. `nginx:alpine` is
# the live case — re-pushed about every 3.6 days against the 1-day soak and 7-day grace
# `FAST_DIGEST_SOAK_DAYS` records, so its digest clock resets at roughly half the threshold,
# forever. This arm is the floor under that: it pages on the BRANCH clock.
#
# DECIDED: the churn allowance is three times the item's own soak + grace, not a flat day
# count. Three consecutive full allowances elapsed with the item never once leaving the section
# says the stall is the branch's, not this digest's — one allowance could be a single unlucky
# re-push landing mid-soak. Keeping it a multiple of the per-item allowance preserves
# `PENDING_GRACE_DAYS`'s rule that the threshold derives from the soak that applies: 24 days for
# an `nginx:alpine` row, 30 for an ordinary digest row, 42 for a version row. Rejected
# alternative: page as soon as the branch clock passes soak + grace, which is just the
# pre-#3076 over-report back again under a new name.
PENDING_CHURN_MULTIPLIER = 3


def churning_pending(
    seen: dict[str, float],
    current: dict[str, str],
    now_epoch: float,
    grace_days: int = PENDING_GRACE_DAYS,
    multiplier: int = PENDING_CHURN_MULTIPLIER,
) -> list[tuple[str, str, int, int]]:
    """(branch, description, branch days, digest days) for every endlessly churning item.

    An item `stale_pending` already reports is left out: the two arms page about the same row
    for different reasons, and reporting it twice in one digest tells the operator nothing the
    first line did not. Sorted longest-churning first, like `stale_pending`.
    """
    stuck = {
        branch
        for branch, _desc, _days in stale_pending(seen, current, now_epoch, grace_days)
    }
    out = []
    for branch, desc in current.items():
        if branch in stuck:
            continue
        allowance = (item_soak_days(desc) + grace_days) * multiplier
        branch_days = (now_epoch - seen.get(branch, now_epoch)) / 86400
        if branch_days <= allowance:
            continue
        digest_days = (
            now_epoch - _digest_first_seen(seen, branch, desc, now_epoch)
        ) / 86400
        out.append((branch, desc, int(branch_days), int(digest_days)))
    return sorted(out, key=lambda item: (-item[2], item[0]))


def churn_fingerprint(items: list[tuple[str, str, int, int]]) -> str:
    """Dedupe key for the churning set: whole weeks of BRANCH dwell, so it re-pages weekly.

    The digest dwell is deliberately out of it — it resets every few days by construction, and
    folding it in would re-page on every re-push, which is the noise this arm exists to avoid.
    """
    return ",".join(
        sorted(
            "%s:%dw" % (branch, bdays // 7) for branch, _desc, bdays, _ddays in items
        )
    )


CHURN_HEADER_MSG = (
    "\U0001f501 Renovate — pending update(s) whose branch keeps churning (every re-push "
    "restarts the soak, so no single digest ever ages out):"
)


CHURN_REMEDY = (
    "   The branch has sat in Pending Status Checks across many re-pushes. Tick its box on "
    "the Dependency Dashboard to force the PR, or shorten that package's minimumReleaseAge."
)


def render_churning(items: list[tuple[str, str, int, int]], limit: int = 1200) -> str:
    """Render the churning list into a Discord message, truncated to `limit` characters.

    A third section that can join the other two into one post, so it bounds itself for
    `render_pending`'s reason — and lower, because it is the least urgent of the three.
    """
    out = [CHURN_HEADER_MSG]
    shown = 0
    for branch, desc, branch_days, digest_days in items:
        line = (
            " \u2022 %s \u2014 branch pending %d days, the digest on it only %d (%s)"
            % (
                desc or branch,
                branch_days,
                digest_days,
                branch,
            )
        )
        # Leave room for the "…and N more" tail and the remedy line.
        if len("\n".join(out + [line, CHURN_REMEDY])) > limit - 30:
            break
        out.append(line)
        shown += 1
    if shown < len(items):
        out.append("\u2026and %d more" % (len(items) - shown))
    out.append(CHURN_REMEDY)
    return "\n".join(out)
