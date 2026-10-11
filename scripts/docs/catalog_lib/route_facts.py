#!/usr/bin/env python3
"""Shared route facts for the reference generators.

WHY THIS IS SEPARATE. service_catalog.py and reference/networking.py both have to
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

STATIC PARSING ONLY: yaml.safe_load over the inventory, plain regex over template text with
its Jinja comments blanked first (`lib.jinja_comments`) -- a comment quoting `public=false`
is prose about the route, not an argument to it.
"""

import html
import re
from pathlib import Path
from typing import Any

import yaml

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

from lib import yaml_fast

from lib.jinja_comments import strip_jinja_comments
from lib.k8s_roles import manifest_template
from lib.repo_paths import ALL_VARS as GROUP_VARS

# `public=false` in a role's own macro call opts the service out of the public Host rule
# whatever k8s_public_route says. See ansible/templates/ingressroute.yml.j2. Matched against
# comment-stripped text: three roles explain the argument in a `{# #}` above the call that
# passes it, and `roles/k8s/docs/` explained one it does NOT pass (#3148).
_PUBLIC_FALSE_RE = re.compile(r"public\s*=\s*false")
# Only the `ingressroute()` macro renders the public Host rule. A role whose templates call
# `monitoring_route()` alone (ical-proxy, loki-homelab) is LAN-only by construction, and a
# comment saying "not ingressroute()" must not read as a call — hence the `{{` anchor.
_INGRESSROUTE_CALL_RE = re.compile(r"\{\{-?\s*ingressroute\(")

LAN = "lan"
PUBLIC = "public"


def ingressroute_templates(role_dir: Path, entry: dict | None = None) -> list[Path]:
    """Every ingressroute template a role's route is rendered from, or [] if it has no route.

    The role's own `templates/ingressroute*.j2`, plus the shared default under
    `ansible/templates/` when the role resolves `ingressroute.yaml` into its manifests and
    ships no template for it. `entry` is its containers_list entry, which decides that. The shared one has to be included or `reachability` reads the role's remaining
    templates alone: sonarr keeps only `ingressroute-monitoring.yaml.j2`, which calls
    `monitoring_route()` and never `ingressroute()`, so the service would report LAN-only.
    """
    templates = role_dir / "templates"
    own = (
        sorted(p for p in templates.glob("*.j2") if "ingressroute" in p.name)
        if templates.is_dir()
        else []
    )
    shared = manifest_template(
        role_dir.name, "ingressroute.yaml", role_dir.parent, entry
    )
    if shared is not None and shared not in own:
        own.append(shared)
    return own


def public_route_enabled(group_vars: Path = GROUP_VARS) -> bool:
    """Whether the cluster-wide public Host rule is on.

    Reads the plaintext group_vars rather than assuming. A missing or unreadable file is
    treated as OFF, because claiming a route is public when it is not sends a reader to a
    name that does not resolve -- the safer error is under-reporting reachability.
    """
    try:
        loaded: Any = yaml_fast.safe_load(group_vars.read_text())
    except OSError, yaml.YAMLError:
        return False
    return bool(isinstance(loaded, dict) and loaded.get("k8s_public_route"))


def reachability(
    role_dir: Path, group_vars: Path = GROUP_VARS, entry: dict | None = None
) -> str:
    """PUBLIC or LAN for a role that has a route. Callers check for a route first.

    `entry` is the role's containers_list entry, which decides whether it takes the shared
    route; `manifest_template` has the default.
    """
    text = "\n".join(
        strip_jinja_comments(p.read_text())
        for p in ingressroute_templates(role_dir, entry)
    )
    if not _INGRESSROUTE_CALL_RE.search(text) or _PUBLIC_FALSE_RE.search(text):
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
