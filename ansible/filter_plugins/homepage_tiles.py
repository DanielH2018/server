"""Ansible filter plugin deriving homepage's tile links and widget edges from containers_list.

A homepage widget that dials a Service's ClusterIP needs that Service's NetworkPolicy to admit
`app: homepage`, because the namespace baseline admits only traefik, prometheus and the node
CIDRs. Before #3691 the edge was written twice: the widget URL in homepage's
`services.yaml.j2`, and `homepage` in the target entry's `netpol_from`. A test kept the two
lists equal.

One key now carries both halves. `homepage_widget: true` on an entry means "homepage dials my
ClusterIP". `netpol_callers` adds `homepage` to the callers every fence renderer reads, and
`homepage_widget_url` is the only way a tile builds that ClusterIP URL. It refuses an entry
without the key, so a widget URL and its edge cannot disagree.

`homepage_href` builds a tile's link from the entry's `hostname`, so a renamed route moves the
tile with it.

No Ansible import, so the tests call the same functions the playbook runs. `FilterModule`
re-raises a function's `ValueError` as `AnsibleFilterError` prefixed with the filter's name,
which fails the render task before any apply.
"""

import sys as _sys
from pathlib import Path as _Path

# Ansible loads a plugin by file path, so `filter_errors` beside it imports by bare name only
# once this directory is on the path (#4527).
if str(_Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))

from filter_errors import _as_filter

HOMEPAGE_APP = "homepage"


def _entry(containers_list, name):
    for entry in containers_list or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    raise ValueError(f"no containers_list entry is named {name!r}")


def netpol_callers(entry):
    """The `app` labels an entry's caller fence admits.

    That is the entry's `netpol_from`, plus `homepage` when the entry sets `homepage_widget`.
    Every template that renders an entry's fence reads this, never `netpol_from` alone, so a
    widget's edge reaches the role-owned fences (sonarr, radarr, qbittorrent) too.
    """
    callers = list(entry.get("netpol_from") or [])
    if entry.get("homepage_widget") and HOMEPAGE_APP not in callers:
        callers.append(HOMEPAGE_APP)
    return callers


def netpol_caller_fences(containers_list):
    """The caller fences netpol-baseline renders, each `{app, port, callers}`, in list order.

    First one fence per entry with any `netpol_callers`, unless the entry sets
    `netpol_role_owned` because its own role renders that fence. The fence takes the entry's
    `netpol_app`, or its `name`, and its `port`. Then one per `netpol_fences` item of each
    entry, unless the item sets `role_owned`. Returning the fences rather than the entries
    lets the deploy narrowing name the fields the template reads (#4395).
    """
    entries = [e for e in containers_list or [] if isinstance(e, dict)]
    fences = []
    for entry in entries:
        callers = netpol_callers(entry)
        if callers and not entry.get("netpol_role_owned", False):
            app = entry.get("netpol_app", entry["name"])
            fences.append({"app": app, "port": entry["port"], "callers": callers})
    for entry in entries:
        for fence in entry.get("netpol_fences", []):
            if not fence.get("role_owned", False):
                fences.append(
                    {
                        "app": fence["app"],
                        "port": fence["port"],
                        "callers": fence["from"],
                    }
                )
    return fences


def homepage_widget_url(containers_list, name, namespace):
    """`http://<name>.<namespace>.svc.cluster.local:<port>` for entry `name`'s Service.

    Raises ValueError when no entry is named `name`, when it has no `port`, or when it does
    not set `homepage_widget: true`. The last is the point: without the key the entry's fence
    does not admit homepage, and the widget would render as a widget-proxy error while
    homepage stays 1/1.
    """
    entry = _entry(containers_list, name)
    if not entry.get("homepage_widget"):
        raise ValueError(
            f"containers_list entry {name!r} does not set `homepage_widget: true`, so its "
            "NetworkPolicy does not admit homepage. Set the key on the entry, unless a bespoke "
            "template renders its fence (pihole); see services.yaml.j2."
        )
    if entry.get("port") is None:
        raise ValueError(f"containers_list entry {name!r} has no `port`")
    return f"http://{name}.{namespace}.svc.cluster.local:{int(entry['port'])}"


def homepage_href(containers_list, name, domain, path="/", lan=False):
    """`https://<hostname>[.local].<domain><path>` for entry `name`'s route.

    `lan=True` names the `.local.` host, for a route that matches no public name (longhorn,
    the deploy queue). Raises ValueError when no entry is named `name` or it has no
    `hostname`, so a tile cannot link to a route that is not declared.
    """
    entry = _entry(containers_list, name)
    if not entry.get("hostname"):
        raise ValueError(f"containers_list entry {name!r} has no `hostname`")
    host = f"{entry['hostname']}.local" if lan else entry["hostname"]
    return f"https://{host}.{domain}{path}"


class FilterModule:
    def filters(self):
        return {
            "netpol_callers": _as_filter(netpol_callers),
            "netpol_caller_fences": _as_filter(netpol_caller_fences),
            "homepage_widget_url": _as_filter(homepage_widget_url),
            "homepage_href": _as_filter(homepage_href),
        }
