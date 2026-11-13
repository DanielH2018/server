"""Ansible filter plugin: the values a template collects from every `containers_list` entry.

A template that loops `containers_list` itself may read any field of any entry (#4395). Each
filter here returns only the values its template renders, never an entry.

No Ansible import, so the render harnesses register the same functions the renderer runs.
"""


def _entries(containers_list):
    return [e for e in containers_list or [] if isinstance(e, dict)]


def entry_names(containers_list):
    """Each entry's `name`, in list order."""
    return [entry["name"] for entry in _entries(containers_list)]


def published_ports(containers_list):
    """`<name>:<port>` for each entry that sets `port`, in list order.

    A `port: null` counts as set and renders `None`, as the Jinja loop this replaced did.
    """
    return [
        f"{entry['name']}:{entry['port']}"
        for entry in _entries(containers_list)
        if "port" in entry
    ]


def lan_hostnames(containers_list):
    """The names an entry's routes answer on, in list order: `hostname`, then `extra_hostnames`.

    An entry contributes its `hostname` when it sets one and its `extra_hostnames` either way.
    The names are bare: the template appends `.local.<domain>`.
    """
    names = []
    for entry in _entries(containers_list):
        if "hostname" in entry:
            names.append(entry["hostname"])
        names.extend(entry.get("extra_hostnames", []))
    return names


class FilterModule:
    def filters(self):
        return {
            "entry_names": entry_names,
            "published_ports": published_ports,
            "lan_hostnames": lan_hostnames,
        }
