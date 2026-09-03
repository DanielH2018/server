"""Guard: the Pi Alloy's GOMEMLIMIT sits above the runtime's measured total, below the cap.

GOMEMLIMIT bounds the Go runtime's TOTAL memory (`go_memstats_sys_bytes`), not the live heap.
A limit below that total does not save memory; it makes the runtime collect continuously,
bounded only by the GC CPU limiter at 50% of GOMAXPROCS. That is how the first Alloy on
daniel-pi ran for 7 days: GOMEMLIMIT=48MiB against a 58.7 MB runtime total, 2 GC cycles a
second for 1.6 log lines a second, 0.28 of a core steady (#932), the largest load on the host
and the contention behind #930.

The floor here is that measured total plus headroom. Lowering the limit back under it "to
save RAM" is a one-line edit that renders, lints and deploys green, and reads as thrift.
The ceiling is the compose memory cap: a limit at or above it is no ceiling at all, and the
container is OOM-killed before the runtime collects.
"""

from __future__ import annotations

import re

from _helpers import REPO

_COMPOSE = REPO / "ansible/roles/containers/alloy/templates/docker-compose.yml.j2"

# `go_memstats_sys_bytes` on 2026-09-03 was 58.7 MB. Anything at or under it is the
# continuous-collection state above.
MEASURED_RUNTIME_TOTAL_MIB = 59
HEADROOM_MIB = 8

_UNITS_MIB = {"MiB": 1, "M": 1, "GiB": 1024, "G": 1024}


def _mib(quantity: str) -> int:
    match = re.fullmatch(r"(\d+)(MiB|M|GiB|G)", quantity)
    assert match, f"unparseable memory quantity {quantity!r}"
    return int(match.group(1)) * _UNITS_MIB[match.group(2)]


def gomemlimit_problem(gomemlimit: str, mem_cap: str) -> str | None:
    """The failure message for a (GOMEMLIMIT, compose memory cap) pair, else None."""
    limit = _mib(gomemlimit)
    floor = MEASURED_RUNTIME_TOTAL_MIB + HEADROOM_MIB
    if limit < floor:
        return (
            f"GOMEMLIMIT={gomemlimit} is under the {floor} MiB floor (runtime total "
            f"{MEASURED_RUNTIME_TOTAL_MIB} MiB + {HEADROOM_MIB} headroom); the runtime "
            "collects continuously at the GC CPU limiter"
        )
    if limit >= _mib(mem_cap):
        return f"GOMEMLIMIT={gomemlimit} is not below the {mem_cap} memory cap; OOM before GC"
    return None


def _live_values() -> tuple[str, str]:
    text = _COMPOSE.read_text()
    limit = re.search(r"^\s*- GOMEMLIMIT=(\S+)", text, re.MULTILINE)
    cap = re.search(r"resources\('[\d.]+', '(\w+)'", text)
    assert limit and cap, "the alloy compose lost its GOMEMLIMIT or resources() line"
    return limit.group(1), cap.group(1)


def test_the_live_limit_has_headroom_and_a_cap_above_it() -> None:
    gomemlimit, mem_cap = _live_values()
    problem = gomemlimit_problem(gomemlimit, mem_cap)
    assert problem is None, problem


def test_the_shipped_pair_is_clean() -> None:
    assert gomemlimit_problem("72MiB", "96M") is None


def test_the_2026_09_02_value_is_flagged() -> None:
    """The limit Alloy first shipped with, sized from RSS rather than the runtime total."""
    assert gomemlimit_problem("48MiB", "96M") == (
        "GOMEMLIMIT=48MiB is under the 67 MiB floor (runtime total 59 MiB + 8 headroom); "
        "the runtime collects continuously at the GC CPU limiter"
    )


def test_a_limit_at_the_cap_is_flagged() -> None:
    assert gomemlimit_problem("96MiB", "96M") == (
        "GOMEMLIMIT=96MiB is not below the 96M memory cap; OOM before GC"
    )
