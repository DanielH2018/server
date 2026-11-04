"""Ansible filter plugin deriving the ingress probe targets from `containers_list`.

Every entry with a `hostname` is served at `https://<hostname>.local.<domain>`, and a probe of
that URL needs nothing the entry does not already carry: the hostname gives the URL, `auth_tier`
gives the expected status, and the entry's `tier` gives the page tier. So observability's
`prometheus.yaml.j2` loops over `containers_list | ingress_probe_targets` to render one target
per entry in the `blackbox-ingress` scrape job, which blackbox_exporter probes (M4).

An entry opts out with `probe: false`, and says why in a comment.

`probe: {id: <stem>, name: <display name>}` overrides the target's derived identity. The id is
`<stem>-k8s`, and the stem defaults to the entry's `name`. The display name defaults to
`k3s <name>`.

No Ansible import, so `probe_lib/health_alerts.py` reads the same function the playbook runs.
`FilterModule` registers the filter through `_as_filter`, so a refusal reaches the operator as
`AnsibleFilterError("ingress_probe_targets: ...")` (#4505).
"""

import sys as _sys
from pathlib import Path as _Path

# Ansible loads a plugin by file path, so `filter_errors` beside it imports by bare name only
# once this directory is on the path (#4527).
if str(_Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))

from filter_errors import _as_filter

# The keys a `probe:` mapping may carry. A misspelt one would otherwise be ignored and the
# target would keep its derived identity, which reads as the override having worked.
_PROBE_KEYS = frozenset({"id", "name"})

ID_SUFFIX = "-k8s"


def ingress_probe_targets(containers_list):
    """One probe target per `containers_list` entry with a `hostname`, in list order.

    Each target is a dict: `id`, `name` (the display name), `hostname`, `behind_sso` (whether
    Authelia answers first, so the probe expects its 302), and `entry` (the entry's `name`, for
    the template's tier lookup).

    Raises ValueError when an entry's `probe` is neither `false` nor a mapping, or carries a key
    outside `id` and `name`.
    """
    targets = []
    for entry in containers_list or []:
        if not isinstance(entry, dict) or not entry.get("hostname"):
            continue
        probe = entry.get("probe", {})
        if probe is False:
            continue
        if not isinstance(probe, dict):
            raise ValueError(
                f"containers_list entry {entry['name']!r}: `probe` must be false or a mapping, "
                f"got {probe!r}"
            )
        unknown = set(probe) - _PROBE_KEYS
        if unknown:
            raise ValueError(
                f"containers_list entry {entry['name']!r}: unknown `probe` keys "
                f"{sorted(unknown)}; known: {sorted(_PROBE_KEYS)}"
            )
        targets.append(
            {
                "id": probe.get("id", entry["name"]) + ID_SUFFIX,
                "name": probe.get("name", f"k3s {entry['name']}"),
                "hostname": entry["hostname"],
                "behind_sso": bool(entry.get("auth_tier")),
                "entry": entry["name"],
            }
        )
    return targets


class FilterModule:
    def filters(self):
        return {"ingress_probe_targets": _as_filter(ingress_probe_targets)}
