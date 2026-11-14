"""Rendered-manifest property rows over the `-monitoring` IngressRoutes.

`test_rendered_properties.py` runs these rows beside its own; the rules for what makes a row are
in its docstring. The forward-auth test exempts every `-monitoring` route from Authelia by name,
so these rows hold the shape that makes that exemption safe.
"""

import re

from _helpers import ALL_VARS, load_yaml
from _property_table import Property

_CLIENT_IP = re.compile(r"ClientIP\(`([^`/]+)/(\d+)`\)")
# The widest grant a monitoring route may carry; lan_subnet is a /24.
_WIDEST_PREFIX = 16
_LOCAL = f".local.{load_yaml(ALL_VARS)['domain']}"


def _monitoring_routes(role: str, tpl: str, doc: dict):
    name = doc.get("metadata", {}).get("name", "")
    if doc.get("kind") == "IngressRoute" and name.endswith("-monitoring"):
        for route in doc["spec"]["routes"]:
            yield name, route


def _top_level_split(expr: str, op: str) -> list[str]:
    """`expr` cut at each `op` outside parentheses and backtick literals."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(expr):
        ch = expr[i]
        if ch == "`":
            i = expr.index("`", i + 1)
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and expr.startswith(op, i):
            parts.append(expr[start:i].strip())
            start = i + len(op)
            i = start
            continue
        i += 1
    parts.append(expr[start:].strip())
    return parts


def _wrapped_whole(expr: str) -> bool:
    if not (expr.startswith("(") and expr.endswith(")")):
        return False
    depth = 0
    for i, ch in enumerate(expr):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth == 0 and i < len(expr) - 1:
            return False
    return True


def _unwrap(expr: str) -> str:
    while _wrapped_whole(expr):
        expr = expr[1:-1].strip()
    return expr


def _monitoring_offence(role: str, route: dict) -> str | None:
    match = route.get("match", "")
    hosts = re.findall(r"Host\(`([^`]+)`\)", match)
    if not hosts or any(not h.endswith(_LOCAL) for h in hosts):
        return f"matches {hosts}, not only *{_LOCAL} hosts"
    conjuncts = _top_level_split(match, "&&")
    if any(len(_top_level_split(c, "||")) > 1 for c in conjuncts):
        return "a top-level || lets a request skip the ClientIP clause"
    for conjunct in conjuncts:
        grants = [
            _CLIENT_IP.fullmatch(t) for t in _top_level_split(_unwrap(conjunct), "||")
        ]
        if all(g is not None for g in grants):
            wide = [
                g[0] for g in grants if g is not None and int(g[2]) < _WIDEST_PREFIX
            ]
            if wide:
                return f"ClientIP grant(s) {wide} are wider than /{_WIDEST_PREFIX}"
            return None
    return "no top-level && clause made only of ClientIP grants"


MONITORING_PROPERTIES = (
    Property(
        name="monitoring-route-is-lan-host-and-clientip-confined",
        reason=(
            "test_every_authed_service_carries_forward_auth_and_rate_limit exempts every "
            "`-monitoring` IngressRoute from Authelia. That holds only while each one answers a "
            ".local. host and ANDs a narrow ClientIP clause; a public Host, a top-level ||, or a "
            "0.0.0.0/0 grant would leave an unauthenticated route open."
        ),
        select=_monitoring_routes,
        offence=_monitoring_offence,
        red=(
            "ical-proxy",
            {"match": "Host(`x.example.com`) && (ClientIP(`10.0.0.0/24`))"},
        ),
        green=(
            "ical-proxy",
            {
                "match": f"Host(`x{_LOCAL}`) && PathPrefix(`/metrics`) && "
                "(ClientIP(`10.0.0.5/32`) || ClientIP(`10.42.0.0/31`))"
            },
        ),
        more_red=(
            ("ical-proxy", {"match": f"Host(`x{_LOCAL}`) || ClientIP(`10.0.0.5/32`)"}),
            ("ical-proxy", {"match": f"Host(`x{_LOCAL}`) && (ClientIP(`0.0.0.0/0`))"}),
            ("ical-proxy", {"match": f"Host(`x{_LOCAL}`) && PathPrefix(`/`)"}),
        ),
        must_find=frozenset({"prometheus-monitoring", "loki-homelab-monitoring"}),
    ),
)
