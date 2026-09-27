"""The two arms that stop the weekly reboot holding a tile red for hours, issue #2783.

The Sunday 07:30 restart left `shipper_dropped` red for 4.4h and `swallowed_verdicts` for 2.9h on
2026-09-27, long after every other tile recovered. Both read a window that still contained the
reboot, and both are keyed here on the NODE's uptime rather than this pod's age — a deploy
restarts the bridge without rebooting anything, and each arm suppresses a fault only a reboot
produces.

Each behaviour gets an accept/reject pair: what the arm holds, and what it must still report
while holding it. The second half is the one that matters. An arm that suppressed the whole
check would be six hours of weekly blindness on partial log loss, and it would read green the
whole time.
"""

import dataclasses

import bridge.common
import checks.logs
from verdicts.logs import TOO_FAR_BEHIND, shipper_dropped


def test_host_uptime_reads_the_first_field(tmp_path):
    f = tmp_path / "uptime"
    f.write_text("123456.78 987654.32\n")
    assert bridge.common.host_uptime_s(str(f)) == 123456.78


def test_an_unreadable_uptime_reads_as_no_grace(tmp_path):
    # Fails toward evaluating: a grace that cannot read the clock must not suppress forever.
    assert bridge.common.host_uptime_s(str(tmp_path / "absent")) is None
    bad = tmp_path / "bad"
    bad.write_text("not a number\n")
    assert bridge.common.host_uptime_s(str(bad)) is None


# --- shipper_dropped: the `too_far_behind` backlog reason ---------------------------------


def test_a_post_reboot_backlog_discard_is_held():
    ok, msg = shipper_dropped(
        client_count=0.0,
        server_reasons=[(TOO_FAR_BEHIND, 161573.0)],
        window="1h",
        threshold=3000.0,
        backlog_grace_active=True,
    )
    assert ok
    assert TOO_FAR_BEHIND in msg
    assert "still catching up" in msg


def test_the_same_discard_pages_outside_the_grace():
    ok, msg = shipper_dropped(
        client_count=0.0,
        server_reasons=[(TOO_FAR_BEHIND, 161573.0)],
        window="1h",
        threshold=3000.0,
    )
    assert not ok
    assert TOO_FAR_BEHIND in msg


def test_another_server_reason_still_pages_inside_the_grace():
    # The reject half that decides whether this arm is worth having: rate_limited is a throughput
    # fault a reboot does not cause, so it must survive the window that holds the backlog.
    ok, msg = shipper_dropped(
        client_count=0.0,
        server_reasons=[(TOO_FAR_BEHIND, 161573.0), ("rate_limited", 9000.0)],
        window="1h",
        threshold=3000.0,
        backlog_grace_active=True,
    )
    assert not ok
    assert "rate_limited" in msg


def test_the_client_side_counter_still_pages_inside_the_grace():
    ok, _ = shipper_dropped(
        client_count=9000.0,
        server_reasons=[(TOO_FAR_BEHIND, 161573.0)],
        window="1h",
        threshold=3000.0,
        backlog_grace_active=True,
    )
    assert not ok


def test_the_grace_says_nothing_when_there_is_nothing_to_hold():
    _, msg = shipper_dropped(
        client_count=0.0,
        server_reasons=[],
        window="1h",
        threshold=3000.0,
        backlog_grace_active=True,
    )
    assert TOO_FAR_BEHIND not in msg


def _grace_seen(cfg, uptime):
    """Whether `check_shipper_dropped` armed the backlog grace at a stated node uptime.

    Read off the verdict rather than by patching it: a 5000-entry `too_far_behind` burst is over
    `SHIPPER_DROPPED_MAX` (3000), so the check is `up` only while the grace drops that reason.
    Every boundary goes in as an argument — the check takes `uptime_s`, `prom_scalar` and
    `prom_vector` for the same reason `with_pi_ports` takes `tcp_open`.
    """
    return checks.logs.check_shipper_dropped(
        cfg,
        uptime_s=lambda: uptime,
        prom_scalar=lambda *a, **k: 0.0,
        prom_vector=lambda *a, **k: [({"reason": TOO_FAR_BEHIND}, 5000.0)],
    )


def test_check_shipper_dropped_arms_the_grace_from_the_node_uptime(cfg):
    ok, msg = _grace_seen(cfg, 600.0)
    assert ok
    assert "still catching up" in msg


def test_check_shipper_dropped_pages_once_the_grace_has_passed(cfg):
    ok, _ = _grace_seen(cfg, cfg.SHIPPER_BACKLOG_GRACE_S + 1.0)
    assert not ok


def test_an_unreadable_clock_does_not_arm_the_backlog_grace(cfg):
    ok, _ = _grace_seen(cfg, None)
    assert not ok


# --- swallowed_verdicts: never read back past the reboot ----------------------------------


def _window_seen(cfg, uptime, lines=()):
    """The lookback `check_swallowed_verdicts` asks for, at a stated node uptime.

    Both the clock and the fetch go in as arguments; the check takes `uptime_s` and `fetch` so a
    test states them.
    """
    windows = []

    def _fetch(_cfg, query, window_s, _limit):
        windows.append(window_s)
        return list(lines) if "syslog" in query else []

    ok, msg = checks.logs.check_swallowed_verdicts(
        cfg, uptime_s=lambda: uptime, fetch=_fetch
    )
    return ok, msg, windows


def test_inside_the_settle_window_the_cycle_is_skipped(cfg):
    ok, msg, windows = _window_seen(cfg, uptime=300.0)
    assert ok
    assert "inside BOOT_SETTLE_S" in msg
    assert windows == [], "nothing should be fetched when no window is left to read"


def test_just_after_the_settle_window_only_the_new_lines_are_read(cfg):
    ok, _, windows = _window_seen(cfg, uptime=cfg.BOOT_SETTLE_S + 900)
    assert ok
    assert windows == [900, 900], (
        "both selectors read back only to the end of the settle window"
    )


def test_the_full_window_returns_once_the_reboot_is_out_of_range(cfg):
    uptime = cfg.BOOT_SETTLE_S + cfg.SWALLOWED_VERDICTS_WINDOW_S + 1
    _, _, windows = _window_seen(cfg, uptime=uptime)
    assert windows == [cfg.SWALLOWED_VERDICTS_WINDOW_S] * 2


def test_an_unreadable_clock_leaves_the_full_window(cfg):
    _, _, windows = _window_seen(cfg, uptime=None)
    assert windows == [cfg.SWALLOWED_VERDICTS_WINDOW_S] * 2


def test_a_shortened_window_still_pages_on_a_verdict_lost_after_the_reboot(cfg):
    # The reject half: shortening the lookback must not make the check inert. A push lost after
    # the settle window is inside the shortened range and still pages.
    header = "2026-09-27T08:10:00.000000+00:00 daniel-box "
    lines = [
        (
            1,
            header
            + "release-staleness-check: push failed (http=500 rc=0) (status=down: x)",
        ),
        (2, header + "longhorn-backup-health: status=up 12 backups"),
        (3, header + "setup-drift-check: status=up clean"),
    ]
    ok, msg, _ = _window_seen(cfg, uptime=cfg.BOOT_SETTLE_S + 900, lines=lines)
    assert not ok
    assert "release-staleness-check" in msg


def test_a_zero_settle_window_disables_the_skip(cfg):
    # The operator switch. BOOT_SETTLE_S=0 restores the pre-#2783 reading exactly.
    _, _, windows = _window_seen(dataclasses.replace(cfg, BOOT_SETTLE_S=0), uptime=60.0)
    assert windows == [60, 60]
