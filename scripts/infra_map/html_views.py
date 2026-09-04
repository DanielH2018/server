"""The HTML host panels: one row per service, one panel per host.

Reads the host dicts ``infra_map.model`` builds and returns markup. Every value that
comes from the inventory goes through ``e`` — the panels carry hostnames, image tags
and error strings this module does not control.
"""

import sys as _sys
from pathlib import Path as _Path

# `infra_map` is a namespace package under `scripts/`, so reaching a sibling by package
# name needs `scripts/` on sys.path: a directly-invoked script gets only its own directory,
# and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from infra_map.style import STATUS_LABELS, e


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


def host_panel(host: dict) -> str:
    """One host's card: its platform, its counts, and a row per service on it."""
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
