#!/usr/bin/env python3
"""Fetch + adapt Grafana community dashboards for headless (provisioned) use.

Companion to ``export_grafana_dashboards.py`` (live DB -> code): this script owns the
community boards (upstream = grafana.com); that one owns the custom boards (upstream = the
live Grafana DB). They write disjoint files in ``files/dashboards/`` and never clobber each
other (the exporter skips the uids seeded here).

The grafana role seeds a few community dashboards as code. Community exports assume the
interactive *Import* flow (which prompts for a datasource and populates template variables);
file-provisioning skips that, so a raw export shows "datasource not found" or empty panels.
This script adapts each export so it renders on first load with no manual clicks:

  1. Pin datasource references to the uids we provision (see datasources.yml.j2) — the
     grafana.com ``${DS_*}`` import placeholders are rewritten to the real uids.
  2. Give every template variable a working default so panels have data on load:
       * ``includeAll`` variables  -> "All" (``$__all``)
       * single-select query vars  -> first value resolved live from Prometheus
         (``label_values(...)``), with chained vars resolved in order.

Re-run to regenerate (e.g. after a Grafana upgrade or to refresh defaults):

    python3 scripts/fetch_grafana_dashboards.py

Requires a running ``grafana`` container — it's used to reach Prometheus on the monitoring
network for the ``label_values`` lookups. Idempotent; overwrites the JSON in the role.
"""

import json
import re
import subprocess
import urllib.parse
import urllib.request

# Datasource uids we provision (adopted from the pre-existing hand-made datasources).
UID_BY_PLUGIN = {
    "prometheus": ("EGdsQqhVk", "Prometheus"),
    "loki": ("bf4q19tuivta8e", "Loki"),
}
DASHBOARDS = {"node-exporter-full": 1860, "cadvisor": 14282, "traefik": 17346}
OUTDIR = "ansible/roles/containers/grafana/files/dashboards"

# Panels removed because the underlying metric has no data on this host: no NIC
# link-speed / battery / fan sensors, and the systemd collector isn't enabled
# (would need host D-Bus access from the container). Dropping them keeps the board
# free of permanently-"No data" panels.
DROP_PANELS = {
    "node-exporter-full": {
        "Network Saturation",
        "Power Supply",
        "Hardware Fan Speed",
        "Systemd Units State",
        "Systemd Sockets Current",
        "Systemd Sockets Accepted",
        "Systemd Sockets Refused",
    },
}


def fetch(gnet_id):
    url = "https://grafana.com/api/dashboards/%d/revisions/latest/download" % gnet_id
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def prom_label_values(query, resolved):
    """Resolve a Grafana ``label_values(...)`` query to a sorted list of values.

    `resolved` maps already-resolved variable name -> chosen value, substituted into the
    query so chained variables (e.g. nodename depends on $job) resolve correctly.
    """
    for name, val in resolved.items():
        query = query.replace("${%s}" % name, val).replace("$" + name, val)
    m = re.match(r"\s*label_values\(\s*(.+?)\s*,\s*([a-zA-Z_]\w*)\s*\)\s*$", query)
    if m:
        selector, label = m.group(1), m.group(2)
        promql = "group by (%s)(%s)" % (label, selector)
    else:
        m = re.match(r"\s*label_values\(\s*([a-zA-Z_]\w*)\s*\)\s*$", query)
        if not m:
            return []
        label = m.group(1)
        promql = 'group by (%s)({__name__!=""})' % label  # rarely used; broad fallback
    out = subprocess.run(
        [
            "docker",
            "exec",
            "grafana",
            "wget",
            "-qO-",
            "http://prometheus:9090/api/v1/query?query=" + urllib.parse.quote(promql),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    result = json.loads(out)["data"]["result"]
    return sorted({s["metric"].get(label) for s in result if s["metric"].get(label)})


def adapt(name, d):
    placeholders = {}  # ${NAME} -> uid string
    resolved = {}  # var name -> chosen single value (for chaining)

    for v in d.get("templating", {}).get("list", []):
        vtype = v.get("type")
        if vtype == "datasource":
            uid, text = UID_BY_PLUGIN.get(v.get("query"), (None, None))
            if uid:
                placeholders[v["name"]] = uid
                v["current"] = {"selected": True, "text": text, "value": uid}
                v["options"] = [{"selected": True, "text": text, "value": uid}]
        elif vtype == "query":
            if v.get("includeAll"):
                v["current"] = {"selected": True, "text": "All", "value": "$__all"}
                v["options"] = [{"selected": True, "text": "All", "value": "$__all"}]
            else:
                q = v.get("query")
                q = q.get("query") if isinstance(q, dict) else q
                vals = prom_label_values(q or "", resolved)
                if vals:
                    first = vals[0]
                    resolved[v["name"]] = first
                    v["current"] = {"selected": True, "text": first, "value": first}
                    v["options"] = [{"selected": True, "text": first, "value": first}]

    # also map any ${DS_*} import inputs to uids
    for i in d.get("__inputs", []):
        if i.get("type") == "datasource":
            uid, _ = UID_BY_PLUGIN.get(i.get("pluginId"), (None, None))
            if uid:
                placeholders[i["name"]] = uid

    # Drop the always-empty panels (top-level and nested inside collapsed rows).
    drop = DROP_PANELS.get(name, set())
    if drop:
        keep = lambda p: (p.get("title") or "") not in drop
        d["panels"] = [p for p in d.get("panels", []) if keep(p)]
        for p in d["panels"]:
            if p.get("panels"):
                p["panels"] = [sp for sp in p["panels"] if keep(sp)]

    d.pop("__inputs", None)
    d.pop("__requires", None)
    d["id"] = None  # let Grafana assign a local id; keep the stable `uid`

    s = json.dumps(d, indent=2)
    for ph, uid in placeholders.items():
        s = s.replace("${%s}" % ph, uid)
    leftover = re.findall(r"\$\{DS_[^}]*\}|\$\{ds_[^}]*\}", s)
    assert not leftover, (name, set(leftover))
    json.loads(s)  # must remain valid JSON
    return s, resolved


def main():
    for name, gnet_id in DASHBOARDS.items():
        s, resolved = adapt(name, fetch(gnet_id))
        with open("%s/%s.json" % (OUTDIR, name), "w") as fh:
            fh.write(s + "\n")
        print("%-20s defaults=%s" % (name, resolved or "{includeAll->All}"))


if __name__ == "__main__":
    main()
