"""The architecture figure: how a request reaches a workload, and on what it runs.

The shape is fixed — these edges live in role templates and Traefik middleware, not in
``containers_list``, so they cannot be derived. Every label, address, count and status
colour on it is read from the model.

Two entry points, because the figure has two homes. ``diagram_view`` is the page
figure, caption included. ``diagram_svg_fragment`` is the bare ``<svg>`` element, which
is what ``render_svg`` wraps into a standalone document for a Markdown page.
"""

import sys as _sys
from pathlib import Path as _Path
from typing import Any

# `infra_map` is a namespace package under `scripts/`, so reaching a sibling by package
# name needs `scripts/` on sys.path: a directly-invoked script gets only its own directory,
# and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from infra_map.style import e

CAPTION = (
    "How a request reaches a workload, and what it runs on. Box outlines carry live "
    "status; every address, hostname and count is read from the inventory and the "
    "cluster at render time."
)


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


def diagram_svg_fragment(model: dict) -> str:
    """The diagram as a bare ``<svg>`` element, with no figure wrapper and no caption."""
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

    node_boxes: list[dict[str, Any]] = cluster["nodes"] or [
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
    # inventory classifies it as a role with no workload of its own. A healthy
    # storage plane must not read as anything but what the volumes say.
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
            state, detail = "healthy", str(target["url"])
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

    return (
        f'<svg class="dg" viewBox="0 0 1140 900" role="img" aria-label="{e(CAPTION)}">'
        f"{''.join(parts)}</svg>"
    )


def diagram_view(model: dict) -> str:
    """The diagram as a page figure: the ``<svg>`` element plus its caption."""
    return (
        '<figure class="diagram">'
        f"{diagram_svg_fragment(model)}"
        f"<figcaption>{e(CAPTION)}</figcaption></figure>"
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
