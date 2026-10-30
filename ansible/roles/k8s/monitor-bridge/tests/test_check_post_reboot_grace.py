"""The arm that stops the weekly reboot holding a tile red for hours.

After the Sunday 07:30 restart, `swallowed_verdicts` stays red long after every other tile
recovers. It reads a window that still contains the reboot, and it is keyed here on the NODE's
uptime rather than this pod's age: a deploy restarts the bridge without rebooting anything, and
the arm suppresses a fault only a reboot produces.

Each behaviour gets an accept/reject pair: what the arm holds, and what it must still report
while holding it. The second half is the one that matters. An arm that suppressed the whole
check would be hours of weekly blindness on lost verdicts, and it would read green the whole
time.
"""

import dataclasses

import bridge.common
import checks.logs
from _fake_sources import FakeSources


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


# --- swallowed_verdicts: never read back past the reboot ----------------------------------


def _window_seen(cfg, uptime, lines=()):
    """The lookback `check_swallowed_verdicts` asks for, at a stated node uptime.

    Both the clock and the fetch go in as arguments; the check takes `uptime_s` and `src` so a
    test states them.
    """
    windows = []

    def _fetch(query, window_s, _limit):
        windows.append(window_s)
        return list(lines) if "syslog" in query else []

    ok, msg = checks.logs.check_swallowed_verdicts(
        cfg, FakeSources(loki_lines=_fetch), uptime_s=lambda: uptime
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
    # The operator switch. BOOT_SETTLE_S=0 disables the hold exactly.
    _, _, windows = _window_seen(dataclasses.replace(cfg, BOOT_SETTLE_S=0), uptime=60.0)
    assert windows == [60, 60]
