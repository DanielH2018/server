"""Guard: the Pi's dockerd and containerd GOMEMLIMITs sit above the treadmill, below a cap.

Each daemon runs `GOGC=off` under `GOMEMLIMIT` (hosts/components/pi-docker, a drop-in per daemon),
so the limit is the only GC trigger and the slack between the daemon's live set plus the
runtime's non-heap classes and the limit is one cycle's allocation. A limit near the live
set makes the runtime collect continuously, bounded only by the GC CPU limiter. A limit
under 120 s of allocation is the
subtler failure: the Go runtime forces a cycle every two minutes when GOGC is on, and
GOGC=off exists to escape that floor, so a limit that cycles faster than the floor is worse
than the default. A "live + 50% + non-heap" recipe lands there: 7 MB of slack on a 14 MB
heap is a cycle every ~60 s, against ~85 s with the shipped limits.

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
from _helpers import load_yaml
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

_COMPONENT = REPO / "hosts" / "components" / "pi-docker"


@pytest.fixture(scope="module")
def pi_package() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


# Measured on daniel-pi over the first 4.3 h of the scrapes, both daemons on
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
    defaults = load_yaml(_COMPONENT / "component.yml")["defaults"]
    limit = defaults[f"docker_engine_gomemlimit_{daemon}"]
    assert gomemlimit_problem(daemon, limit) is None


@pytest.mark.parametrize("daemon", sorted(MEASURED))
def test_each_daemon_pairs_gogc_off_with_its_own_limit(daemon, pi_package):
    """One drop-in per daemon, so a change to one limit marks only that daemon pending."""
    unit = {"dockerd": "docker", "containerd": "containerd"}[daemon]
    dropin = f"/usr/lib/systemd/system/{unit}.service.d/50-homelab.conf"
    limit = load_yaml(_COMPONENT / "component.yml")["defaults"][
        f"docker_engine_gomemlimit_{daemon}"
    ]
    content = pi_package.files[dropin][0].decode()
    assert "Environment=GOGC=off" in content
    assert f"Environment=GOMEMLIMIT={limit}" in content


def test_the_shim_override_rides_with_containerds_config():
    """containerd's shims inherit its GOGC=off unless config.toml hands them the defaults back."""
    (block,) = (
        edit["block"]
        for edit in load_yaml(_COMPONENT / "component.yml")["lines"]
        if edit["path"] == "/etc/containerd/config.toml"
    )
    assert SHIM_OVERRIDE in block
    assert 'env = ["GOGC=100", "GOMEMLIMIT=off"]' in block
