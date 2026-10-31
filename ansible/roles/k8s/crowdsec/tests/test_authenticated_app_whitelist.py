"""The authenticated-app whitelist names routers the render really produces, and both agents load it.

The whitelist's expressions match Traefik router names, which Traefik derives from each
IngressRoute's namespace, name and match. A renamed route or a changed match moves the name,
and a stale pattern then exempts nothing while the agent loads cleanly, so these tests derive
the names from the render rather than restating them.

What this does NOT prove: that CrowdSec evaluates the expressions as intended. That evidence is
`cscli explain` over real access-log lines, recorded in the PR that added the file.
"""

import re

import yaml
from _k8s_render import rendered_docs

import remote_allowlist as ra
from lib.repo_paths import ANSIBLE

WHITELIST = (
    ANSIBLE / "roles/k8s/crowdsec/files/crowdsec-authenticated-app-whitelist.yaml"
)
SEED = "authenticated-app-whitelist.yaml"
# Both pods carrying an agent sidecar; a third must join this set rather than slip past it.
AGENT_ROLES = frozenset({"traefik", "authelia"})


def _expressions():
    return yaml.safe_load(WHITELIST.read_text())["whitelist"]["expression"]


def _router_pattern(expression):
    found = re.search(r"traefik_router_name matches '([^']+)'", expression)
    assert found, expression
    return found.group(1)


def _crd_router(role, ingressroute):
    [doc] = [
        doc
        for r, _template, doc in rendered_docs()
        if r == role
        and doc.get("kind") == "IngressRoute"
        and doc["metadata"]["name"] == ingressroute
    ]
    [route] = doc["spec"]["routes"]
    meta = doc["metadata"]
    return ra.router_name(meta["namespace"], meta["name"], route["match"])


def test_the_crd_patterns_match_the_routers_traefik_derives_and_no_sibling():
    homepage, _livesync, jellyfin = _expressions()
    assert re.match(
        _router_pattern(homepage), _crd_router("homepage", "homepage-public")
    )
    assert re.match(
        _router_pattern(jellyfin), _crd_router("jellyfin", "jellyfin-public")
    )
    # The LAN routes carry no Authelia on Jellyfin and a LAN bypass may apply on Homepage.
    assert not re.match(_router_pattern(homepage), _crd_router("homepage", "homepage"))
    assert not re.match(_router_pattern(jellyfin), _crd_router("jellyfin", "jellyfin"))


def test_the_livesync_names_are_the_token_gated_routers_and_not_the_probe():
    [gate] = [
        doc
        for role, _template, doc in rendered_docs()
        if role == "traefik" and doc["metadata"]["name"] == "traefik-livesync-gate"
    ]
    routers = yaml.safe_load(gate["stringData"]["livesync-gate.yml"])["http"]["routers"]
    named = set(re.findall(r"'([a-z-]+)@file'", _expressions()[1]))
    assert named == {"livesync-sync-public", "livesync-sync-local"}
    assert named <= routers.keys()
    for name in named:
        assert "X-Sync-Token" in routers[name]["rule"]


def _pods():
    return {
        role: doc["spec"]["template"]["spec"]
        for role, _template, doc in rendered_docs()
        if role in AGENT_ROLES
        and doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == role
    }


def test_both_agents_seed_the_whitelist_into_s02_enrich():
    secrets = {
        role: doc["stringData"]
        for role, _template, doc in rendered_docs()
        if doc.get("kind") == "Secret" and doc["metadata"]["name"] == f"{role}-crowdsec"
    }
    assert secrets.keys() >= AGENT_ROLES
    for role, pod in _pods().items():
        assert yaml.safe_load(secrets[role][SEED]) == yaml.safe_load(
            WHITELIST.read_text()
        )
        [install] = [
            c for c in pod["initContainers"] if c["name"] == "crowdsec-config-install"
        ]
        command = " ".join(install["command"])
        assert re.search(rf"/seed/{SEED}\s+/etc/crowdsec/parsers/s02-enrich/", command)
        [agent] = [c for c in pod["containers"] if c["name"] == "crowdsec-agent"]
        parsers = {e["name"]: e.get("value") for e in agent["env"]}.get("PARSERS")
        assert ("jellyfin-whitelist" in (parsers or "")) == (role == "traefik")
    assert _pods().keys() == AGENT_ROLES
