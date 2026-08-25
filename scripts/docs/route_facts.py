#!/usr/bin/env python3
"""Shared route facts for the reference generators.

WHY THIS IS SEPARATE. service_catalog.py and gen_reference_networking.py both have to
answer "is this service's route public or LAN-only", and both build the FQDN the same way.
Deriving that twice means the two pages can disagree about the same service, which is worse
than either being wrong on its own -- a reader has no way to tell which one to believe.

THE DOMAIN IS NOT RESOLVED HERE, ON PURPOSE. `ingressroute.yml.j2` builds the hostname as
"{{ hostname }}.local.{{ domain }}", and `domain` is SOPS-sourced with no static default, so
a generator that parses the tree statically cannot know it. Rather than print a label and
leave the reader to assemble a URL by hand, `fqdn()` emits a span the docs site resolves in
the browser: docs/assets/fqdn-links.js reads the domain off the URL the reader is already on
and rewrites the span into a link. That also picks the right TIER -- a reader on the LAN name
gets LAN links, a reader on the public name gets public ones -- which a baked FQDN could not
do. Without JavaScript the span still reads as the placeholder text it always did.

STATIC PARSING ONLY: yaml.safe_load over the inventory, plain regex over template text.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
GROUP_VARS = REPO / "ansible" / "inventory" / "group_vars" / "all.yml"
K8S_ROLES = REPO / "ansible" / "roles" / "k8s"

# `public=false` in a role's own macro call opts the service out of the public Host rule
# whatever k8s_public_route says. See ansible/templates/ingressroute.yml.j2.
_PUBLIC_FALSE_RE = re.compile(r"public\s*=\s*false")

LAN = "lan"
PUBLIC = "public"


def ingressroute_templates(role_dir: Path) -> list[Path]:
    """Every ingressroute template in a role, or [] if it declares no route."""
    templates = role_dir / "templates"
    if not templates.is_dir():
        return []
    return sorted(p for p in templates.glob("*.j2") if "ingressroute" in p.name)


def public_route_enabled(group_vars: Path = GROUP_VARS) -> bool:
    """Whether the cluster-wide public Host rule is on.

    Reads the plaintext group_vars rather than assuming. A missing or unreadable file is
    treated as OFF, because claiming a route is public when it is not sends a reader to a
    name that does not resolve -- the safer error is under-reporting reachability.
    """
    try:
        loaded: Any = yaml.safe_load(group_vars.read_text())
    except OSError, yaml.YAMLError:
        return False
    return bool(isinstance(loaded, dict) and loaded.get("k8s_public_route"))


def reachability(role_dir: Path, group_vars: Path = GROUP_VARS) -> str:
    """PUBLIC or LAN for a role that has a route. Callers check for a route first."""
    text = "\n".join(p.read_text() for p in ingressroute_templates(role_dir))
    if _PUBLIC_FALSE_RE.search(text):
        return LAN
    return PUBLIC if public_route_enabled(group_vars) else LAN


def route_cell(label: str, reach: str) -> str:
    """The route column's contents for one service, as plain text.

    A public service is reachable on BOTH names, so both are printed -- the public one
    first, because it is the one that works from anywhere.
    """
    if reach == PUBLIC:
        return f"{label}.<domain> · {label}.local.<domain>"
    return f"{label}.local.<domain> (LAN only)"


# "sonarr.local.<domain>" -> host "sonarr.local". The literal ".<domain>" suffix is the
# marker; nothing else in these pages ends that way, so this cannot match prose by accident.
_FQDN_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9.-]*)\.<domain>")


def linkify_fqdns(text: str) -> str:
    """Wrap every "<host>.<domain>" placeholder so the docs site can link it.

    Applied by the MARKDOWN renderers only. The stored value stays plain text, so the
    standalone HTML artifact and every text consumer are unaffected, and a page read
    outside the docs site still shows the placeholder it always showed.
    """

    def _wrap(match: re.Match[str]) -> str:
        host = html.escape(match.group(1), quote=True)
        return f'<span class="fqdn" data-host="{host}">{host}.&lt;domain&gt;</span>'

    return _FQDN_RE.sub(_wrap, text)
