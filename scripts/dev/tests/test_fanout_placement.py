"""Placement is a pure function over (host, cap, current, live_agents) — spec §2.

Run: uv run pytest scripts/dev/tests/test_fanout_placement.py
"""

import pytest

from _fanout_fakes import HOST_KEY, fake_tools, ok
from fanout_lib.placement import (
    READ_COMMAND,
    RESERVATION_BYTES,
    HostReading,
    NoHeadroom,
    headroom,
    parse_reading,
    place,
)
from fanout_lib.transport import HOST_READ_COMMAND, read_host

GIB = 1024**3


def _r(host, cap, current, agents=0):
    return HostReading(host, cap, current, agents)


def test_parse_reading_is_clean_on_the_four_line_shape():
    r = parse_reading("daniel-box", f"5754224640\n12884901888\n4\n{HOST_KEY}\n")
    assert r == HostReading("daniel-box", 12884901888, 5754224640, 4, HOST_KEY)


def test_parse_reading_reads_max_as_no_cap():
    assert (
        parse_reading("daniel-server", f"1200918528\nmax\n1\n{HOST_KEY}\n").cap_bytes
        is None
    )


def test_parse_reading_is_flagged_on_a_read_that_lost_its_signing_key_line():
    """A host whose key read printed nothing must not parse: the gate fails closed."""
    with pytest.raises(ValueError):
        parse_reading("daniel-box", "5754224640\n12884901888\n4\n")


def test_parse_reading_never_puts_the_read_output_in_its_message():
    """user.signingkey can name a PRIVATE key file, so the output stays out of the error."""
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----"
    with pytest.raises(ValueError) as err:
        parse_reading("daniel-box", f"5754224640\n12884901888\n4\n{secret}\nmore\n")
    assert secret not in str(err.value)


def test_parse_reading_is_flagged_on_a_short_or_non_numeric_read():
    with pytest.raises(ValueError):
        parse_reading("daniel-box", "5754224640\n")
    with pytest.raises(ValueError):
        parse_reading("daniel-box", f"lots\n12884901888\n4\n{HOST_KEY}\n")


def test_an_uncapped_host_has_no_headroom():
    """A host without the role's drop-in is not a candidate: nothing bounds the agent there."""
    assert headroom(_r("daniel-server", None, 1 * GIB)) is None


def test_headroom_subtracts_current_and_one_reservation():
    assert headroom(_r("h", 12 * GIB, 5 * GIB)) == 7 * GIB - RESERVATION_BYTES


def test_places_on_emptier_host():
    readings = [
        _r("daniel-box", 12 * GIB, 9 * GIB),
        _r("daniel-server", 10 * GIB, 2 * GIB),
    ]
    assert place(["a"], readings) == [("a", "daniel-server")]


def test_refuses_when_neither_has_headroom():
    readings = [
        _r("daniel-box", 12 * GIB, 11 * GIB),
        _r("daniel-server", 10 * GIB, 9 * GIB),
    ]
    with pytest.raises(NoHeadroom) as exc:
        place(["a"], readings)
    assert "daniel-box" in str(exc.value) and "daniel-server" in str(exc.value)


def test_the_reservation_is_subtracted_before_the_next_batch():
    """Six batches do not pile onto one host: each placement costs that host a reservation."""
    readings = [
        _r("daniel-box", 12 * GIB, 2 * GIB),
        _r("daniel-server", 10 * GIB, 2 * GIB),
    ]
    placed = place(["a", "b", "c", "d"], readings)
    hosts = [h for _, h in placed]
    assert hosts.count("daniel-box") == 2 and hosts.count("daniel-server") == 2


def test_a_pin_wins_while_it_has_headroom_and_refuses_when_it_does_not():
    readings = [
        _r("daniel-box", 12 * GIB, 9 * GIB),
        _r("daniel-server", 10 * GIB, 1 * GIB),
    ]
    assert place(["a"], readings, pin="daniel-box") == [("a", "daniel-box")]
    with pytest.raises(NoHeadroom):
        place(["a"], [_r("daniel-box", 12 * GIB, 11 * GIB)], pin="daniel-box")


def test_the_read_command_is_one_read_only_string():
    for command in (READ_COMMAND, HOST_READ_COMMAND):
        assert "\n" not in command
        for verb in ("rm", "systemctl", ">", "sudo", "kill"):
            assert verb not in command


def test_read_host_goes_over_ssh_only_for_the_other_host():
    tools, run = fake_tools({"daniel-server": ok(f"1\n10737418240\n0\n{HOST_KEY}\n")})
    assert read_host(tools, "daniel-server") == HostReading(
        "daniel-server", 10737418240, 1, 0, HOST_KEY
    )
    # One call, not two: the signing-key read rides the headroom read's connection.
    assert run.calls == [("daniel-server", HOST_READ_COMMAND, None)]


def test_read_host_reports_an_unreachable_host_as_a_string():
    tools, _ = fake_tools({})
    err = read_host(tools, "daniel-server")
    assert isinstance(err, str) and "refused" in err
