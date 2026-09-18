"""Guard: the Pi Alloy's GOMEMLIMIT sits above the heap GOGC asks for, below the cap.

GOMEMLIMIT bounds the Go runtime's TOTAL memory (heap, stacks, GC metadata), not the live
heap. A limit below what GOGC would grow the heap to takes over the heap goal, and a limit
near the live set itself makes the runtime collect continuously, bounded only by the GC CPU
limiter at 50% of GOMAXPROCS. That is how the first Alloy on daniel-pi ran for 7 days:
GOMEMLIMIT=48MiB against a heap that needed more, 2 GC cycles a second for 1.6 log lines a
second, 0.28 of a core steady (#932), the largest load on the host and the contention
behind #930.

The floor here is the heap goal GOGC sets on the measured live heap, plus the runtime's
non-heap classes. It is NOT `go_memstats_sys_bytes`, which the 2026-09-03 sizing used: that
metric never shrinks, counts pages already handed back to the kernel, and read 96.3 MB on
2026-09-17 — above the limit and the cap alike — while collection ran at 0.7 cycles a
minute (#1944). Lowering the limit back under the floor "to save RAM" is a one-line edit
that renders, lints and deploys green, and reads as thrift. The ceiling is the compose
memory cap: a limit at or above it is no ceiling at all, and the container is OOM-killed
before the runtime collects.
"""

import re

from _helpers import REPO

_COMPOSE = REPO / "ansible/roles/containers/alloy/templates/docker-compose.yml.j2"

# The post-GC floor of `go_memstats_heap_alloc_bytes{job="alloy-pi"}`: 38.3-38.5 MB on every
# day from 2026-09-06 to 2026-09-17, after a three-day climb from 29 MB. Re-measure with
# `min_over_time(go_memstats_heap_alloc_bytes{job="alloy-pi"}[1d])` on a process older than
# three days, never on a fresh one (22 MB in its first hour).
MEASURED_LIVE_HEAP_MIB = 37
# `go_memstats_sys_bytes` − `go_memstats_heap_sys_bytes`: stacks, GC metadata, mspan/mcache
# and the profiling buckets. 8.3 MB on 2026-09-18.
NON_HEAP_RUNTIME_MIB = 8

_UNITS_MIB = {"MiB": 1, "M": 1, "GiB": 1024, "G": 1024}


def _mib(quantity: str) -> int:
    match = re.fullmatch(r"(\d+)(MiB|M|GiB|G)", quantity)
    assert match, f"unparseable memory quantity {quantity!r}"
    return int(match.group(1)) * _UNITS_MIB[match.group(2)]


# With GOGC=off the limit is the only trigger, so the slack between the live set and the
# limit is the whole cycle. Less than GOGC=50's half-heap of slack would give a shorter cycle
# than the 2026-09-03 shape the marker at the GOGC line replaced, which is the direction of
# the treadmill above. The forced two-minute cycle GOGC=off removes is why it is off at all.
MIN_SLACK_PERCENT_WHEN_OFF = 50


def gomemlimit_problem(gomemlimit: str, mem_cap: str, gogc: int | str) -> str | None:
    """The failure message for a (GOMEMLIMIT, compose memory cap, GOGC) triple, else None.

    `gogc` is the percentage, or the string "off"; anything else is a malformed compose.
    """
    limit = _mib(gomemlimit)
    if gogc == "off":
        heap_goal = MEASURED_LIVE_HEAP_MIB * (100 + MIN_SLACK_PERCENT_WHEN_OFF) // 100
        how = f"GOGC=off needs at least {MIN_SLACK_PERCENT_WHEN_OFF}% slack, a {heap_goal} MiB goal"
    else:
        assert isinstance(gogc, int), (
            f"GOGC must be a percentage or 'off', not {gogc!r}"
        )
        heap_goal = MEASURED_LIVE_HEAP_MIB * (100 + gogc) // 100
        how = f"at GOGC={gogc} is a {heap_goal} MiB goal"
    floor = heap_goal + NON_HEAP_RUNTIME_MIB
    if limit < floor:
        return (
            f"GOMEMLIMIT={gomemlimit} is under the {floor} MiB floor (live heap "
            f"{MEASURED_LIVE_HEAP_MIB} MiB {how}, plus "
            f"{NON_HEAP_RUNTIME_MIB} non-heap); the limit sets the heap goal, not GOGC"
        )
    if limit >= _mib(mem_cap):
        return f"GOMEMLIMIT={gomemlimit} is not below the {mem_cap} memory cap; OOM before GC"
    return None


def _live_values() -> tuple[str, str, int | str]:
    text = _COMPOSE.read_text()
    limit = re.search(r"^\s*- GOMEMLIMIT=(\S+)", text, re.MULTILINE)
    gogc = re.search(r"^\s*- GOGC=(\d+|off)", text, re.MULTILINE)
    cap = re.search(r"resources\('[\d.]+', '(\w+)'", text)
    assert limit and gogc and cap, (
        "the alloy compose lost its GOMEMLIMIT, GOGC or resources() line"
    )
    raw = gogc.group(1)
    return limit.group(1), cap.group(1), raw if raw == "off" else int(raw)


def test_the_live_limit_has_headroom_and_a_cap_above_it() -> None:
    gomemlimit, mem_cap, gogc = _live_values()
    problem = gomemlimit_problem(gomemlimit, mem_cap, gogc)
    assert problem is None, problem


def test_the_shipped_triple_is_clean() -> None:
    assert gomemlimit_problem("104MiB", "128M", "off") is None


def test_a_limit_with_too_little_slack_for_gogc_off_is_flagged() -> None:
    """With GOGC off the limit is the only trigger; 60MiB leaves under half a heap of slack."""
    assert gomemlimit_problem("60MiB", "128M", "off") == (
        "GOMEMLIMIT=60MiB is under the 63 MiB floor (live heap 37 MiB GOGC=off needs at "
        "least 50% slack, a 55 MiB goal, plus 8 non-heap); the limit sets the heap goal, "
        "not GOGC"
    )


def test_the_2026_09_03_triple_is_clean() -> None:
    """The GOGC=50 pairing that ran 2026-09-03 to 2026-09-18, before #1967 raised the goal."""
    assert gomemlimit_problem("72MiB", "96M", 50) is None


def test_the_2026_09_02_value_is_flagged() -> None:
    """The limit Alloy first shipped with, sized from RSS rather than the heap goal."""
    assert gomemlimit_problem("48MiB", "96M", 50) == (
        "GOMEMLIMIT=48MiB is under the 63 MiB floor (live heap 37 MiB at GOGC=50 is a "
        "55 MiB goal, plus 8 non-heap); the limit sets the heap goal, not GOGC"
    )


def test_the_default_gogc_moves_the_floor_past_the_limit() -> None:
    """Dropping the GOGC line restores Go's default 100%, and 72MiB no longer clears it."""
    assert gomemlimit_problem("72MiB", "96M", 100) == (
        "GOMEMLIMIT=72MiB is under the 82 MiB floor (live heap 37 MiB at GOGC=100 is a "
        "74 MiB goal, plus 8 non-heap); the limit sets the heap goal, not GOGC"
    )


def test_a_limit_at_the_cap_is_flagged() -> None:
    assert gomemlimit_problem("96MiB", "96M", 50) == (
        "GOMEMLIMIT=96MiB is not below the 96M memory cap; OOM before GC"
    )
