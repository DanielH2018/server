"""The GitOps deployer checks for monitor-bridge: Alive, and Status with its four marker arms.

Both read the deployer's state directory off a hostPath the pod is pinned to; the basenames
and the line parsers come from `gitops_markers`, the deployer's own module copied into this
`files/` (its header says how it is kept fresh), the `owed` ledger's reader comes from
the `gitops_ledger` copy beside it, and Status reads every marker it judges through the
`gitops_hold.DeployerSnapshot` copy. `gitops_status` is a
verdict that reads `cfg` itself — its thresholds default to `None` and resolve inside the
body, because a default argument is evaluated at import and there is no `Config` then — so it
lives here beside its only caller rather than in `verdicts/service.py`, where `gitops_alive`
takes its threshold as an argument.

Split out of `checks/service.py` when the busy-service-lock arm (issue #1847) took that module
past the 600-line cap; the same idiom as `checks/host_edge.py`. Rule and enforcement:
bridge/config.py's header.
"""

import os
import time

from bridge.config import Config
from bridge.sources import Sources
from gitops_markers import (
    CONTENTION_CLEAR_CMD,
    HOLD_CLEAR_CMD,
    MARKERS,
    ManualPlaneEntry,
    NO_PLAYBOOK,
    k8s_deferred_deploy_cmd,
    maximal_apply_warning,
    owed_clear_cmd,
    parse_behind,
    parse_contention,
    target_arg,
)
from gitops_ledger import (
    OWED_K8S_DEFERRED,
    OWED_MANUAL_PLANE,
    held_planes,
    k8s_deferred_entries,
    manual_plane_entries,
)
from gitops_hold import DeployerSnapshot
from verdicts.service import gitops_alive


def _apply_and_clear(pending, narrow) -> str:
    """What to run for every pending role, apply command before its clear.

    The page printed the clear alone, and the clear carries `--applied <the row>` while the
    apply it follows named no tags at all (#2371). An operator who applied an earlier, narrower
    set and then pasted this dropped the tags a later range had added. So the apply command is
    printed beside it, from the same row, and carries `maximal_apply_warning` where the tags it
    names arm something gated — the shape `deployer_park.manual_plane_lines` prints.

    Args:
      pending: the `manual_plane_entries` entries. Should one role appear twice, the OLDEST
        entry decides its playbook, matching the age this page fired on.
      narrow: the tags each role needs, by role, from `manual_plane_entries`.
    """
    oldest: dict[str, ManualPlaneEntry] = {}
    for entry in sorted(pending, key=lambda e: e.at):
        oldest.setdefault(entry.role, entry)
    parts = []
    for role in sorted(oldest):
        entry = oldest[role]
        selected = narrow.get(role) or {role}
        warning = maximal_apply_warning(role, selected)
        how = (
            "apply the role by hand"
            if entry.playbook == NO_PLAYBOOK
            else "apply `%s --tags %s%s` by hand%s"
            % (
                entry.playbook,
                ",".join(sorted(selected)),
                target_arg(role),
                " (WARNING: %s)" % warning if warning else "",
            )
        )
        parts.append(
            "%s, then `%s`" % (how, owed_clear_cmd(OWED_MANUAL_PLANE, role, selected))
        )
    return "; ".join(parts)


def gitops_status(
    cfg: Config,
    hold_sha: str | None,
    diverged_sha: str | None = None,
    behind_since: str | None = None,
    now: float | None = None,
    max_behind_s: float | None = None,
    contention_since: str | None = None,
    max_contention_s: float | None = None,
    owed: str | None = None,
) -> tuple[bool, str]:
    """Pure: is the deploy pipeline in a state needing operator action? Returns (ok, msg).

    Six down states share this monitor: a rolled-back commit HELD pending a revert, a
    local↔origin DIVERGENCE where the deployer can't fast-forward and silently noops forever
    while origin's new commits never deploy (2026-07-15 review L3), the host simply sitting
    BEHIND origin for too long, consecutive ticks deferred on one busy service LOCK, a
    setup role the deployer fast-forwarded past and cannot apply itself, and a promoted image
    BUMP a broad tick fast-forwarded and then deferred for lack of budget.

    The lock arm is a specific instance of behind, reported ahead of it because it names the
    cause and the fix (issue #1847): a contention defer resets the tree, so the host is
    behind and `behind_since` ages — but toward a six-hour threshold sized for a dirty tree,
    while the lock's legitimate holder is a deploy no longer than thirty minutes. Age-gated
    on the streak's FIRST tick, which the deployer never refreshes within a streak.

    The last is the signal a park used to carry. The deployer no longer holds a whole range
    back for a role only a hand can apply — that parked every other session's landing too — so
    it merges, records the role in the `owed` ledger's `manual_plane` class, and this pages once the OLDEST pending role
    is older than the same threshold. Age-gated for the same reason the behind arm is: a role
    recorded ten minutes ago is an ordinary merge, not a fault. Its clear names each role and,
    from the ledger line's `tags` key, the `--applied` tags a narrowed apply needs — the same
    derivation the SessionStart banner prints (#2349).

    It is reported LAST, behind rather than ahead of the behind arm, and the two are
    independent faults rather than a cause and its symptom — so specificity does not order
    them. Urgency does: a host sustained-behind is a deployer that has stopped, and every
    other session's landing exits 4 from deploy.sh until a hand pulls the primary checkout,
    where a pending role blocks nobody and only waits on work nobody has started.

    Behind-ness is the general case the other two are specific instances of, and it is the one that
    caught nothing before: a deferred BROAD change never fast-forwards, so the host parks on an old
    tree while last_run keeps ticking (Alive green) and is_diverged stays false (origin is a strict
    descendant, so Status green too). daniel-server ran a 12-commit-old tree for hours that way on
    2026-08-02, all signals green, until un-deployed DNS records were noticed by hand.

    It is age-gated because being behind is normal in the small: a push is behind for one tick, and
    the dirty-tree path is behind for a whole edit session by design. What the age measures is
    time WITHOUT A FAST-FORWARD, not time behind the tip — the deployer renews `behind_since`
    on any tick that moved the tree, so a host landing every merge at the newest green ancestor
    reads healthy here while a host that has stopped moving still pages. hold/diverged are still
    reported ahead of it — they name the actual cause, where "behind" only names the symptom.

    Args:
      cfg: The configuration; `max_behind_s` defaults to its GITOPS_BEHIND_MAX_S.
      max_behind_s: How long the host may sit behind origin before this pages. None reads
        cfg.GITOPS_BEHIND_MAX_S — a None default rather than the config value itself, because a
        default argument is evaluated once when the module is imported and there is no `cfg` to
        read at that point any more.
      contention_since: the deployer's `contention_since` marker, or None.
      max_contention_s: how long a contention streak may run before this pages. None reads
        cfg.GITOPS_CONTENTION_MAX_S, for the reason `max_behind_s` does.
      owed: the deployer's `owed` ledger, or None. Its `hold_plane` class names the planes
        the held arm prints. Its `manual_plane` class is the
        setup-role arm above (#3392); `gitops_ledger.manual_plane_entries` reads it. Its `k8s_deferred`
        class is the last arm, through `gitops_ledger.k8s_deferred_entries`. That arm is
        age-gated on `max_behind_s`, for the reasons the `manual_plane` arm is: a bump deferred
        ten minutes ago is a tick that ran out of wall clock rather than a fault, and a bump
        nobody has deployed blocks no other session's landing. It is here at all because the
        deferring tick MERGED the bump, so `behind_since` is empty and no later tick's range
        carries it — the failure mode `manual_plane` closed one plane over (#2449).
    """
    max_behind_s = cfg.GITOPS_BEHIND_MAX_S if max_behind_s is None else max_behind_s
    max_contention_s = (
        cfg.GITOPS_CONTENTION_MAX_S if max_contention_s is None else max_contention_s
    )
    if hold_sha:
        # A held BROAD apply is a different fault with a different fix. That arm is
        # forward-only: the tree is already fast-forwarded and a plane playbook failed
        # partway, so reverting the PR undoes nothing and the operator has to fix forward
        # and re-run. hold_sha still decides whether we page — the held planes only say
        # which sentence to print, so a stale line left by a cleared hold cannot page alone.
        # There is one plane per failed apply, and hold_sha is the NEWEST failure's SHA, not
        # each plane's. A Clear after re-running only one would erase another still
        # unapplied. `held_planes` reads them off the ledger's `hold_plane` class, where the
        # deployer records them (#3392). The Clear is named
        # rather than an rm because an rm of hold_sha leaves the ledger's planes behind.
        # The way out comes BEFORE the plane list: `cap_push_msg` cuts from the right, so a
        # long list would otherwise evict the command. It carries the FULL hold_sha, the only
        # form `clear-hold` accepts (#4015).
        planes = held_planes(owed)
        if planes:
            return False, (
                "broad apply held at %s (the newest failure) — fix forward and re-run each "
                "plane, then once all are applied Clear the hold in the deploy UI or run "
                "`%s %s`; %d plane%s unapplied: %s"
                % (
                    hold_sha[:8],
                    HOLD_CLEAR_CMD,
                    hold_sha,
                    len(planes),
                    "s" if len(planes) > 1 else "",
                    "; ".join(planes),
                )
            )
        return False, "deploy held at %s — revert the offending PR" % hold_sha[:8]
    if diverged_sha:
        return False, (
            "local diverged from origin at %s — deployer can't fast-forward, new commits "
            "aren't deploying; reconcile the host tree" % diverged_sha[:8]
        )
    streak = parse_contention(contention_since)
    if streak is not None:
        age_s = (time.time() if now is None else now) - streak.first_seen
        if age_s > max_contention_s:
            return False, (
                "%d consecutive tick(s) deferred on service lock %s for %.0f min (> %.0f min) "
                "— a deploy.sh holding /var/lock/server-deploy-%s.lock has outlived any "
                "legitimate deploy; find it (fuser), end it, then `%s`"
                % (
                    streak.count,
                    streak.lock,
                    age_s / 60,
                    max_contention_s / 60,
                    streak.lock,
                    CONTENTION_CLEAR_CMD,
                )
            )
    behind = parse_behind(behind_since)
    if behind is not None:
        sha, since = behind
        age_s = (time.time() if now is None else now) - since
        if age_s > max_behind_s:
            return False, (
                "host has not fast-forwarded for %.0fh and is behind origin at %s (> %.0fh) "
                "— deploy deferred (broad change / dirty tree); run the manual deploy on the "
                "host" % (age_s / 3600, sha[:8], max_behind_s / 3600)
            )
    pending, narrow = manual_plane_entries(owed)
    if pending:
        oldest = min(e.at for e in pending)
        age_s = (time.time() if now is None else now) - oldest
        if age_s > max_behind_s:
            roles = ", ".join(sorted({e.role for e in pending}))
            return False, (
                "%s unapplied for %.0fh (> %.0fh) — the tick cannot apply %s; %s"
                % (
                    roles,
                    age_s / 3600,
                    max_behind_s / 3600,
                    "it" if len(pending) == 1 else "them",
                    _apply_and_clear(pending, narrow),
                )
            )
    deferred = k8s_deferred_entries(owed)
    if deferred:
        oldest = min(e.at for e in deferred)
        age_s = (time.time() if now is None else now) - oldest
        if age_s > max_behind_s:
            services = sorted({e.service for e in deferred})
            return False, (
                "%s merged but not deployed for %.0fh (> %.0fh) — a broad tick ran out of "
                "budget for the image bump and fast-forwarded past it, so no later range "
                "carries it; deploy `%s`, then `%s`"
                % (
                    ", ".join(services),
                    age_s / 3600,
                    max_behind_s / 3600,
                    k8s_deferred_deploy_cmd(services),
                    owed_clear_cmd(OWED_K8S_DEFERRED, services[0]),
                )
            )
    return True, "no held deploy"


def check_gitops_alive(
    cfg: Config, src: Sources, now: float | None = None
) -> tuple[bool, str]:
    """Checks that the GitOps deployer's last_run marker is fresh.

    Down when the marker is missing (the deployer never completed a tick) or unparseable.
    Returns (ok, msg).
    """
    try:
        with open(os.path.join(cfg.GITOPS_STATE_DIR, MARKERS["last_run"])) as fh:
            ts = float(fh.read().strip())
    except FileNotFoundError:
        return False, "no last_run marker (deployer never completed a tick?)"
    except ValueError:
        return False, "last_run marker unparseable"
    return gitops_alive(
        (now if now is not None else time.time()) - ts, cfg.GITOPS_MAX_AGE_S
    )


def check_gitops_status(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Read the deployer's markers through `gitops_hold.DeployerSnapshot` and judge them.

    An absent marker is None. Any other failure to read one raises, and `gates._evaluate`
    reports DOWN "check error": a `hold_sha` the check cannot read is NOT "no hold". The
    snapshot drops each `owed` ledger line that does not decode, so one torn byte cannot turn
    this into "check error" every cycle and mask the hold, diverged, behind and contention
    arms (#2371).
    """
    snap = DeployerSnapshot.load(cfg.GITOPS_STATE_DIR)
    return gitops_status(
        cfg,
        snap.hold,
        snap.diverged,
        snap.behind,
        contention_since=snap.contention,
        owed=snap.owed,
    )
