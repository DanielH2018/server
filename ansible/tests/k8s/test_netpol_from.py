"""Guards `netpol_from:`, the caller list a containers_list entry declares for its fence.

Two templates render the same key. netpol-baseline's `networkpolicy-callers.yaml.j2` loops over
every entry carrying it, and a role that must carry its own fence (sonarr, radarr, prowlarr,
qbittorrent, bazarr, headlamp) renders it from its own template while its entry sets `netpol_role_owned: true`.
An entry whose pod label is not its own name names the label in `netpol_app`, and the fence
selects that label and takes its name (scrutiny -> scrutiny-web). A sub-workload with no entry
of its own is a `netpol_fences: [{app, port, from}]` item on its role's entry, rendered by the
same template (freshrss -> freshrss-feed-cache), unless the item sets `role_owned: true` and
its role renders it (authelia -> authelia-redis). Forgetting the `netpol_role_owned`
flag renders one NetworkPolicy name from two roles, and whichever role deploys last silently
wins. Rendering neither leaves the service with no caller fence, and every listed caller is
denied by the baseline.

Asserted against the RENDERED manifests, because the loop's filter and the macro are both
Jinja: a text scan of the inventory says what was declared, not what renders.

Run: uv run pytest ansible/tests/k8s/test_netpol_from.py
"""

import pytest

from _helpers import K8S_ROLES
from _k8s_render import host_context, pod_template, rendered_docs
from homepage_tiles import netpol_callers
from lib import yaml_fast
from lib.ansible_inventory import containers_entries_in
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs

# Entries this guard must find, by the policy name each renders, with its renderer. Named rather
# than counted: if the key were renamed, the census below would go empty and every `all()` here
# would pass on nothing.
KNOWN_FENCES = {
    "authelia": "netpol-baseline",
    "speedtest": "netpol-baseline",
    "ical-proxy": "netpol-baseline",
    "home-assistant": "netpol-baseline",
    "karakeep": "netpol-baseline",
    "uptime-kuma": "netpol-baseline",
    "sonarr": "sonarr",
    "radarr": "radarr",
    "prowlarr": "prowlarr",
    "qbittorrent": "qbittorrent",
    "bazarr": "bazarr",
    "headlamp": "headlamp",
    # Entry `scrutiny`, rendered under its `netpol_app`.
    "scrutiny-web": "netpol-baseline",
    # A `netpol_fences` item on entry `freshrss`.
    "freshrss-feed-cache": "netpol-baseline",
    # `role_owned` `netpol_fences` items, each a backend its role's pod waits on.
    "authelia-redis": "authelia",
    "karakeep-meilisearch": "karakeep",
    "karakeep-chrome": "karakeep",
}


# Fences that admit homepage through `homepage_widget: true` on the entry rather than through
# `netpol_from`, one per renderer: a role-owned fence and a netpol-baseline one. Named so the
# key's derivation cannot quietly stop reaching either.
HOMEPAGE_WIDGET_FENCES = frozenset(
    {"sonarr", "qbittorrent", "scrutiny-web", "ical-proxy"}
)


def fence_name(entry: dict) -> str:
    """The pod label a fence selects, which is also the NetworkPolicy's name."""
    return entry.get("netpol_app", entry["name"])


# Callers whose pods render only from a role's `templates/verify/`, which the renderer emits into
# `<tag>-verify` and `rendered_docs()` does not read. Named so the derivation below cannot
# quietly stop reaching a role: each must be a pod label `verify_pod_labels` returns.
# qbittorrent's verify Job keeps the label of the `kubectl run` probe it replaced at the flip.
# janitorr's tests/test_verify_janitorr.py also checks that sonarr's and radarr's fences admit its Job.
VERIFY_CALLERS = frozenset(
    {
        "sonarr-verify",
        "radarr-verify",
        "prowlarr-verify",
        "qbittorrent-reach-probe",
        "janitorr-verify",
    }
)


@pytest.fixture(scope="module")
def verify_pod_labels(tmp_path_factory) -> set[str]:
    """The `app` label of every pod a role's `templates/verify/` renders, as Flux receives it."""
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    stubs = load_stubs(path)
    labels = set()
    for templates in sorted(K8S_ROLES.glob("*/templates/verify")):
        rendered = cluster.render_tag(templates.parent.parent.name, stubs)
        for name, text in rendered.verify.items():
            for doc in yaml_fast.safe_load_all(text):
                if name != "kustomization.yaml" and doc and doc["kind"] == "Job":
                    labels.add(pod_template(doc)["metadata"]["labels"]["app"])
    return labels


def declared_fences() -> dict[str, dict]:
    """Each fence daniel-box's entries declare, by the policy name it renders.

    Covers an entry's own callers (`netpol_from`, plus homepage for `homepage_widget`) and
    each of its `netpol_fences` items, normalised to `{"owner", "port", "callers"}`. A fence is role-owned through the entry's
    `netpol_role_owned` or the item's `role_owned`, and its owner is then the entry's role.
    """
    fences = {}
    for e in containers_entries_in(host_context()):
        if callers := netpol_callers(e):
            fences[fence_name(e)] = {
                "owner": e["name"] if e.get("netpol_role_owned") else "netpol-baseline",
                "port": e["port"],
                "callers": callers,
            }
        for f in e.get("netpol_fences", []):
            fences[f["app"]] = {
                "owner": e["name"] if f.get("role_owned") else "netpol-baseline",
                "port": f["port"],
                "callers": f["from"],
            }
    return fences


def network_policies() -> list[tuple[str, dict]]:
    """(rendering role, doc) for every rendered NetworkPolicy."""
    return [
        (role, doc)
        for role, _template, doc in rendered_docs()
        if doc.get("kind") == "NetworkPolicy"
    ]


def doubly_rendered(policies: list[tuple[str, dict]]) -> set[tuple[str, str]]:
    """(namespace, name) of every NetworkPolicy rendered more than once, by one role or two.

    Two documents of one name in the same role are a duplicate too: `homepage_widget` on an
    entry whose role also renders a bespoke fence of that name (pihole) would make the callers
    loop render a second copy, and the last one applied would replace the bespoke fence.
    """
    counts: dict[tuple[str, str], int] = {}
    for _role, doc in policies:
        meta = doc["metadata"]
        key = (meta.get("namespace", ""), meta["name"])
        counts[key] = counts.get(key, 0) + 1
    return {key for key, n in counts.items() if n > 1}


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


def test_each_homepage_widget_entry_admits_homepage():
    """Non-vacuity for `homepage_widget`: the test below checks only fences the census finds."""
    fences = declared_fences()
    assert {
        n for n in HOMEPAGE_WIDGET_FENCES if "homepage" in fences[n]["callers"]
    } == (HOMEPAGE_WIDGET_FENCES)


def test_no_network_policy_renders_twice():
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


def test_a_policy_rendered_twice_by_one_role_is_flagged():
    """A bespoke fence and a callers-loop fence of one name both render in netpol-baseline."""
    doc = {"metadata": {"name": "pihole", "namespace": "homelab"}}
    assert doubly_rendered([("netpol-baseline", doc), ("netpol-baseline", doc)]) == {
        ("homelab", "pihole")
    }


def test_each_entry_renders_one_fence_from_its_owner_admitting_its_callers():
    """The list on the entry is what renders, on the entry's port, from the expected role."""
    rendered = {
        doc["metadata"]["name"]: (role, doc) for role, doc in network_policies()
    }
    for name, fence in declared_fences().items():
        assert name in rendered, f"{name} declares a fence but no policy renders"
        role, doc = rendered[name]
        owner = fence["owner"]
        assert role == owner, f"{name}'s fence renders from {role}, expected {owner}"
        assert doc["spec"]["podSelector"] == {"matchLabels": {"app": name}}
        assert admitted(doc) == [
            (frozenset(fence["callers"]), frozenset({fence["port"]}))
        ]
    for name, owner in KNOWN_FENCES.items():
        assert rendered[name][0] == owner


def test_every_verify_caller_is_a_label_a_verify_job_carries(verify_pod_labels):
    """Non-vacuity for the verify render the test below adds to the manifests' labels."""
    assert VERIFY_CALLERS <= verify_pod_labels


def test_every_caller_is_a_label_some_rendered_pod_carries(verify_pod_labels):
    """A caller naming no pod admits nothing, and the real caller stays denied."""
    labels = verify_pod_labels | {
        (pod_template(doc).get("metadata") or {}).get("labels", {}).get("app")
        for _role, _template, doc in rendered_docs()
    }
    unknown = {
        (name, caller)
        for name, fence in declared_fences().items()
        for caller in fence["callers"]
        if caller not in labels
    }
    assert unknown == set()
