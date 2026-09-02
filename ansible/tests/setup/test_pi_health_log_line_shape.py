#!/usr/bin/env python3
"""What the Pi health crons WRITE is what probe_alerts.py can READ.

Two halves had to land together for a daniel-pi DOWN to become an episode: the crons had to
emit a `status=` line at all (kuma-push-lib only calls `logger` when the PUSH fails), and the
Pi had to ship it (no rsyslog, and that promtail build is a journal stub -- so it goes to a
file the Pi's promtail tails under `job="syslog"`). This pins the join between them.

The join is a FORMAT, which is the part that rots silently. `_SYSLOG_LINE_RE` wants exactly
two whitespace-free tokens before the tag, because that is rsyslog's own prefix on the server
hosts. `date -Is` is one token and matches; traditional syslog format ("Aug 29 19:12:26 host")
is four and parses as nothing. Either side can be edited alone by someone who cannot see the
other, and the failure is silent -- every Pi episode simply stops being reconstructed, which
is indistinguishable from the Pi being healthy.

So the assertion runs the REAL rendered scripts and feeds their actual output through the
REAL parser. Nothing here matches on source text.

Run: uv run pytest ansible/tests/setup/test_pi_health_log_line_shape.py
"""

import sys

import pytest
from _helpers import REPO
from _pi_health import run

sys.path.insert(0, str(REPO / "scripts" / "diagnostics"))
from probe_alerts import parse_syslog_down_line  # noqa: E402


BOTH = ["autoheal", "docker-proxy"]


def _only_line(lines):
    assert len(lines) == 1, f"expected exactly one health line, got {lines!r}"
    return lines[0]


def test_a_recovery_down_line_parses_into_an_episode(tmp_path):
    """ACCEPT: the emitted DOWN line is what the alert reconstruction consumes."""
    _, msg, _, lines = run("pi-recovery-health", tmp_path, running=["docker-proxy"])

    parsed = parse_syslog_down_line(_only_line(lines))

    assert parsed is not None, (
        f"probe_alerts could not parse {_only_line(lines)!r} -- the Pi emits a shape the "
        "reconstruction does not read, so its outages stay invisible"
    )
    tag, parsed_msg = parsed
    assert tag == "pi-recovery-health"
    assert parsed_msg == msg, (
        f"parsed {parsed_msg!r} but the cron pushed {msg!r} to Kuma"
    )


def test_an_sd_health_down_line_parses_into_an_episode(tmp_path):
    """ACCEPT: the second cron, under its own tag -- `--check` filters on it."""
    _, msg, _, lines = run("pi-sd-health", tmp_path, counter="7")

    parsed = parse_syslog_down_line(_only_line(lines))

    assert parsed is not None, f"could not parse {_only_line(lines)!r}"
    tag, parsed_msg = parsed
    assert tag == "pi-sd-health"
    assert parsed_msg == msg


def test_a_failed_push_still_parses(tmp_path):
    """ACCEPT: when the push fails, this file is the ONLY record of the DOWN."""
    _, msg, _, lines = run(
        "pi-recovery-health", tmp_path, running=["docker-proxy"], push_ok="0"
    )

    line = _only_line(lines)
    assert "push failed" in line, (
        f"a failed push must be recorded as such -- got {line!r}"
    )

    parsed = parse_syslog_down_line(line)
    assert parsed is not None, f"could not parse the push-failed form: {line!r}"
    tag, parsed_msg = parsed
    assert tag == "pi-recovery-health"
    assert msg in parsed_msg


@pytest.mark.parametrize(
    ("script", "kwargs"),
    [("pi-recovery-health", {"running": BOTH}), ("pi-sd-health", {"counter": "0"})],
)
def test_a_healthy_cycle_emits_a_line_that_is_not_an_episode(tmp_path, script, kwargs):
    """REJECT: `up` lines ship as a heartbeat, and must NOT become DOWN episodes.

    They are what distinguishes "nothing is wrong" from "nothing is shipping", so they have
    to be present in the file and absent from the alert history.
    """
    status, _, _, lines = run(script, tmp_path, **kwargs)

    assert status == "up"
    line = _only_line(lines)
    assert "status=up" in line, (
        f"no heartbeat line emitted on a healthy cycle: {line!r}"
    )
    assert parse_syslog_down_line(line) is None, (
        f"{line!r} was read as a DOWN episode -- every healthy 5-minute cycle would "
        "manufacture one"
    )


def test_the_timestamp_format_is_the_load_bearing_half(tmp_path):
    """REJECT: the same content under traditional syslog's 4-token prefix parses as nothing.

    This is the regression the pairing exists to catch, so it is asserted directly rather
    than trusted: it proves the parser really does discriminate on the prefix, and that the
    ACCEPT cases above are not passing for some unrelated reason.
    """
    _, _, _, lines = run("pi-recovery-health", tmp_path, running=["docker-proxy"])
    line = _only_line(lines)
    _, rest = line.split(" ", 1)

    assert parse_syslog_down_line(f"Aug 29 19:12:26 {rest}") is None
    assert parse_syslog_down_line(line) is not None
