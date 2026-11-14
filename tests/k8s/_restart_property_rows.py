"""Rendered-manifest property rows about how a container recovers before the kubelet restarts it.

`test_rendered_properties.py` runs these rows beside its own. A row lives here only to keep that
file under the test-module length cap; the rules for what makes a row are in its docstring.
"""

import re

from _k8s_render import pod_spec
from _property_table import Property

# zigbee2mqtt 2.14.3 index.js exits on every boot when Z2M_WATCHDOG does not match this.
_Z2M_WATCHDOG_FORMAT = re.compile(r"^\d+(\.\d+)?(,\d+(\.\d+)?)*$")
_LIVENESS_DEFAULT_FAILURE_THRESHOLD = 3


def _z2m_containers(role: str, tpl: str, doc: dict):
    if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "zigbee2mqtt":
        yield "zigbee2mqtt", pod_spec(doc)["containers"][0]


def _z2m_watchdog_offence(role: str, container: dict) -> str | None:
    env = {e["name"]: e.get("value") for e in container.get("env", [])}
    value = env.get("Z2M_WATCHDOG")
    if value is None:
        return "no Z2M_WATCHDOG, so a coordinator socket reset exits the container"
    if not _Z2M_WATCHDOG_FORMAT.match(value):
        return f"Z2M_WATCHDOG={value!r} is not minutes-CSV, so Z2M exits on every boot"
    probe = container["livenessProbe"]
    window = probe["periodSeconds"] * probe.get(
        "failureThreshold", _LIVENESS_DEFAULT_FAILURE_THRESHOLD
    )
    waited = sum(float(v) * 60 for v in value.split(","))
    if waited < window:
        return None
    return f"Z2M_WATCHDOG waits {waited:g}s, past the {window}s liveness window"


def _z2m_container(watchdog: str | None) -> tuple[str, dict]:
    env = [] if watchdog is None else [{"name": "Z2M_WATCHDOG", "value": watchdog}]
    return "zigbee2mqtt", {"env": env, "livenessProbe": {"periodSeconds": 30}}


RESTART_PROPERTIES = (
    Property(
        name="zigbee2mqtt-restarts-in-process-on-a-coordinator-reset",
        reason=(
            "The SLZB coordinator resets its TCP link (`read ECONNRESET`) and accepts a "
            "reconnect 2 s later; without Z2M's watchdog each reset exits the container (#8322). "
            "Z2M exits on boot for a value outside its minutes-CSV regex, a permanent crash "
            "loop. The frontend stops while the watchdog waits, so delays summing past "
            "liveness periodSeconds x failureThreshold end in a kubelet kill, not a reconnect."
        ),
        select=_z2m_containers,
        offence=_z2m_watchdog_offence,
        red=_z2m_container(None),
        green=_z2m_container("0.05,0.25,0.5"),
        more_red=(_z2m_container("3s,15s"), _z2m_container("1,5")),
        must_find=frozenset({"zigbee2mqtt"}),
    ),
)
