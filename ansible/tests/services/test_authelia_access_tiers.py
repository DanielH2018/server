"""Every SSO service's Authelia policy is the `auth_tier` declared on its containers_list entry.

Until #2057 the per-service rules were hand-written in the Authelia template and joined to
the inventory by a hostname string: four services had a rule and 24 rode the
`*.local.<domain>` one_factor wildcard by omission, and the only guard was one test naming one
service (deploy). `filter_plugins/authelia_access.py` now renders the rules from the entries
themselves and refuses an entry that attaches the middleware without declaring a tier.

The filter is checked as a `..._is_clean` / `..._is_flagged` pair on hand-built entries, and
then against the real render: for every `use_authelia: true` entry, the rendered rules must
resolve its LAN name, from an RFC1918 source, to the tier it declares. A census that found no
entries would pass that loop vacuously, so KNOWN_SSO names a set it must contain.
"""

import ipaddress
import random

import pytest
from ansible.errors import AnsibleFilterError
from lib import yaml_fast

from _helpers import HOST_VARS
from _k8s_render import rendered_docs
from authelia_access import AUTH_TIERS, authelia_service_rules

DOMAIN = "example.com"
RFC1918 = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]

# Non-vacuity: entries the real inventory must still carry with `use_authelia: true`. deploy,
# longhorn, code-server and n8n are the four two_factor services; the rest are one_factor
# members from across the list.
KNOWN_SSO = frozenset(
    {
        "deploy-ui",
        "longhorn-ui",
        "code-server",
        "n8n",
        "sonarr",
        "claude-otel",
        "scrutiny",
    }
)


def _entry(name, **overrides):
    entry = {
        "name": name,
        "platform": "k8s",
        "hostname": name,
        "use_authelia": True,
        "auth_tier": "one_factor",
    }
    entry.update(overrides)
    return entry


# --- the filter --------------------------------------------------------------------------


def test_tiers_group_into_two_sorted_rules_is_clean():
    rules = authelia_service_rules(
        [
            _entry("beta"),
            _entry("shell", auth_tier="two_factor"),
            _entry("alpha"),
            {
                "name": "plain",
                "platform": "k8s",
                "hostname": "plain",
                "use_authelia": False,
            },
            {"name": "helper", "platform": "k8s"},
        ],
        DOMAIN,
        RFC1918,
    )
    assert rules == [
        {"domain": [f"shell.local.{DOMAIN}"], "policy": "two_factor"},
        {
            "domain": [f"alpha.local.{DOMAIN}", f"beta.local.{DOMAIN}"],
            "policy": "one_factor",
            "networks": RFC1918,
        },
    ]


def test_a_reordered_list_renders_identically():
    """The Secret must not churn — and roll Authelia — on a reorder of containers_list."""
    entries = [_entry(f"svc{i}", auth_tier=AUTH_TIERS[i % 2]) for i in range(12)]
    expected = authelia_service_rules(entries, DOMAIN, RFC1918)
    shuffled = list(entries)
    random.Random(2057).shuffle(shuffled)
    assert authelia_service_rules(shuffled, DOMAIN, RFC1918) == expected


def test_an_entry_without_auth_tier_is_flagged():
    entry = _entry("sonarr")
    del entry["auth_tier"]
    with pytest.raises(AnsibleFilterError, match="'sonarr'.*no auth_tier"):
        authelia_service_rules([entry], DOMAIN, RFC1918)


def test_an_unknown_tier_is_flagged():
    with pytest.raises(AnsibleFilterError, match="'sonarr'.*'bypass'"):
        authelia_service_rules([_entry("sonarr", auth_tier="bypass")], DOMAIN, RFC1918)


def test_a_tier_without_the_middleware_is_flagged():
    """A declared tier on a `use_authelia: false` entry gates nothing; refuse the lie."""
    with pytest.raises(AnsibleFilterError, match="'jellyfin'.*not use_authelia"):
        authelia_service_rules(
            [_entry("jellyfin", use_authelia=False)], DOMAIN, RFC1918
        )


def test_an_sso_entry_without_a_hostname_is_flagged():
    entry = _entry("sonarr")
    del entry["hostname"]
    with pytest.raises(AnsibleFilterError, match="'sonarr'.*no hostname"):
        authelia_service_rules([entry], DOMAIN, RFC1918)


# --- applied to the real render ----------------------------------------------------------


def _rendered_rules():
    for role, _name, doc in rendered_docs():
        if role != "authelia" or doc.get("kind") != "Secret":
            continue
        raw = (doc.get("stringData") or {}).get("configuration.yml")
        if raw:
            return yaml_fast.safe_load(raw)["access_control"]["rules"]
    raise AssertionError("authelia configuration.yml not found in the render")


def _matches(rule, host, source):
    """Authelia's first-match test for a UI request to `/`, so a path-scoped bypass never
    matches: `*.x` matches any host ending in `.x`, and `networks` scopes by source."""
    domains = rule["domain"] if isinstance(rule["domain"], list) else [rule["domain"]]
    if not any(
        host == d or (d.startswith("*.") and host.endswith(d[1:])) for d in domains
    ):
        return False
    networks = rule.get("networks") or []
    if networks and not any(
        ipaddress.ip_address(source) in ipaddress.ip_network(n) for n in networks
    ):
        return False
    return "resources" not in rule


def _policy(rules, host, source):
    return next((r["policy"] for r in rules if _matches(r, host, source)), "deny")


def _sso_entries():
    box = yaml_fast.safe_load((HOST_VARS / "daniel-box.yml").read_text())
    return [c for c in box["containers_list"] if c.get("use_authelia")]


def test_every_sso_entry_resolves_to_its_declared_tier():
    rules = _rendered_rules()
    entries = _sso_entries()
    names = {c["name"] for c in entries}
    missing = KNOWN_SSO - names
    assert not missing, f"census lost {sorted(missing)}"
    assert len(entries) >= 24, len(entries)
    domain = next(
        d
        for r in rules
        for d in ([r["domain"]] if isinstance(r["domain"], str) else r["domain"])
        if d.startswith("*.local.")
    )[len("*.local.") :]
    for entry in entries:
        host = f"{entry['hostname']}.local.{domain}"
        assert _policy(rules, host, "10.0.0.5") == entry["auth_tier"], entry["name"]
        # The LAN tier is the LAN name's alone: the public name stays two_factor.
        assert (
            _policy(rules, f"{entry['hostname']}.{domain}", "1.2.3.4") == "two_factor"
        )


def test_the_wildcards_are_the_last_two_rules():
    """Specific rules must sort ahead of the wildcards or Authelia's first match hides them."""
    rules = _rendered_rules()
    tail = [r["domain"] for r in rules[-2:]]
    assert all(isinstance(d, str) and d.startswith("*.") for d in tail), tail
    assert not any(
        isinstance(r["domain"], str) and r["domain"].startswith("*.")
        for r in rules[:-2]
    )


def test_the_one_factor_rule_is_scoped_like_the_wildcard_it_replaces():
    """A service gaining its own rule must not gain reach the wildcard withheld: the same
    RFC1918 `networks` list, so a non-LAN source still falls through to two_factor."""
    rules = _rendered_rules()
    wildcard = next(r for r in rules if r["domain"] == f"*.local.{DOMAIN}")
    generated = next(
        r
        for r in rules
        if r["policy"] == "one_factor" and isinstance(r["domain"], list)
    )
    assert generated["networks"] == wildcard["networks"]
    assert wildcard["networks"] == RFC1918
