"""What the tick decides from git state alone: deploy, skip, or do nothing.

`is_diverged` is the load-bearing one -- local and origin each holding commits the other lacks
means an automated pull would either lose work or conflict, so the tick has to stop and say so.
A dirty tree outranks every other reason to deploy, its journal line has to name the file that
parked the tree, and its Discord page has to throttle or it fires every tick of a long edit
session. The behind-origin marker is the watchdog for a tick that keeps running while never
catching up. The CI gate has its own file, test_deploy_git_ci.py.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_git.py

from datetime import datetime
from zoneinfo import ZoneInfo

from deploy_git import (
    behind_marker,
    dirty_alert_slot,
    dirty_summary,
    is_diverged,
    next_action,
    should_alert_dirty,
)

# should_alert_dirty takes `now` already in the target zone (America/Chicago).
CT = ZoneInfo("America/Chicago")


def test_next_action_noop_when_in_sync():
    assert next_action("aaa", "aaa", None) == "noop"


def test_next_action_skip_when_origin_is_hold():
    assert next_action("aaa", "bad", "bad") == "skip_hold"


def test_next_action_deploy_when_origin_ahead():
    assert next_action("aaa", "bbb", None) == "deploy"


def test_next_action_deploy_when_hold_is_stale():
    # origin advanced past the held bad SHA (operator reverted) -> deploy again
    assert next_action("aaa", "ccc", "bad") == "deploy"


def test_next_action_dirty_tree_skips_even_in_sync():
    # A dirty working tree is a *healthy* skip (operator mid-edit), not an outage.
    # It must short-circuit to "dirty" so main() can still push liveness instead
    # of going silent and falsely tripping the push monitor's dead-man's-switch.
    assert next_action("aaa", "aaa", None, dirty=True) == "dirty"


def test_next_action_dirty_tree_never_deploys():
    # Must NOT deploy from a dirty tree even when origin has advanced — dirty
    # takes precedence over every other outcome.
    assert next_action("aaa", "bbb", None, dirty=True) == "dirty"


def test_next_action_clean_tree_still_deploys():
    # Regression: a clean tree (the default) behaves exactly as before.
    assert next_action("aaa", "bbb", None, dirty=False) == "deploy"


# The deployer is pull-based and only ever fast-forwards: it must act ONLY when
# origin is strictly ahead of local. When the operator has committed locally but
# not pushed, origin is an *ancestor* of local (origin_ahead=False). The old code
# saw origin != local and returned "deploy", then diffed local..origin (the reverse
# of the un-pushed commits) and mis-fired a deploy + false rollback. Must be a no-op.
def test_next_action_noop_when_local_ahead_of_origin():
    assert next_action("localnew", "originold", None, origin_ahead=False) == "noop"


def test_next_action_deploy_requires_origin_ahead():
    # The normal pull path: origin strictly ahead (the default) still deploys.
    assert next_action("aaa", "bbb", None, origin_ahead=True) == "deploy"


def test_next_action_dirty_precedes_origin_ahead_check():
    # dirty still short-circuits even when origin isn't ahead.
    assert (
        next_action("localnew", "originold", None, dirty=True, origin_ahead=False)
        == "dirty"
    )


# is_diverged: local↔origin diverged (neither an ancestor of the other) → the deployer noops
# forever while origin's new commits never deploy; surfaced via GitOps Status (review L3).
def test_is_diverged_true_when_neither_is_ancestor():
    assert is_diverged("originX", "localY", origin_ahead=False, local_ahead=False)


def test_is_diverged_false_when_origin_ahead():
    # normal pull path — fast-forwardable, deploys.
    assert not is_diverged("originX", "localY", origin_ahead=True, local_ahead=False)


def test_is_diverged_false_when_local_ahead_unpushed():
    # committed-but-unpushed local commit is a plain noop (secret-rotate's domain), not divergence.
    assert not is_diverged("originX", "localY", origin_ahead=False, local_ahead=True)


def test_is_diverged_false_when_in_sync():
    assert not is_diverged("same", "same", origin_ahead=True, local_ahead=True)


# ── the dirty skip's journal line ───────────────────────────────────────────────────────────
#
# The skip itself is deliberate and healthy (operator mid-edit). What was missing is any trace
# of it in the journal between the two throttled Discord slots: on 2026-08-30 one untracked
# file parked the primary checkout 7 commits behind for ~40 minutes while `journalctl -t
# gitops-deploy` read `-- No entries --`, identical to "ticked, nothing to do".


def test_an_untracked_file_is_named_with_its_code():
    """The case that costs the diagnosis time.

    `git status --porcelain` counts untracked files, so the tree reads dirty with nothing
    modified. The `??` code is what tells the reader that, so it has to survive into the line.
    """
    summary = dirty_summary("?? .claude/staging-backfill.jsonl\n")
    assert summary == "?? .claude/staging-backfill.jsonl"


def test_a_modified_file_keeps_its_code_and_path():
    assert (
        dirty_summary(" M ansible/vars/secrets.yml\n") == "M ansible/vars/secrets.yml"
    )


def test_several_entries_are_joined():
    summary = dirty_summary("?? a.txt\n M b.txt\n")
    assert summary == "?? a.txt, M b.txt"


def test_a_rename_is_left_whole():
    """Both halves of `old -> new` are the fact; truncating to one would misreport it."""
    assert dirty_summary("R  old.py -> new.py\n") == "R old.py -> new.py"


def test_a_long_list_is_truncated_with_a_count():
    """A parked tree can be dirty by hundreds of files. One journal line, not hundreds."""
    summary = dirty_summary("".join(f"?? f{i}.txt\n" for i in range(30)), limit=12)
    assert summary.endswith(", +18 more")
    assert summary.count("??") == 12


def test_an_empty_status_does_not_render_an_empty_line():
    """`dirty` is decided from the same string, so empty here means the tree changed under us.

    Saying that is better than logging a bare trailing colon.
    """
    assert "no entries" in dirty_summary("")


def test_a_blank_trailing_line_is_not_an_entry():
    """`splitlines` on porcelain output yields a trailing empty string. It is not a path."""
    assert dirty_summary("?? a.txt\n\n") == "?? a.txt"


# behind_marker: the "host is parked on an old tree" signal. Its whole value is the timestamp —
# presence alone is normal (a push is behind for one tick), so these pin the clock semantics.


def test_behind_marker_cleared_when_caught_up():
    assert behind_marker(False, "originX", "originW 100.0", now=200.0) is None


def test_behind_marker_stamps_now_on_first_tick_behind():
    assert behind_marker(True, "originX", None, now=200.0) == "originX 200.0"


def test_behind_marker_keeps_first_seen_across_ticks():
    # Still behind 10 min later: the age must keep growing, not reset.
    assert behind_marker(True, "originX", "originX 200.0", now=800.0) == "originX 200.0"


def test_behind_marker_keeps_first_seen_when_origin_advances():
    # A new push while still stuck refreshes the SHA but must NOT restart the clock — otherwise a
    # steady trickle of pushes to a permanently-stuck host never trips the age threshold.
    assert behind_marker(True, "originZ", "originX 200.0", now=800.0) == "originZ 200.0"


def test_behind_marker_restamps_when_marker_unparseable():
    assert behind_marker(True, "originX", "garbage", now=200.0) == "originX 200.0"


# The dirty-tree alert fires on every 30-min tick by default, which spams the
# webhook through a long edit session. should_alert_dirty() throttles it to at
# most once per slot — a morning slot (08:00-19:59 CT) and an evening slot
# (>=20:00 CT) — and never before the morning hour, so an overnight-dirty tree
# pages once at ~8 AM and once at ~8 PM, not all night.
def test_dirty_alert_fires_first_tick_after_8am_when_never_alerted():
    # Overnight-dirty tree, first eligible morning tick, no prior alert today.
    now = datetime(2026, 6, 20, 8, 0, tzinfo=CT)
    assert should_alert_dirty(now, None) is True


def test_dirty_alert_suppressed_before_8am():
    # A pre-dawn tick must stay silent even if we've never alerted.
    now = datetime(2026, 6, 20, 7, 59, tzinfo=CT)
    assert should_alert_dirty(now, None) is False


def test_dirty_alert_suppressed_when_already_alerted_this_morning():
    # Second (and every later) morning tick after the morning alert.
    now = datetime(2026, 6, 20, 12, 30, tzinfo=CT)
    assert should_alert_dirty(now, "2026-06-20:am") is False


def test_dirty_alert_fires_in_evening_after_morning_alert():
    # Still dirty at night after the morning page -> the evening slot fires once.
    # 21:00, not 20:00: the boundary itself is test_dirty_alert_at_exactly_8pm_boundary_
    # inclusive's job, and using it here made the two tests the same case, so the evening
    # slot's interior was never covered by either.
    now = datetime(2026, 6, 20, 21, 0, tzinfo=CT)
    assert should_alert_dirty(now, "2026-06-20:am") is True


def test_dirty_alert_suppressed_when_already_alerted_this_evening():
    # A later evening tick after the evening alert -> no repeat.
    now = datetime(2026, 6, 20, 22, 15, tzinfo=CT)
    assert should_alert_dirty(now, "2026-06-20:pm") is False


def test_dirty_alert_fires_again_next_morning():
    # Still dirty the next morning after last night's page -> a fresh reminder.
    now = datetime(2026, 6, 21, 8, 15, tzinfo=CT)
    assert should_alert_dirty(now, "2026-06-20:pm") is True


def test_dirty_alert_at_exactly_8am_boundary_inclusive():
    now = datetime(2026, 6, 20, 8, 0, tzinfo=CT)
    assert should_alert_dirty(now, "2026-06-19:pm") is True


def test_dirty_alert_at_exactly_8pm_boundary_inclusive():
    now = datetime(2026, 6, 20, 20, 0, tzinfo=CT)
    assert should_alert_dirty(now, "2026-06-20:am") is True


def test_dirty_alert_predawn_tick_after_evening_alert_stays_quiet():
    # Dirty from 8 PM through 2 AM: the after-midnight tick is before the morning
    # slot, so it must not re-page even though the date has rolled over.
    now = datetime(2026, 6, 21, 2, 0, tzinfo=CT)
    assert should_alert_dirty(now, "2026-06-20:pm") is False


def test_dirty_alert_newly_dirtied_after_8am_alerts_once():
    # Tree goes dirty mid-afternoon with no alert recorded today -> one alert now.
    now = datetime(2026, 6, 20, 15, 0, tzinfo=CT)
    assert should_alert_dirty(now, None) is True


def test_dirty_alert_custom_hours():
    # Custom morning/evening hours push both slot boundaries.
    assert (
        should_alert_dirty(datetime(2026, 6, 20, 8, 0, tzinfo=CT), None, 9, 21) is False
    )
    assert (
        should_alert_dirty(datetime(2026, 6, 20, 9, 0, tzinfo=CT), None, 9, 21) is True
    )
    assert (
        should_alert_dirty(
            datetime(2026, 6, 20, 20, 0, tzinfo=CT), "2026-06-20:am", 9, 21
        )
        is False
    )
    assert (
        should_alert_dirty(
            datetime(2026, 6, 20, 21, 0, tzinfo=CT), "2026-06-20:am", 9, 21
        )
        is True
    )


def test_dirty_alert_slot_keys():
    assert dirty_alert_slot(datetime(2026, 6, 20, 7, 59, tzinfo=CT)) is None
    assert dirty_alert_slot(datetime(2026, 6, 20, 8, 0, tzinfo=CT)) == "2026-06-20:am"
    assert dirty_alert_slot(datetime(2026, 6, 20, 19, 59, tzinfo=CT)) == "2026-06-20:am"
    assert dirty_alert_slot(datetime(2026, 6, 20, 20, 0, tzinfo=CT)) == "2026-06-20:pm"
    assert dirty_alert_slot(datetime(2026, 6, 20, 23, 30, tzinfo=CT)) == "2026-06-20:pm"
