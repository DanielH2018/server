"""What the live Kuma says about itself: its pod's age and the monitors it holds.

`probe.py kuma-drift` and postflight need both to tell a pending tile from a missing one. The
monitor set is read from Kuma's public status page.

Kuma's exporter emits a monitor only after its first beat, so the exporter cannot tell a tile
Kuma never created from one that is not yet due. A weekly tile never leaves the not-yet-due
window, because the weekly reboot restarts Kuma first. That is how a tile AutoKuma refused
hid in #3985 (#4005). This census answers the question the exporter cannot.

It reads the status page rather than `kuma monitor list`. That command needs Kuma's admin
password over socket.io, and `probe.py` is an allow-listed read-only probe.
`/api/status-page/` bypasses Authelia from the LAN by design, because Homepage's widget reads
it. Four calls on 2026-10-09 answered in 0.034-0.041s.

The page is a census only because `kuma-status-page-sync` places EVERY declared monitor on it,
with `Other` as the net for an unmatched one. ENFORCED:
`ansible/roles/k8s/uptime-kuma/tests/test_status_page_groups.py::test_every_declared_monitor_lands_in_a_named_group`.
Kuma's schema declares `monitor_group.monitor_id` ON DELETE CASCADE, so a monitor AutoKuma
deletes leaves the page as well. A monitor Kuma never created was never placed.

It has two blind spots. A new tile reads as absent until the quarter-hourly sync places it. A
sync that has stopped leaves the page stale, and the sync's own push tile reports that.
"""

import json
from datetime import datetime, timezone

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# `core.<name>` so a test's monkeypatch of core reaches the call.
from diagnostics.probe_lib import core
from diagnostics.probe_lib.health_kubectl import k8s_pods_args
from diagnostics.probe_lib.health_rollout import seconds_since
from lib.json_types import as_object, as_object_list
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from lib.kubectl import DEFAULT_CLUSTER, kubectl_json

_DEFAULTS_PATH = K8S_ROLES / "uptime-kuma" / "defaults" / "main.yml"


def status_page_url():
    """(url, curl --resolve pin) for the page `kuma-status-page-sync` maintains.

    The slug comes from the role default the sync reads, so a renamed page cannot leave this
    reading a 404 as "every pending tile never created".
    """
    base, pin = core.k8s_endpoint("uptime-kuma")
    with open(_DEFAULTS_PATH) as f:
        slug = yaml_fast.safe_load(f)["uptime_kuma_k8s_status_page_slug"]
    return f"{base}/api/status-page/{slug}", pin


def parse_status_page(body):
    """The monitor names on a status page body, or None when the body has no group list.

    A body without `publicGroupList` (an error object, an HTML error page) is unreadable, not
    empty. Read as an empty census, it would report every pending tile as never created.
    """
    try:
        return {
            monitor["name"]
            for group in json.loads(body)["publicGroupList"]
            for monitor in group["monitorList"]
            if monitor.get("name")
        }
    except json.JSONDecodeError, KeyError, AttributeError, TypeError:
        return None


def created_monitors():
    """The set of monitor names Kuma holds, or a string saying why it could not be read."""
    url, pin = status_page_url()
    status, body = core.get_status(url, resolve=pin)
    if status != 200:
        return f"status page {url} returned {status or body}"
    names = parse_status_page(body)
    if names is None:
        return f"status page {url} returned a body with no group list"
    return names


def pod_age_seconds(cluster=DEFAULT_CLUSTER):
    """Seconds since the uptime-kuma pod started, or None if that cannot be read."""
    pods_doc = kubectl_json(
        cluster, *k8s_pods_args("uptime-kuma", core.k8s_namespace())
    )
    if pods_doc is None:
        return None
    starts = [
        seconds_since(
            as_object(p.get("status") or {}, "pod status").get("startTime"),
            datetime.now(timezone.utc),
        )
        for p in as_object_list(pods_doc.get("items", []), "kubectl pods items")
    ]
    starts = [s for s in starts if s is not None]
    return min(starts) if starts else None
