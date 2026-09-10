"""Placement is a pure function over (host, cap, current, live_agents) — spec §2.

Run: uv run pytest scripts/dev/tests/test_fanout_placement.py
"""

import pytest

from _fanout_fakes import fake_tools, ok
from fanout_lib.placement import (
    READ_COMMAND,
    RESERVATION_BYTES,
    HostReading,
    NoHeadroom,
    headroom,
    parse_reading,
    place,
)
from fanout_lib.transport import read_host

GIB = 1024**3


def _r(host, cap, current, agents=0, plane_cap=None, plane_current=0):
    """A reading whose login plane does not bound it unless a test says so.

    `plane_cap=None` keeps every fleet-only case measuring the fleet arithmetic it was
    written for; the cases below that mean to exercise the plane pass it explicitly.
    """
    return HostReading(host, cap, current, plane_cap, plane_current, agents)


def test_parse_reading_is_clean_on_the_five_line_shape():
    r = parse_reading(
        "daniel-box", "5754224640\n12884901888\n4952506368\n8589934592\n4\n"
    )
    assert r == HostReading(
        "daniel-box", 12884901888, 5754224640, 8589934592, 4952506368, 4
    )


def test_parse_reading_reads_max_as_no_cap_on_either_side():
    r = parse_reading("daniel-server", "1200918528\nmax\n952242176\nmax\n1\n")
    assert r.cap_bytes is None and r.plane_cap_bytes is None


def test_parse_reading_is_flagged_on_a_short_or_non_numeric_read():
    # The three-line shape the older read produced is a parse error now, not a reading with
    # the plane half missing.
    with pytest.raises(ValueError):
        parse_reading("daniel-box", "5754224640\n12884901888\n4\n")
    with pytest.raises(ValueError):
        parse_reading("daniel-box", "5754224640\n")
    with pytest.raises(ValueError):
        parse_reading("daniel-box", "lots\n12884901888\n4952506368\n8589934592\n4\n")
    with pytest.raises(ValueError):
        parse_reading("daniel-box", "5754224640\n12884901888\n4952506368\nlots\n4\n")


def test_an_uncapped_host_has_no_headroom():
    """A host without the role's drop-in is not a candidate: nothing bounds the agent there."""
    assert headroom(_r("daniel-server", None, 1 * GIB)) is None


def test_a_host_capped_on_only_one_side_is_bounded_by_that_side():
    """`max` on one cgroup does not make the host uncapped — the other still throttles."""
    plane_only = _r("h", None, 20 * GIB, plane_cap=8 * GIB, plane_current=1 * GIB)
    assert headroom(plane_only) == 7 * GIB - RESERVATION_BYTES


def test_headroom_subtracts_current_and_one_reservation():
    assert headroom(_r("h", 12 * GIB, 5 * GIB)) == 7 * GIB - RESERVATION_BYTES


def test_headroom_is_the_smaller_of_the_fleet_and_login_plane_headrooms():
    """The plane binds first at the real caps: 12G fleet over an 8G plane."""
    plane_tighter = _r("h", 12 * GIB, 5 * GIB, plane_cap=8 * GIB, plane_current=6 * GIB)
    assert headroom(plane_tighter) == 2 * GIB - RESERVATION_BYTES
    fleet_tighter = _r(
        "h", 12 * GIB, 11 * GIB, plane_cap=8 * GIB, plane_current=1 * GIB
    )
    assert headroom(fleet_tighter) == 1 * GIB - RESERVATION_BYTES


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


def test_a_full_login_plane_refuses_a_batch_the_fleet_cap_would_allow():
    """The 2026-09-10 case: 12G/10G fleet with room, 8G plane full on both hosts."""
    readings = [
        _r("daniel-box", 12 * GIB, 2 * GIB, plane_cap=8 * GIB, plane_current=8 * GIB),
        _r(
            "daniel-server", 10 * GIB, 1 * GIB, plane_cap=8 * GIB, plane_current=8 * GIB
        ),
    ]
    with pytest.raises(NoHeadroom):
        place(["a"], readings)


def test_the_refusal_names_which_cap_the_number_came_from():
    """A min() over two caps is unreadable without it: the fleet here looks 9 GiB free."""
    readings = [
        _r("daniel-box", 12 * GIB, 2 * GIB, plane_cap=8 * GIB, plane_current=8 * GIB)
    ]
    with pytest.raises(NoHeadroom) as exc:
        place(["a"], readings)
    assert "under its login-plane cap" in str(exc.value)

    fleet_bound = [
        _r("daniel-box", 12 * GIB, 12 * GIB, plane_cap=8 * GIB, plane_current=1 * GIB)
    ]
    with pytest.raises(NoHeadroom) as exc:
        place(["a"], fleet_bound)
    assert "under its fleet cap" in str(exc.value)


def test_a_full_fleet_refuses_a_batch_the_login_plane_would_allow():
    readings = [
        _r("daniel-box", 12 * GIB, 12 * GIB, plane_cap=8 * GIB, plane_current=1 * GIB),
        _r(
            "daniel-server",
            10 * GIB,
            10 * GIB,
            plane_cap=8 * GIB,
            plane_current=1 * GIB,
        ),
    ]
    with pytest.raises(NoHeadroom):
        place(["a"], readings)


def test_a_batch_is_placed_when_both_the_fleet_and_the_plane_have_room():
    readings = [
        _r("daniel-box", 12 * GIB, 9 * GIB, plane_cap=8 * GIB, plane_current=5 * GIB),
        _r(
            "daniel-server", 10 * GIB, 2 * GIB, plane_cap=8 * GIB, plane_current=1 * GIB
        ),
    ]
    assert place(["a"], readings) == [("a", "daniel-server")]


def test_the_read_command_is_one_read_only_string():
    assert "\n" not in READ_COMMAND
    for verb in ("rm", "systemctl", ">", "sudo", "kill"):
        assert verb not in READ_COMMAND


def test_the_read_command_reads_both_cgroups_an_agent_lives_in():
    """The login plane is where a transient user service actually lands."""
    assert "/sys/fs/cgroup/user.slice/memory.high" in READ_COMMAND
    assert "/sys/fs/cgroup/user.slice/user-1000.slice/memory.high" in READ_COMMAND
    assert "/sys/fs/cgroup/user.slice/user-1000.slice/memory.current" in READ_COMMAND


def test_read_host_goes_over_ssh_only_for_the_other_host():
    tools, run = fake_tools({"daniel-server": ok("1\n10737418240\n2\n8589934592\n0\n")})
    assert read_host(tools, "daniel-server") == HostReading(
        "daniel-server", 10737418240, 1, 8589934592, 2, 0
    )
    assert run.calls == [("daniel-server", READ_COMMAND, None)]


def test_read_host_reports_an_unreachable_host_as_a_string():
    tools, _ = fake_tools({})
    err = read_host(tools, "daniel-server")
    assert isinstance(err, str) and "refused" in err
