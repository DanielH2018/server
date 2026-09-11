"""`probe.py alerts`: reading one log line, and collapsing many into episodes.

Split from test_probe_alerts.py, which passed the 500-line cap. Everything here is pure — a
line in, a parse or an episode list out. The fetch path (which stream is queried, what `--check`
and `--pi` admit, which end a hit `--limit` cuts) stays in test_probe_alerts.py.

Two streams are parsed, because monitor-bridge polls no Kuma state and so says nothing about the
host crons that push Kuma directly. Reading only its log left the backup plane's sole DOWN signal
unrecorded: measured 2026-08-22, 465 `longhorn-backup-health: status=down` lines over 7 days
appeared in no episode list.
"""

from datetime import UTC, datetime

from diagnostics.probe_lib import alerts
from diagnostics.probe_lib.alerts import alert_episodes


SYSLOG_DOWN = (
    "2026-08-19T13:50:03.382504+00:00 daniel-box longhorn-backup-health: "
    "status=down backed-up volumes stale or missing: homelab/tdarr-server (weekly-d1)"
)
SYSLOG_PUSH_FAILED = (
    "2026-08-16T07:40:04.188815+00:00 daniel-box claude-otel-health: "
    "push failed (status=down: loki 0/1 ready; prometheus not answering queries)"
)
SYSLOG_PUSH_FAILED_TRUNCATED = (
    "2026-08-16T10:36:02.000000+00:00 daniel-box longhorn-backup-health: "
    "push failed (status=down: backups in Error state: backup-4a471c15 backup-9818b9cc"
)


def test_parse_down_line_extracts_name_and_strips_cycle_counter():
    line = "[2026-07-21T08:37:00] DOWN n8n - 1 active workflow(s) failed (2 cycles)"
    assert alerts.parse_down_line(line) == ("n8n", "1 active workflow(s) failed")


def test_parse_down_line_ignores_ok_and_malformed_lines():
    assert alerts.parse_down_line("[2026-07-21T08:37:00] OK   n8n - fine") is None
    assert alerts.parse_down_line("not a monitor-bridge line") is None
    # Making the stamp optional must not make DOWN itself optional, nor let it match mid-line.
    assert alerts.parse_down_line("n8n went DOWN n8n - later") is None


def test_alert_episodes_splits_on_a_silence_gap():
    minute = int(60 * 1e9)
    rows = [
        (0, "backup", "shrank"),
        (5 * minute, "backup", "shrank"),  # same episode (5m <= 30m gap)
        (60 * minute, "backup", "shrank again"),  # new episode (55m gap)
    ]
    eps = alerts.alert_episodes(rows, gap_s=1800)
    assert len(eps) == 2
    # newest episode first; its latest msg wins
    assert eps[0]["cycles"] == 1 and eps[0]["msg"] == "shrank again"
    assert eps[1]["cycles"] == 2 and eps[1]["first_ns"] == 0


def test_alert_episodes_keeps_distinct_checks_separate():
    rows = [(0, "backup", "a"), (0, "cpu", "b")]
    eps = alerts.alert_episodes(rows, gap_s=1800)
    assert {e["name"] for e in eps} == {"backup", "cpu"}


def test_format_alert_episodes_empty_is_all_clear():
    assert alerts.format_alert_episodes([], 7) == "no DOWN alerts in the last 7d"


def test_format_alert_episodes_renders_name_and_msg():
    eps = [{"name": "n8n", "first_ns": 0, "last_ns": 0, "cycles": 1, "msg": "boom"}]
    out = alerts.format_alert_episodes(eps, 7)
    assert "1 DOWN episode(s)" in out and "n8n" in out and "boom" in out


def test_parse_syslog_down_line_reads_the_tag_and_the_message():
    # The rsyslog prefix ("<iso-ts> <host> ") is real and the bare "<tag>: status=down <msg>"
    # shape a reading of the cron scripts suggests never reaches Loki.
    assert alerts.parse_syslog_down_line(SYSLOG_DOWN) == (
        "longhorn-backup-health",
        "backed-up volumes stale or missing: homelab/tdarr-server (weekly-d1)",
    )


def test_parse_syslog_down_line_unwraps_a_failed_push():
    # A failed push is the case where syslog is the ONLY record — Kuma never learned — so the
    # prefix stays in the message rather than being discarded.
    name, msg = alerts.parse_syslog_down_line(SYSLOG_PUSH_FAILED)
    assert name == "claude-otel-health"
    assert msg == "push failed: loki 0/1 ready; prometheus not answering queries"


def test_parse_syslog_down_line_survives_rsyslog_truncation():
    name, msg = alerts.parse_syslog_down_line(SYSLOG_PUSH_FAILED_TRUNCATED)
    assert name == "longhorn-backup-health"
    assert msg.startswith("push failed: backups in Error state:")


def test_parse_syslog_down_line_ignores_up_and_unrelated_lines():
    assert alerts.parse_syslog_down_line("not a syslog line") is None
    assert (
        alerts.parse_syslog_down_line(
            "2026-08-20T12:40:03+00:00 daniel-box disk-health: status=up / at 22%"
        )
        is None
    )


#
# Episode splitting and timestamp rendering. Both halves of the 2026-09-04 misdating (#1104):
# a `*/30` cron's ticks landed exactly on a fixed 30-minute splitting gap, so one 13.5-hour
# outage rendered as 16 episodes and the newest-first list put a mid-incident fragment on top;
# and every row was stamped America/Chicago with no marker, five hours off the `journalctl
# --utc` output beside it. Each rule below is an accept/reject pair — a splitter that merges
# everything and one that merges nothing are indistinguishable from the passing side alone.

_MIN_NS = int(60 * 1e9)


def _cron_run(period_min, count, name="release-staleness-check", start=0, jitter=1):
    """One check's DOWN samples at a fixed period, with a second of cron jitter each tick."""
    return [
        (start + i * period_min * _MIN_NS + i * jitter * int(1e9), name, "stale")
        for i in range(count)
    ]


def test_alert_episodes_merges_a_run_of_ticks_at_the_checks_own_period():
    # The accept half: 28 consecutive `*/30` ticks are ONE outage. A fixed 30-minute gap made
    # this 16 episodes, because 1800s of jitter-free period is not < 1800s.
    eps = alert_episodes(_cron_run(30, 28))
    assert len(eps) == 1
    assert eps[0]["cycles"] == 28
    assert eps[0]["first_ns"] == 0


def test_alert_episodes_splits_when_the_check_recovered_between_runs():
    # The reject half: the same cadence with one UP tick in the middle (a 60-minute silence)
    # is two outages, and an adaptive gap must still say so.
    rows = _cron_run(30, 4) + _cron_run(30, 4, start=5 * 30 * _MIN_NS)
    eps = alert_episodes(rows)
    assert len(eps) == 2


def test_episode_gap_s_derives_the_threshold_from_the_sample_cadence():
    half_hourly = [i * 30 * _MIN_NS for i in range(6)]
    assert alerts.episode_gap_s(half_hourly) == 30 * 60 * 1.5


def test_episode_gap_s_clamps_a_sparse_check_to_the_ceiling():
    # A check that fires once a day has a median delta of a day. Unclamped, that would
    # swallow a week of separate incidents into one episode.
    daily = [i * 24 * 60 * _MIN_NS for i in range(4)]
    assert alerts.episode_gap_s(daily) == alerts._GAP_CEILING_S
    assert len(alert_episodes([(ns, "backup", "gone") for ns in daily])) == 4


def test_episode_gap_s_floors_a_burst_of_near_simultaneous_samples():
    assert alerts.episode_gap_s([0, int(1e9)]) == alerts._GAP_FLOOR_S


def test_episode_gap_s_honours_an_explicit_gap_over_the_cadence():
    assert (
        alerts.episode_gap_s([i * 30 * _MIN_NS for i in range(6)], gap_s=1800) == 1800
    )


def test_format_alert_episodes_stamps_utc_and_carries_the_episode_end():
    # 2026-09-04 00:30 -> 14:00 UTC is the real release-staleness-check outage from #1104,
    # which the Chicago-stamped view rendered as 2026-09-03 19:30.
    first = int(datetime(2026, 9, 4, 0, 30, tzinfo=UTC).timestamp() * 1e9)
    last = int(datetime(2026, 9, 4, 14, 0, tzinfo=UTC).timestamp() * 1e9)
    out = alerts.format_alert_episodes(
        [
            {
                "name": "release-staleness-check",
                "first_ns": first,
                "last_ns": last,
                "cycles": 28,
                "msg": "registry: changed since applied",
            }
        ],
        2,
    )
    assert "times UTC" in out
    assert "2026-09-04 00:30 -> 14:00" in out
    assert "2026-09-03" not in out


def test_format_alert_episodes_keeps_the_date_on_an_end_in_another_day():
    first = int(datetime(2026, 9, 3, 23, 30, tzinfo=UTC).timestamp() * 1e9)
    last = int(datetime(2026, 9, 4, 0, 30, tzinfo=UTC).timestamp() * 1e9)
    out = alerts.format_alert_episodes(
        [
            {
                "name": "backup",
                "first_ns": first,
                "last_ns": last,
                "cycles": 2,
                "msg": "x",
            }
        ],
        2,
    )
    assert "2026-09-03 23:30 -> 2026-09-04 00:30" in out
