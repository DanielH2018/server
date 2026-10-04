"""Guards `netpol_from:`, the caller list a containers_list entry declares for its fence.

Two templates render the same key. netpol-baseline's `networkpolicy-callers.yaml.j2` loops over
every entry carrying it, and a role that must carry its own fence (sonarr, radarr, prowlarr)
renders it from its own template while its entry sets `netpol_role_owned: true`. Forgetting that
flag renders one NetworkPolicy name from two roles, and whichever role deploys last silently
wins. Rendering neither leaves the service with no caller fence, and every listed caller is
denied by the baseline.

Asserted against the RENDERED manifests, because the loop's filter and the macro are both
Jinja: a text scan of the inventory says what was declared, not what renders.

Run: uv run pytest ansible/tests/k8s/test_netpol_from.py
"""

from _k8s_render import host_context, pod_template, rendered_docs
from lib.ansible_inventory import containers_entries_in

# Entries this guard must find, one per renderer. Named rather than counted: if the key were
# renamed, the census below would go empty and every `all()` here would pass on nothing.
KNOWN_FENCES = {
    "speedtest": "netpol-baseline",
    "ical-proxy": "netpol-baseline",
    "sonarr": "sonarr",
    "radarr": "radarr",
    "prowlarr": "prowlarr",
}


def declared_fences() -> dict[str, dict]:
    """Each daniel-box entry carrying `netpol_from`, by name."""
    return {
        e["name"]: e
        for e in containers_entries_in(host_context())
        if "netpol_from" in e
    }


def network_policies() -> list[tuple[str, dict]]:
    """(rendering role, doc) for every rendered NetworkPolicy."""
    return [
        (role, doc)
        for role, _template, doc in rendered_docs()
        if doc.get("kind") == "NetworkPolicy"
    ]


def doubly_rendered(policies: list[tuple[str, dict]]) -> set[tuple[str, str]]:
    """(namespace, name) of every NetworkPolicy more than one role renders."""
    owners: dict[tuple[str, str], set[str]] = {}
    for role, doc in policies:
        meta = doc["metadata"]
        owners.setdefault((meta.get("namespace", ""), meta["name"]), set()).add(role)
    return {key for key, roles in owners.items() if len(roles) > 1}


def admitted(doc: dict) -> list[tuple[frozenset[str], frozenset[int]]]:
    """(caller apps, ports) for each ingress rule, read from `matchLabels` peers."""
    rules = []
    for rule in doc["spec"].get("ingress") or []:
        apps = frozenset(
            peer["podSelector"]["matchLabels"]["app"]
            for peer in rule.get("from") or []
            if "app" in ((peer.get("podSelector") or {}).get("matchLabels") or {})
        )
        ports = frozenset(p["port"] for p in rule.get("ports") or [])
        rules.append((apps, ports))
    return rules


def test_the_census_finds_every_known_fence():
    """Non-vacuity: the tests below pass on an empty census."""
    assert KNOWN_FENCES.keys() <= declared_fences().keys()


def test_no_network_policy_renders_from_two_roles():
    assert doubly_rendered(network_policies()) == set()


def test_a_policy_rendered_by_two_roles_is_flagged():
    """The rejecting half: an entry missing `netpol_role_owned` would render twice."""
    doc = {"metadata": {"name": "sonarr", "namespace": "homelab"}}
    policies = [
        ("sonarr", doc),
        ("netpol-baseline", doc),
        ("radarr", {"metadata": {"name": "radarr", "namespace": "homelab"}}),
    ]
    assert doubly_rendered(policies) == {("homelab", "sonarr")}


def test_each_entry_renders_one_fence_from_its_owner_admitting_its_callers():
    """The list on the entry is what renders, on the entry's port, from the expected role."""
    rendered = {
        doc["metadata"]["name"]: (role, doc) for role, doc in network_policies()
    }
    for name, entry in declared_fences().items():
        owner = name if entry.get("netpol_role_owned") else "netpol-baseline"
        assert name in rendered, f"{name} declares netpol_from but no policy renders"
        role, doc = rendered[name]
        assert role == owner, f"{name}'s fence renders from {role}, expected {owner}"
        assert doc["spec"]["podSelector"] == {"matchLabels": {"app": name}}
        assert admitted(doc) == [
            (frozenset(entry["netpol_from"]), frozenset({entry["port"]}))
        ]
    for name, owner in KNOWN_FENCES.items():
        assert rendered[name][0] == owner


def test_every_caller_is_a_label_some_rendered_pod_carries():
    """A caller naming no pod admits nothing, and the real caller stays denied."""
    labels = {
        (pod_template(doc).get("metadata") or {}).get("labels", {}).get("app")
        for _role, _template, doc in rendered_docs()
    }
    unknown = {
        (name, caller)
        for name, entry in declared_fences().items()
        for caller in entry["netpol_from"]
        if caller not in labels
    }
    assert unknown == set()
