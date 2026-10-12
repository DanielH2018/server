"""Which declared Kuma monitors are daniel-pi's own, for `probe.py kuma-drift --pi`.

Read from the YAML `stringData` key each monitor is declared under, which names the host or
role that owns it, since the monitor's JSON body does not.
"""

import re

_ENTITY_NAME_RE = re.compile(r'"name":\s*"([^"]+)"')
_MONITOR_KEY_RE = re.compile(r"^\s*([a-z0-9_-]+)\.json:\s*\|\s*$")


def monitor_keys(text):
    """Map each declared monitor's display name to its YAML `stringData` key.

    The key is the file-like name Ansible templates ("daniel-pi-host.json"), which encodes
    which host/role owns the monitor even though the JSON body doesn't carry that. Used to
    scope `kuma-drift --pi` to daniel-pi's own monitors without hand-listing their display
    names. It reads literal keys only, so the table loops' tiles are absent; neither loop
    renders a Pi tile.
    """
    keys, pending = {}, None
    for line in text.splitlines():
        km = _MONITOR_KEY_RE.match(line)
        if km:
            pending = km.group(1)
            continue
        name = _ENTITY_NAME_RE.search(line)
        if name and pending:
            keys[name.group(1)] = pending
            pending = None
    return keys


def is_pi_monitor_key(key):
    """Whether a monitor's YAML key names it as daniel-pi's, not a k8s workload's.

    `pi` must be its own hyphen-delimited token — the k8s Pi-hole role's key has `pi` only
    as a substring of `pihole`, so it is correctly excluded even though it also runs a Pi.
    """
    return "pi" in key.split("-")


def pi_monitor_names(text):
    """Declared monitor display names whose YAML key marks them as daniel-pi's."""
    return {name for name, key in monitor_keys(text).items() if is_pi_monitor_key(key)}
