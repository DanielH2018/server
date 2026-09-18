"""Guard: the Pi's dockerd and containerd GOMEMLIMITs sit above the treadmill, below a cap.

Each daemon runs `GOGC=off` under `GOMEMLIMIT` (roles/setup/docker_install, go-runtime.yml),
so the limit is the only GC trigger and the slack between the daemon's live set plus the
runtime's non-heap classes and the limit is one cycle's allocation. A limit near the live
set makes the runtime collect continuously, bounded only by the GC CPU limiter -- the shape
the first Pi Alloy ran in for seven days (#932). A limit under 120 s of allocation is the
subtler failure: the Go runtime forces a cycle every two minutes when GOGC is on, and
GOGC=off exists to escape that floor, so a limit that cycles faster than the floor is worse
than the default it replaced. That is what the issue's own "live + 50% + non-heap" recipe
would have shipped: 7 MB of slack on a 14 MB heap, a cycle every ~60 s against ~85 s
today (#2003).

The ceiling is not a cgroup cap -- these are host daemons -- but the limit IS the daemon's
steady-state footprint under GOGC=off, since the heap grows to it before every collection,
and daniel-pi has 456 MB with 20 MB free and 224 MB in zram. 128 MiB is the Pi Alloy's
containment cap and is not a measured number.

Re-measure, on a process older than a day, with the `dockerd-pi` / `containerd-pi` jobs:
  min_over_time(go_memstats_heap_alloc_bytes{job="dockerd-pi"}[1d])        -> live floor
  max_over_time(go_memstats_next_gc_bytes{job="dockerd-pi"}[1d]) / 2       -> live peak
      (under GOGC=100 only; with GOGC off next_gc is the limit, use heap_alloc's
      post-cycle troughs instead)
  go_memstats_sys_bytes - go_memstats_heap_sys_bytes                        -> non-heap
  rate(go_memstats_alloc_bytes_total{job="dockerd-pi"}[1h]) / 1e3          -> KB/s

Run: uv run pytest ansible/tests/setup/test_docker_daemons_gomemlimit_headroom.py
"""

import re

import pytest
from _helpers import SETUP_ROLES, load_yaml

_ROLE = SETUP_ROLES / "docker_install"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_GO_RUNTIME = _ROLE / "tasks" / "go-runtime.yml"
_INSTALL = _ROLE / "tasks" / "install.yml"

# Measured 2026-09-18 on daniel-pi over the first 4.3 h of the scrapes, both daemons on
# go1.26.8 under the default GOGC=100. Live peak is next_gc's ceiling halved (28.0 and
# 27.3 MB); the floors were 10.7 and 10.4 MB.
MEASURED = {
    "dockerd": {"live_mib": 14, "non_heap_mib": 10, "alloc_kb_s": 114},
    "containerd": {"live_mib": 14, "non_heap_mib": 8, "alloc_kb_s": 97},
}
# The runtime's forced-cycle period (`forcegcperiod`, two minutes). Slack for fewer
# seconds of allocation than this gives a shorter cycle than GOGC=100 on the floor.
MIN_CYCLE_SECONDS = 120
MAX_LIMIT_MIB = 128

# The tags and file every reader of the role's CLAUDE.md is pointed at.
DAEMON_TAGS = frozenset({"docker-go-runtime-dockerd", "docker-go-runtime-containerd"})
SHIM_OVERRIDE = "[plugins.'io.containerd.shim.v1.manager']"

_UNITS_MIB = {"MiB": 1, "M": 1, "GiB": 1024, "G": 1024}


def _mib(quantity: str) -> int:
    match = re.fullmatch(r"(\d+)(MiB|M|GiB|G)", quantity)
    assert match, f"unparseable memory quantity {quantity!r}"
    return int(match.group(1)) * _UNITS_MIB[match.group(2)]


def gomemlimit_problem(daemon: str, gomemlimit: str) -> str | None:
    """The failure message for one daemon's GOMEMLIMIT, else None."""
    m = MEASURED[daemon]
    limit = _mib(gomemlimit)
    slack_mib = MIN_CYCLE_SECONDS * m["alloc_kb_s"] // 1024
    floor = m["live_mib"] + m["non_heap_mib"] + slack_mib
    if limit < floor:
        return (
            f"{daemon}: GOMEMLIMIT={gomemlimit} is under the {floor} MiB floor (live "
            f"{m['live_mib']} + non-heap {m['non_heap_mib']} + {slack_mib} MiB, which is "
            f"{MIN_CYCLE_SECONDS} s at {m['alloc_kb_s']} KB/s); GOGC=off would cycle "
            "faster than the two-minute floor it exists to escape"
        )
    if limit > MAX_LIMIT_MIB:
        return (
            f"{daemon}: GOMEMLIMIT={gomemlimit} is over {MAX_LIMIT_MIB} MiB; under "
            "GOGC=off the heap grows to the limit, so this is the daemon's footprint"
        )
    return None


@pytest.mark.parametrize("daemon", sorted(MEASURED))
def test_headroom_rule_is_clean_on_a_limit_with_a_cycle_of_slack(daemon):
    assert gomemlimit_problem(daemon, "64MiB") is None


@pytest.mark.parametrize("daemon", sorted(MEASURED))
def test_headroom_rule_is_flagged_on_the_issues_fifty_percent_recipe(daemon):
    m = MEASURED[daemon]
    recipe = f"{m['live_mib'] * 3 // 2 + m['non_heap_mib']}MiB"
    assert "under the" in (gomemlimit_problem(daemon, recipe) or "")


def test_headroom_rule_is_flagged_on_a_limit_over_the_footprint_cap():
    assert "over 128 MiB" in (gomemlimit_problem("dockerd", "256MiB") or "")


@pytest.mark.parametrize("daemon", sorted(MEASURED))
def test_committed_limit_clears_the_floor_and_the_cap(daemon):
    # One scalar per daemon: a dict would be replaced whole by a host_vars override.
    limit = load_yaml(_DEFAULTS)[f"docker_install_gomemlimit_{daemon}"]
    assert gomemlimit_problem(daemon, limit) is None


def test_each_daemon_has_its_own_tag_and_the_shim_override_rides_with_containerds():
    """The stagger the issue asks for is one daemon per day, so each needs its own tag,
    and containerd's must also select the config.toml block that stops its shims
    inheriting the pairing."""
    imports = [
        t
        for t in load_yaml(_INSTALL)
        if isinstance(t, dict)
        and t.get("ansible.builtin.import_tasks") == "go-runtime.yml"
    ]
    assert len(imports) == 2, "one import per daemon"
    tags_seen = {tag for t in imports for tag in t["tags"]}
    assert DAEMON_TAGS <= tags_seen, DAEMON_TAGS - tags_seen
    shim_block = next(
        t
        for t in load_yaml(_INSTALL)
        if isinstance(t, dict)
        and SHIM_OVERRIDE
        in (t.get("ansible.builtin.blockinfile") or {}).get("block", "")
    )
    assert "docker-go-runtime-containerd" in shim_block["tags"]
    assert "GOGC=100" in shim_block["ansible.builtin.blockinfile"]["block"]
    assert "GOMEMLIMIT=off" in shim_block["ansible.builtin.blockinfile"]["block"]


def test_go_runtime_drop_in_pairs_gogc_off_with_the_limit_and_can_be_removed():
    tasks = [t for t in load_yaml(_GO_RUNTIME) if isinstance(t, dict)]
    written = next(t for t in tasks if "ansible.builtin.copy" in t)
    content = written["ansible.builtin.copy"]["content"]
    assert "Environment=GOGC=off" in content
    assert "Environment=GOMEMLIMIT={{ docker_install_go_runtime_limit }}" in content
    lookup = next(t for t in tasks if "ansible.builtin.set_fact" in t)[
        "ansible.builtin.set_fact"
    ]
    assert "docker_install_gomemlimit_" in lookup["docker_install_go_runtime_limit"]
    removed = next(
        t
        for t in tasks
        if (t.get("ansible.builtin.file") or {}).get("state") == "absent"
    )
    assert (
        removed["ansible.builtin.file"]["path"]
        == written["ansible.builtin.copy"]["dest"]
    )
