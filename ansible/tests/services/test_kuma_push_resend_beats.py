"""Kuma's `resendInterval` is a count of consecutive DOWN beats, not a duration (#1838).

The value shipped as `kuma_push_resend_interval_minutes: 360`, and the k3s Workload Health
tile's own log showed the resend arriving once a DAY across a 3.5-day outage: a bridge-fed tile
takes one DOWN beat per bridge push plus one per heartbeat window, 15 an hour. The spacing is
derivable from the two cadences the repo already declares, so a value that reads as hours again
fails here rather than in the next multi-day outage. Its own module because
`test_kuma_static_monitors.py` sits at the 500-line test cap.
"""

import re

from _helpers import ANSIBLE
from _kuma_entities import ROLE_DEFAULTS, _entities

BRIDGE_ENV_SECRET = ANSIBLE / "roles/k8s/monitor-bridge/templates/env-secret.yaml.j2"


def test_bridge_fed_push_monitors_resend_within_a_working_day():
    beats = ROLE_DEFAULTS["kuma_push_resend_down_beats"]
    push_period = int(
        re.search(r'^\s*INTERVAL: "(\d+)"', BRIDGE_ENV_SECRET.read_text(), re.M).group(
            1
        )
    )
    window = ROLE_DEFAULTS["kuma_bridge_push_interval"]
    beats_per_hour = 3600 / push_period + 3600 / window
    spacing_h = beats / beats_per_hour
    assert 4 <= spacing_h <= 12, (
        f"kuma_push_resend_down_beats={beats} resends a bridge-fed tile every {spacing_h:.1f}h "
        f"({beats_per_hour:g} DOWN beats/h at INTERVAL={push_period}s, window={window}s); "
        "the intent is ~6h — see the defaults comment"
    )


def test_no_push_monitor_can_resend_faster_than_four_hours():
    # One beat count serves every push tile, and the tiles' windows differ 1000x, so the count
    # has to be read against the FASTEST beat rate any tile can reach. Only push tiles carry a
    # resend here (the http/ping tiles declare none), and the bridge is the fastest feeder in
    # the estate at one push per INTERVAL — every cron feeder is slower — so a tile's beat rate
    # is bounded by 3600/INTERVAL pushes plus 3600/window watchdog beats. At the narrowest
    # window that bound must still leave four hours between resends, or the count that is six
    # hours on the bridge tiles is a much shorter one on a fast cron's.
    beats = ROLE_DEFAULTS["kuma_push_resend_down_beats"]
    push_period = int(
        re.search(r'^\s*INTERVAL: "(\d+)"', BRIDGE_ENV_SECRET.read_text(), re.M).group(
            1
        )
    )
    windows = {
        entity["name"]: int(entity["interval"])
        for entity in _entities().values()
        if entity["type"] == "push"
    }
    assert {"k3s Workload Health", "Cloudflare DDNS Proxied"} <= windows.keys()
    assert len(windows) >= 40, f"push tile census went thin: {len(windows)}"
    name, narrowest = min(windows.items(), key=lambda kv: kv[1])
    fastest_beats_per_hour = 3600 / push_period + 3600 / narrowest
    floor_h = beats / fastest_beats_per_hour
    assert floor_h >= 4, (
        f"{name} ({narrowest}s window) could resend every {floor_h:.1f}h at "
        f"kuma_push_resend_down_beats={beats}"
    )


def test_every_push_monitor_carries_the_shared_beat_count_or_a_deliberate_hold():
    # The uniform value is what lets the spacing above be a statement about every tile. A hold
    # at 0 is the one other legitimate value, and test_kuma_static_monitors.py names which.
    beats = ROLE_DEFAULTS["kuma_push_resend_down_beats"]
    for name, entity in _entities().items():
        if entity["type"] != "push":
            continue
        assert entity.get("resendInterval") in (0, beats), (
            f"{name}: resendInterval must be the shared beat count or a hold at 0, got "
            f"{entity.get('resendInterval')!r}"
        )
