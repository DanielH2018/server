"""Ansible filter plugin sizing monitor-bridge's builder lag grace from the canary's updater schedule.

The builder holds every host package behind the rollout canary until the canary reports the
gate's commit (`hosts/lib/builder_rollout_gate.py`), and the canary is daniel-pi at all hours
(`CANARY_ORDER`, #6530). The Pi's updater timer skips 23:00 to 00:59 and 04:00 to 05:59, so a Pi
package published just after a fire before a gap waits 2h15m for the next one. The builder's
unbuilt-since series counts that wait, and monitor-bridge's `host_packages` row pages once it
passes `BUILDER_MAX_LAG_S`. A fixed 2 hours would page on every such publish.

`updater_lag_grace_s` derives the grace from the timer text, never from a number written beside
it: the longest gap between two fires of the shared `homelab-updater.timer` with the canary's
drop-in over it, plus the timer's `RandomizedDelaySec`, plus the service's `TimeoutStartSec`
(the same sum `scripts/renderer/tests/test_renderer_host_update_limits.py` holds
`HostUpdateStale` to), plus `SLACK_S`. The floor is the grace for the causes that do not depend
on the schedule, such as a trust refusal or a red master, so a dense schedule never lowers it.

The schedule reader is `scripts/lib/systemd_timer.py`, which Ansible cannot import by name.
This module loads it by path, as `standalone_guard.py` does for its resolver, so `scripts/`
stays off `sys.path`.

`FilterModule.filters()` returns one literal dict, because
`scripts/deploy_tools/narrow_lib/filters.py:filter_names` reads the filter names statically.
"""

import functools
import importlib.util
import sys as _sys
from pathlib import Path as _Path

# Ansible loads a plugin by file path, so `filter_errors` beside it imports by bare name only
# once this directory is on sys.path.
if str(_Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))

from filter_errors import _as_filter

_READER = _Path(__file__).resolve().parents[2] / "scripts" / "lib" / "systemd_timer.py"

# What the gap, the delay and the run timeout leave out of a gate's life: master CI and one
# builder run before the canary's package stages (the unbuilt clock starts at the commit's
# committer time), then the scrape and one builder run after the canary reports. The landing's
# host wait adds the same 10 minutes (`DEADLINE_SLACK_S`).
SLACK_S = 10 * 60


@functools.cache
def _reader():
    # Loaded on the first call, not at import: Ansible imports every plugin in every play, so
    # a reader that failed to load would otherwise stop plays that never use the filter.
    spec = importlib.util.spec_from_file_location("homelab_systemd_timer", _READER)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the schedule reader from {_READER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def updater_lag_grace_s(timer: str, service: str, dropin: str, floor_s) -> int:
    """How long the builder may leave master unbuilt behind a canary on this schedule, in seconds.

    Args:
        timer: The shared `homelab-updater.timer` text.
        service: The `homelab-updater.service` text, which carries `TimeoutStartSec=`.
        dropin: The canary host's `homelab-updater.timer.conf` text.
        floor_s: The grace for causes that do not depend on the schedule.

    Returns:
        The larger of `floor_s` and the canary's longest gap between two fires, its randomized
        delay, its run timeout and `SLACK_S`.

    Raises:
        ValueError: The schedule uses a trigger the reader cannot bound, or `floor_s` is not a
            whole number of seconds.
    """
    reader = _reader()
    try:
        floor = int(floor_s)
    except TypeError, ValueError:
        raise ValueError(
            f"the floor {floor_s!r} is not a whole number of seconds"
        ) from None
    needed = (
        reader.longest_gap_s(timer, [dropin])
        + reader.timeout_start_s(service)
        + SLACK_S
    )
    return max(floor, needed)


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {"updater_lag_grace_s": _as_filter(updater_lag_grace_s)}
