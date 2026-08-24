"""Rendering: turn the reconciled model into one self-contained HTML page.

Reads the model dict ``infra_map_model`` builds and nothing else — no repo, no
cluster — so a page can be re-rendered from a saved model. All CSS and the SVG
diagram are inlined, because the output has to open over ``file://``.
"""

from __future__ import annotations

import html
from typing import Any

from infra_map_common import PAGE_REFRESH_SECONDS

# Catppuccin Mocha, matching the terminal these pages are generated from.
STYLE = """
:root {
  --base: #1e1e2e; --mantle: #181825; --crust: #11111b;
  --surface0: #313244; --surface1: #45475a; --surface2: #585b70;
  --text: #cdd6f4; --subtext0: #a6adc8; --overlay0: #6c7086;
  --blue: #89b4fa; --green: #a6e3a1; --yellow: #f9e2af; --red: #f38ba8;
  --mauve: #cba6f7; --peach: #fab387; --teal: #94e2d5; --lavender: #b4befe;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2.5rem 1.5rem 4rem;
  background: var(--base); color: var(--text);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  line-height: 1.6; font-size: 15px;
}
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 1.85rem; font-weight: 650; letter-spacing: -0.02em; margin: 0 0 .35rem; }
h2 {
  font-size: 1.05rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  color: var(--subtext0); margin: 3rem 0 1rem; padding-bottom: .5rem;
  border-bottom: 1px solid var(--surface1);
}
h3 { font-size: 1.15rem; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
p { margin: 0 0 1rem; }
.lede { color: var(--subtext0); max-width: 68ch; }
.meta { color: var(--overlay0); font-size: .85rem; font-family: var(--mono); }
code, .mono { font-family: var(--mono); font-size: .85em; }

.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: .75rem; margin: 1.75rem 0 0; }
.kpi { background: var(--mantle); border: 1px solid var(--surface0); border-radius: 10px; padding: .9rem 1rem; }
.kpi .n { font-size: 1.9rem; font-weight: 650; line-height: 1.1; font-variant-numeric: tabular-nums; }
.kpi .l { font-size: .78rem; color: var(--overlay0); text-transform: uppercase; letter-spacing: .05em; margin-top: .15rem; }
.n.good { color: var(--green); } .n.warn { color: var(--yellow); }
.n.bad { color: var(--red); } .n.info { color: var(--blue); } .n.alt { color: var(--mauve); }

.hosts { display: grid; grid-template-columns: repeat(auto-fit, minmax(430px, 1fr)); gap: 1.25rem; }
.host { background: var(--mantle); border: 1px solid var(--surface0); border-radius: 12px; overflow: hidden; }
.host-head { padding: 1.1rem 1.25rem; border-bottom: 1px solid var(--surface0); background: var(--crust); }
.host-head .row { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.host-sub { color: var(--overlay0); font-size: .85rem; font-family: var(--mono); margin-top: .3rem; }
.host-body { padding: .5rem .6rem 1rem; }

.svc { display: flex; align-items: flex-start; gap: .7rem; padding: .5rem .65rem; border-radius: 8px; }
.svc:hover { background: var(--surface0); }
.svc + .svc { border-top: 1px solid rgba(69,71,90,.45); }
.svc-main { flex: 1; min-width: 0; }
.svc-name { font-weight: 550; font-family: var(--mono); font-size: .92rem; }
.svc-detail { color: var(--overlay0); font-size: .8rem; overflow-wrap: anywhere; }
.svc-tags { display: flex; gap: .3rem; flex-wrap: wrap; margin-top: .25rem; }

.dot { width: 9px; height: 9px; border-radius: 50%; flex: 0 0 auto; margin-top: .45rem; box-shadow: 0 0 0 2px var(--mantle); }
.dot.healthy { background: var(--green); } .dot.degraded { background: var(--yellow); }
.dot.down, .dot.missing { background: var(--red); }
.dot.job { background: var(--teal); } .dot.companion { background: var(--blue); }
.dot.undeclared { background: var(--mauve); } .dot.unknown { background: var(--overlay0); }

.tag { font-size: .68rem; font-family: var(--mono); padding: .1rem .4rem; border-radius: 4px;
       background: var(--surface0); color: var(--subtext0); border: 1px solid var(--surface1); }
.tag.auth { background: var(--lavender); color: var(--crust); border-color: var(--lavender); }
.tag.route { background: var(--blue); color: var(--crust); border-color: var(--blue); }
.tag.net { background: transparent; color: var(--teal); border-color: var(--surface2); }
.tag.ns { background: transparent; color: var(--peach); border-color: var(--surface2); }
.tag.node { background: transparent; color: var(--lavender); border-color: var(--surface2); }

.legend { display: flex; gap: 1.1rem; flex-wrap: wrap; margin: 1rem 0 0; font-size: .82rem; color: var(--subtext0); }
.legend span { display: flex; align-items: center; gap: .4rem; }
.legend .dot { margin-top: 0; }

.warn-box { background: rgba(243,139,168,.1); border: 1px solid var(--red); border-radius: 8px;
            padding: .7rem .9rem; margin: .75rem 0 0; color: var(--text); font-size: .87rem; }

figure.diagram { margin: 0; }
.dg { width: 100%; height: auto; display: block; }
.dg .box { fill: var(--mantle); stroke: var(--surface2); stroke-width: 1.4; }
.dg .plane { fill: rgba(49,50,68,.3); stroke: var(--surface1); stroke-width: 1.2; stroke-dasharray: 6 5; }
.dg .box.s-healthy { stroke: var(--green); }
.dg .box.s-degraded { stroke: var(--yellow); }
.dg .box.s-down, .dg .box.s-missing { stroke: var(--red); }
.dg .box.s-job { stroke: var(--teal); }
.dg .box.s-unknown { stroke: var(--overlay0); stroke-dasharray: 4 3; }
.dg .t-title { fill: var(--text); font-size: 13px; font-weight: 550;
                font-family: system-ui, -apple-system, sans-serif; }
.dg .t-sub { fill: var(--subtext0); font-size: 11px; font-family: var(--mono); }
.dg .t-lane { fill: var(--overlay0); font-size: 11px; letter-spacing: .1em;
              text-transform: uppercase; font-weight: 600;
              font-family: system-ui, -apple-system, sans-serif; }
.dg .t-edge { fill: var(--overlay0); font-size: 10.5px; font-family: var(--mono); }
.dg .edge { stroke: var(--surface2); stroke-width: 1.5; fill: none; }
.dg .edge.bypass { stroke: var(--peach); stroke-dasharray: 7 5; }
.dg .t-edge.bypass { fill: var(--peach); }
.dg .arrowhead { fill: var(--surface2); }
figcaption { color: var(--overlay0); font-size: .84rem; margin-top: .9rem; max-width: 78ch; }

.grps { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 1rem; }
.grp { background: var(--mantle); border: 1px solid var(--surface0); border-radius: 10px; padding: .9rem 1rem; }
.grp h4 { margin: 0 0 .6rem; font-size: .9rem; font-weight: 600; display: flex;
          justify-content: space-between; gap: .5rem; }
.grp-n { color: var(--overlay0); font-family: var(--mono); font-size: .8rem; font-weight: 400; }
.grp ul { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: .3rem; }
.grp li { display: flex; align-items: center; gap: .45rem; font-family: var(--mono); font-size: .8rem; }
.grp li .dot { margin-top: 0; }

table { width: 100%; border-collapse: collapse; font-size: .85rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--surface0); vertical-align: top; }
th { color: var(--overlay0); font-size: .74rem; text-transform: uppercase; letter-spacing: .05em; font-weight: 600; }
td.mono { font-family: var(--mono); }
.scroll { overflow-x: auto; }
details > summary { cursor: pointer; color: var(--subtext0); font-size: .9rem; padding: .4rem 0; }
footer { margin-top: 3rem; padding-top: 1.25rem; border-top: 1px solid var(--surface0);
         color: var(--overlay0); font-size: .82rem; }
"""

STATUS_LABELS = {
    "healthy": "Healthy",
    "degraded": "Degraded",
    "down": "Down",
    "missing": "Missing",
    "job": "Scheduled job",
    "companion": "Role companion",
    "undeclared": "Undeclared",
    "unknown": "Unknown",
}


def e(value: Any) -> str:
    """Escape a value for HTML text content."""
    return html.escape("" if value is None else str(value))


def _service_row(service: dict) -> str:
    status = service["status"]
    tags = []
    if service["hostname"]:
        target = service["hostname"]
        tags.append(f'<span class="tag route">{e(target)}:{e(service["port"])}</span>')
    elif service["port"]:
        tags.append(f'<span class="tag">:{e(service["port"])}</span>')
    if service["authelia"]:
        tags.append('<span class="tag auth">SSO</span>')
    if service.get("namespace"):
        tags.append(f'<span class="tag ns">ns/{e(service["namespace"])}</span>')
    for node in service.get("nodes") or []:
        tags.append(f'<span class="tag node">{e(node)}</span>')
    for net in service["networks"]:
        tags.append(f'<span class="tag net">{e(net)}</span>')
    if not service["declared"]:
        tags.append('<span class="tag">not in inventory</span>')

    detail = service["detail"] or STATUS_LABELS.get(status, status)
    return (
        f'<div class="svc">'
        f'<span class="dot {e(status)}" title="{e(STATUS_LABELS.get(status, status))}"></span>'
        f'<div class="svc-main">'
        f'<div class="svc-name">{e(service["name"])}</div>'
        f'<div class="svc-detail">{e(STATUS_LABELS.get(status, status))} &middot; {e(detail)}</div>'
        f'<div class="svc-tags">{"".join(tags)}</div>'
        f"</div></div>"
    )


def _host_panel(host: dict) -> str:
    counts = host["counts"]
    summary_bits = [
        f"{counts.get(key, 0)} {STATUS_LABELS[key].lower()}"
        for key in ("healthy", "degraded", "down", "missing", "undeclared", "unknown")
        if counts.get(key)
    ]
    platform_label = host["role"] or (
        "k3s / Kubernetes" if host["platform"] == "k8s" else "Docker Compose"
    )
    node = host.get("node")
    if host["platform"] == "k8s":
        # Say what is *running here*, not what declares it — the inventory
        # declares every k8s service under daniel-box, and a "0 declared" line
        # on daniel-server reads as an idle box while it carries half the pods.
        scope = f"{len(host['services'])} services running here"
        if node:
            scope += f" &middot; {node['pods']} pods"
    else:
        scope = f"{host['declared_count']} declared"
    warn = ""
    if not host["reachable"]:
        warn = (
            f'<div class="warn-box"><strong>Live state unavailable</strong> — showing '
            f"declared inventory only. {e(host['error'])}</div>"
        )
    rows = "".join(_service_row(s) for s in host["services"])
    return (
        f'<section class="host"><div class="host-head">'
        f'<div class="row"><h3>{e(host["name"])}</h3>'
        f'<span class="meta">{e(platform_label)}</span></div>'
        f'<div class="host-sub">{e(host["ip"])} &middot; {scope} '
        f"&middot; {host['routed_count']} routed &middot; {host['authelia_count']} SSO-gated</div>"
        f'<div class="host-sub">{" &middot; ".join(e(bit) for bit in summary_bits)}</div>'
        f"{warn}</div>"
        f'<div class="host-body">{rows}</div></section>'
    )


# Functional grouping for the workload strip under the diagram. This is the one
# piece of the page that is a hand-kept list rather than derived, so anything
# unlisted falls into "Other" and stays visible — a new service shows up as
# ungrouped instead of silently vanishing from the page.
SERVICE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Edge & identity",
        ("traefik", "authelia", "crowdsec", "cloudflare-ddns", "pihole", "headlamp"),
    ),
    (
        "Media",
        (
            "jellyfin",
            "sonarr",
            "radarr",
            "bazarr",
            "prowlarr",
            "qbittorrent",
            "tdarr",
            "configarr",
            "janitorr",
            "media-volume",
            "seed-volume",
        ),
    ),
    (
        "Home automation",
        ("home-assistant", "zigbee2mqtt", "mosquitto", "ical-proxy", "peanut", "nut"),
    ),
    (
        "Observability",
        (
            "uptime-kuma",
            "loki-homelab",
            "claude-otel",
            "node-exporter",
            "scrutiny",
            "monitor-bridge",
            "autofix-bridge",
            "healthchecks",
            "speedtest",
            "rollout-drain",
        ),
    ),
    (
        "Apps & tooling",
        (
            "freshrss",
            "karakeep",
            "littlelink",
            "bento-pdf",
            "homepage",
            "n8n",
            "n8n-images",
            "code-server",
            "livesync",
            "homelab-mcp",
            "registry",
            "image-builder",
        ),
    ),
    ("Games", ("terraria", "terraria-stats", "valheim", "valheim-stats")),
    (
        "Storage & backup",
        ("longhorn-ui", "pi-peer-backup", "dri-device-plugin"),
    ),
)


def group_services(model: dict) -> list[dict]:
    """Bucket every service into a functional group for the diagram strip."""
    by_group: dict[str, list[dict]] = {name: [] for name, _ in SERVICE_GROUPS}
    by_group["Pi · LAN-only"] = []
    by_group["Other"] = []
    lookup = {name: group for group, names in SERVICE_GROUPS for name in names}

    for service in model["services"]:
        if service["platform"] == "docker":
            by_group["Pi · LAN-only"].append(service)
        else:
            by_group[lookup.get(service["name"], "Other")].append(service)

    groups = []
    for name, services in by_group.items():
        if not services:
            continue
        groups.append(
            {
                "name": name,
                "services": sorted(services, key=lambda s: s["name"]),
                "healthy": sum(1 for s in services if s["status"] == "healthy"),
            }
        )
    return groups


def _svg_box(
    x: int,
    y: int,
    w: int,
    h: int,
    title: str,
    subtitle: str = "",
    status: str = "",
    css: str = "",
) -> str:
    """One labelled node of the diagram, optionally tinted by live status."""
    cx = x + w // 2
    classes = " ".join(
        part for part in ("box", css, f"s-{status}" if status else "") if part
    )
    if subtitle:
        title_y, sub_y = y + h // 2 - 3, y + h // 2 + 15
        text = (
            f'<text class="t-title" x="{cx}" y="{title_y}" text-anchor="middle">{e(title)}</text>'
            f'<text class="t-sub" x="{cx}" y="{sub_y}" text-anchor="middle">{e(subtitle)}</text>'
        )
    else:
        text = f'<text class="t-title" x="{cx}" y="{y + h // 2 + 5}" text-anchor="middle">{e(title)}</text>'
    return f'<rect class="{classes}" x="{x}" y="{y}" width="{w}" height="{h}" rx="9"/>{text}'


def _svg_edge(
    points: str, label: str = "", label_xy: tuple[int, int] = (0, 0), css: str = ""
) -> str:
    classes = " ".join(part for part in ("edge", css) if part)
    edge = (
        f'<polyline class="{classes}" points="{points}" marker-end="url(#dg-arrow)"/>'
    )
    if label:
        x, y = label_xy
        label_css = "t-edge bypass" if "bypass" in css else "t-edge"
        edge += f'<text class="{label_css}" x="{x}" y="{y}">{e(label)}</text>'
    return edge


def _service_status(model: dict, name: str) -> str:
    """Live status of one service by name, for tinting a diagram box."""
    for host in model["hosts"]:
        for service in host["services"]:
            if service["name"] == name:
                return service["status"]
    return "unknown"


def _diagram_view(model: dict) -> str:
    """The architecture figure: how a request reaches a workload, and on what.

    The shape is fixed — these edges live in role templates and Traefik
    middleware, not in ``containers_list``, so they cannot be derived. Every
    label, address, count and status colour on it is read from the model.
    """
    ep = model["endpoints"]
    cluster = model["cluster"]
    box = next((h for h in model["hosts"] if h["name"] == "daniel-box"), None)
    pi = next((h for h in model["hosts"] if h["name"] == "daniel-pi"), None)
    routed = box["routed_count"] if box else 0
    gated = box["authelia_count"] if box else 0
    domain = model["domain"] or "the domain"

    parts = [
        '<defs><marker id="dg-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path class="arrowhead" d="M 0 0 L 10 5 L 0 10 z"/></marker></defs>'
    ]

    # Request path, top to bottom.
    parts.append(_svg_box(250, 26, 200, 48, "Internet"))
    parts.append(_svg_box(490, 26, 200, 48, "LAN clients"))
    parts.append(
        _svg_box(
            250,
            108,
            200,
            52,
            "Cloudflare DNS",
            domain,
            _service_status(model, "cloudflare-ddns"),
        )
    )
    parts.append(
        _svg_box(
            490,
            108,
            200,
            52,
            "Pi-hole → Unbound",
            f"{ep['dns_vip']}:53",
            _service_status(model, "pihole"),
        )
    )
    parts.append(
        _svg_box(
            330,
            196,
            480,
            52,
            "Router :80/:443 → MetalLB ingress VIP",
            ep["ingress_vip"],
        )
    )
    parts.append(
        _svg_box(
            330,
            278,
            480,
            58,
            "Traefik  ·  CrowdSec bouncer + AppSec WAF",
            f"{routed} routed hostnames",
            _service_status(model, "traefik"),
        )
    )
    parts.append(
        _svg_box(
            330,
            372,
            480,
            58,
            "Authelia  ·  forwardAuth SSO",
            f"{gated} of {routed} routes gated",
            _service_status(model, "authelia"),
        )
    )

    # k8s_public_route decides whether IngressRoutes *match* the public hostname,
    # not whether the router forwards 80/443 here. Say only the first.
    public_label = (
        f"routes match *.{domain}" if ep["public_routes"] else "LAN-only routes"
    )
    parts.append(_svg_edge("350,74 350,108", "A/AAAA", (356, 96)))
    parts.append(_svg_edge("590,74 590,108", "DHCP-assigned resolver", (596, 96)))
    parts.append(_svg_edge("350,160 350,178 430,178 430,196", public_label, (250, 190)))
    parts.append(
        _svg_edge("590,160 590,178 670,178 670,196", f"*.local.{domain}", (676, 190))
    )
    parts.append(_svg_edge("570,248 570,278", ":80/:443", (578, 266)))
    parts.append(_svg_edge("570,336 570,372", "forwardAuth", (578, 358)))
    parts.append(
        _svg_edge("570,430 570,470", "proxies to ClusterIP Services", (578, 454))
    )

    # The LoadBalancer VIPs that never touch Traefik — raw TCP, and the reason a
    # Traefik outage does not take Jellyfin or MQTT with it.
    parts.append(_svg_edge("690,50 1120,50 1120,540 1100,540", css="bypass"))
    parts.append(
        '<text class="t-edge bypass" x="704" y="38">LoadBalancer VIPs — bypass Traefik</text>'
    )
    parts.append(
        f'<text class="t-edge bypass" x="704" y="66">Jellyfin {e(ep["jellyfin_vip"])}'
        f" · MQTT {e(ep['mqtt_vip'])}</text>"
    )

    # Cluster plane.
    volumes = cluster["volumes"]
    plane_sub = f"{cluster['pod_count']} pods"
    if volumes is not None:
        plane_sub += f"  ·  {volumes} Longhorn volumes"
    parts.append(
        '<rect class="plane" x="40" y="470" width="1060" height="200" rx="14"/>'
    )
    parts.append('<text class="t-lane" x="62" y="497">k3s cluster</text>')
    parts.append(
        f'<text class="t-sub" x="1078" y="497" text-anchor="end">{e(plane_sub)}</text>'
    )

    node_boxes = cluster["nodes"] or [
        {
            "name": h["name"],
            "ip": h["ip"],
            "ready": False,
            "pods": 0,
            "roles": [],
            "version": "",
        }
        for h in model["hosts"]
        if h["platform"] == "k8s"
    ]
    for index, node in enumerate(node_boxes[:2]):
        roles = ", ".join(node["roles"]) or "agent"
        # Only claim NotReady when the query actually answered. An uncollected
        # cluster and a failed node look identical in this dict, and painting the
        # second when it was the first is the false alarm that teaches a reader
        # to stop believing red.
        if not cluster["ok"]:
            state = "unknown"
        else:
            state = "healthy" if node["ready"] else "down"
        parts.append(
            _svg_box(
                70 + index * 510,
                520,
                490,
                110,
                f"{node['name']}  ·  {roles}",
                (
                    f"{node['ip']}  ·  {node['pods']} pods  ·  {node['version']}"
                    if cluster["ok"]
                    else f"{node['ip']}  ·  not collected"
                ),
                state,
            )
        )

    # Storage and the backup chain. Tinted by whether the volumes could be read,
    # not by the longhorn-ui service: that entry declares only an IngressRoute,
    # its Deployment belongs to the Longhorn chart in longhorn-system, and the
    # name lookup in ns/homelab therefore misses it and reports "missing". A
    # healthy storage plane must not read as red because a route lookup missed.
    parts.append(
        _svg_box(
            40,
            706,
            330,
            76,
            "Longhorn",
            f"ns/{ep['longhorn_namespace']}"
            + (f"  ·  {volumes} volumes" if volumes is not None else ""),
            "healthy" if volumes is not None else "unknown",
        )
    )
    # Same rule as the nodes, and it matters more here: "disarmed" is a real and
    # deliberate state in this repo, so a failed query must not be able to
    # announce it. No targets collected means unknown, not unarmed.
    targets = cluster["backup_targets"] or [
        {"name": "default", "url": "", "armed": False, "available": False}
    ]
    for index, target in enumerate(targets[:2]):
        if not cluster["ok"] or not cluster["backup_targets"]:
            state, detail = "unknown", "not collected"
        elif not target["armed"]:
            state, detail = "missing", "disarmed — no backup target URL"
        elif target["available"]:
            state, detail = "healthy", target["url"]
        else:
            state, detail = "down", f"unavailable — {target['url']}"
        parts.append(
            _svg_box(
                440,
                690 + index * 80,
                300,
                60,
                f"BackupTarget/{target['name']}",
                detail,
                state,
            )
        )
        parts.append(
            _svg_edge(f"370,744 405,744 405,{720 + index * 80} 440,{720 + index * 80}")
        )

    # The Pi: its own plane, reached from the LAN and never through the cluster edge.
    pi_services = ", ".join(s["name"] for s in (pi["services"] if pi else [])) or "none"
    parts.append(
        '<rect class="plane" x="790" y="690" width="310" height="190" rx="14"/>'
    )
    parts.append('<text class="t-lane" x="812" y="717">daniel-pi · Docker</text>')
    parts.append(
        f'<text class="t-sub" x="812" y="740">{e(pi["ip"] if pi else "")} · LAN-only, no Traefik route</text>'
    )
    parts.append(
        '<text class="t-sub" x="812" y="766">WireGuard peers → wg-easy :51820/udp</text>'
    )
    # Four lines is what fits inside the plane; a longer list is elided rather
    # than drawn past the edge, and the host panel below carries all of it.
    lines = _wrap(pi_services, 34)
    if len(lines) > 4:
        lines = lines[:3] + [f"… +{len(lines) - 3} more lines"]
    for index, chunk in enumerate(lines):
        parts.append(
            f'<text class="t-title" x="812" y="{800 + index * 20}">{e(chunk)}</text>'
        )

    caption = (
        "How a request reaches a workload, and what it runs on. Box outlines carry live "
        "status; every address, hostname and count is read from the inventory and the "
        "cluster at render time."
    )
    return (
        '<figure class="diagram">'
        f'<svg class="dg" viewBox="0 0 1140 900" role="img" aria-label="{e(caption)}">'
        f"{''.join(parts)}</svg>"
        f"<figcaption>{e(caption)}</figcaption></figure>"
    )


def _wrap(text: str, width: int) -> list[str]:
    """Greedy wrap — SVG text has no flow, so lines are placed by hand."""
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _groups_view(model: dict) -> str:
    """A status-coloured chip per service, grouped by what it is for."""
    columns = []
    for group in group_services(model):
        chips = "".join(
            f'<li><span class="dot {e(s["status"])}" title="{e(STATUS_LABELS.get(s["status"], s["status"]))}">'
            f"</span>{e(s['name'])}</li>"
            for s in group["services"]
        )
        columns.append(
            f'<div class="grp"><h4>{e(group["name"])} '
            f'<span class="grp-n">{group["healthy"]}/{len(group["services"])}</span></h4>'
            f"<ul>{chips}</ul></div>"
        )
    return f'<div class="grps">{"".join(columns)}</div>'


def _table_view(model: dict) -> str:
    rows = []
    for service in model["services"]:
        # "Runs on" is where the pods landed; a k8s service with none falls back
        # to the host that declares it.
        runs_on = ", ".join(service.get("nodes") or []) or service.get("host", "")
        rows.append(
            "<tr>"
            f'<td class="mono">{e(service["name"])}</td>'
            f'<td class="mono">{e(runs_on)}</td>'
            f"<td>{e(service['platform'])}</td>"
            f"<td>{e(STATUS_LABELS.get(service['status'], service['status']))}</td>"
            f'<td class="mono">{e(service["hostname"] or "—")}</td>'
            f'<td class="mono">{e(service["image"] or "—")}</td>'
            f"<td>{e(service['detail'] or '—')}</td>"
            "</tr>"
        )
    return (
        "<details><summary>Full service table (sortable by eye, copy-pasteable)</summary>"
        '<div class="scroll"><table><thead><tr>'
        "<th>Service</th><th>Runs on</th><th>Platform</th><th>Status</th>"
        "<th>Hostname</th><th>Image</th><th>Detail</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></details>"
    )


def render_svg(model: dict) -> str:
    """The architecture diagram as a standalone SVG document, for embedding in Markdown.

    _diagram_view already draws the whole figure, but two things stop its <svg> element
    standing alone:

    * Its fills and strokes resolve against the page-level STYLE block. Embedded in a
      Markdown page there is no such block, so the diagram renders as unstyled black
      shapes -- which reads as a broken diagram rather than a missing stylesheet.
      Inlining the CSS as a <style> child makes the element carry its own appearance.
    * A bare <svg> works inside HTML, but a .svg file served on its own is parsed as
      XML and needs xmlns declared.

    The drawing code is untouched. The <figcaption> is dropped: the Markdown page around
    the image carries that prose, and a caption baked into the image cannot be edited.
    """
    figure = _diagram_view(model)
    start = figure.index("<svg")
    end = figure.index("</svg>") + len("</svg>")
    svg = figure[start:end]

    open_tag_end = svg.index(">") + 1
    open_tag = svg[:open_tag_end].replace(
        "<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1
    )
    # CDATA because the stylesheet contains '>' in descendant selectors, which is not
    # legal bare text in XML.
    # Trailing newline: the end-of-file-fixer prek hook adds one otherwise, and a
    # generated file a hook keeps rewriting fails the docs-refresh cron's commit on
    # every run.
    return f"{open_tag}<style><![CDATA[{STYLE}]]></style>{svg[open_tag_end:]}\n"


def render_html(model: dict) -> str:
    """Render the model to a single self-contained HTML document."""
    totals = model["totals"]
    kpis = [
        ("info", totals["services"], "Services"),
        ("good", totals["healthy"], "Healthy"),
        ("warn", totals["degraded"], "Degraded"),
        ("bad", totals["down"], "Down / missing"),
        ("alt", totals["undeclared"], "Undeclared"),
        ("info", model["cluster"]["pod_count"], "Pods running"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="n {css}">{value}</div><div class="l">{e(label)}</div></div>'
        for css, value, label in kpis
    )
    legend = "".join(
        f'<span><span class="dot {key}"></span>{label}</span>'
        for key, label in STATUS_LABELS.items()
    )
    hosts_html = "".join(_host_panel(h) for h in model["hosts"])

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{PAGE_REFRESH_SECONDS}">
<title>Homelab infrastructure — daniel-box &amp; daniel-server</title>
<style>{STYLE}</style>
</head><body><div class="wrap">
<h1>Homelab infrastructure</h1>
<p class="lede">Declared state from <code>ansible/inventory/host_vars/</code>, overlaid with
live state from <code>kubectl</code> and <code>docker ps</code>. Regenerated on a timer, so
edits to the inventory and drift in the running fleet both show up here on their own.</p>
<p class="meta">Generated {e(model["generated_at"])} &middot; page reloads every {PAGE_REFRESH_SECONDS // 60} min</p>
<div class="kpis">{kpi_html}</div>
<div class="legend">{legend}</div>

<h2>Architecture</h2>
{_diagram_view(model)}

<h2>Workloads by function</h2>
{_groups_view(model)}

<h2>Hosts</h2>
<div class="hosts">{hosts_html}</div>

<h2>All services</h2>
{_table_view(model)}

<footer>
Sources: <code>ansible/inventory/</code> for declared state; live state from
<code>kubectl</code> on daniel-box (deployments, nodes, pods, Longhorn volumes and
backup targets) and one <code>docker ps</code> over ssh to daniel-pi.
The diagram's <em>shape</em> is fixed in <code>scripts/gen_infra_map.py</code> — those edges
live in role templates, not in the inventory — while its labels, counts and status
colours are read at render time.
Regenerate with <code>uv run python scripts/gen_infra_map.py</code>.
</footer>
</div></body></html>
"""
