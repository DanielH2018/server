#!/usr/bin/env python3
"""The k8s play toposorts containers_list; this guards that the sort is actually correct.

deploy.yml calls build_k8s_dep_map + toposort_containers (ansible/filter_plugins/toposort.py),
so the ordering rules are graph edges rather than hand position in a list -- hand position
holds right up until someone appends a service in the obvious place. Two rules are derived:

  traefik <- every ROUTED role.
      Derived from the containers_list entry's `hostname` (_entry_is_routed), with the
      role's own templates (_role_renders_traefik_crd) as a second test for a role that
      renders a Traefik CRD without declaring a host of its own. Neither is hand-listed.
      Fails LOUDLY without the edge, on a fresh cluster: the CRD does not exist, `kubectl
      apply` exits non-zero, the task fails.

      The entry is the primary test because the shared default,
      `ansible/templates/ingressroute-default.yaml.j2`, renders a route that leaves no string
      in the role, so a templates-only derivation would drop the edge for every role using it.

  authelia <- every entry with use_authelia: true.
      Derived from the containers_list entry itself. Fails SILENTLY without the edge: the
      IngressRoute applies cleanly whether or not the Middleware exists -- Traefik resolves
      middleware references at request time, not apply time -- so the deploy is green and
      the route 500s later, at whatever hour someone first opens it.

A third edge is declared data, not derived: traefik `depends_on: [crowdsec]` in host_vars,
because the LAPI machine-credential constraint isn't something a template carries. It is
also the reverse of the first rule for crowdsec specifically -- crowdsec renders its own
Traefik CRD (an IngressRoute) -- which is why K8S_CRD_EDGE_EXEMPT in toposort.py excludes
crowdsec from the auto-derived traefik edge; without that exemption the two edges would
cycle and toposort_containers would raise on every deploy.

test_derived_order_matches_todays_hand_ordered_list is the cheapest proof the sort is
sound: the hand-ordered list is a valid topological order, and toposort_containers is a
stable sort, so running it over the list must reproduce the list unchanged. If it doesn't, an edge points the
wrong way.

Run: uv run pytest ansible/tests/deploy/test_k8s_toposort.py
"""

import random
from pathlib import Path

import pytest
from lib import yaml_fast

from _helpers import ANSIBLE, HOST_VARS
from _k8s_render import rendered_docs
from toposort import (
    K8S_CRD_EDGE_EXEMPT,
    build_k8s_dep_map,
    toposort_containers,
)


def _k8s_entries(path: Path) -> list[dict]:
    loaded = yaml_fast.safe_load(path.read_text()) or {}
    return [
        c
        for c in (loaded.get("containers_list") or [])
        if c.get("platform") == "k8s" and c.get("name")
    ]


def _host_files() -> list[Path]:
    return sorted(p for p in HOST_VARS.glob("*.yml") if not p.name.startswith("_"))


HOSTS_WITH_K8S = [p for p in _host_files() if _k8s_entries(p)]


def _roles_rendering_traefik_crds() -> set[str]:
    """Roles whose rendered manifests include any traefik.io/* object -- render ground truth.

    Derived from the rendered output, not a filename or text scan: an IngressRoute that
    only appears under a Jinja conditional still counts, and a role that stops rendering
    one drops out without anyone editing this file. It covers the shared default too, so a
    role whose IngressRoute is `ansible/templates/ingressroute-default.yaml.j2` is in here
    under its own name. Compared below against the edge set build_k8s_dep_map derives at
    deploy time, which has to agree with this on every role or the derived edges are wrong.
    """
    return {
        role
        for role, _tpl, doc in rendered_docs()
        if doc and str(doc.get("apiVersion", "")).split("/")[0] == "traefik.io"
    }


TRAEFIK_CRD_ROLES = _roles_rendering_traefik_crds()


def _index(entries: list[dict]) -> dict[str, int]:
    return {c["name"]: i for i, c in enumerate(entries)}


def _sorted_names(entries: list[dict]) -> list[str]:
    dep_map = build_k8s_dep_map(entries, str(ANSIBLE))
    return [c["name"] for c in toposort_containers(entries, dep_map)]


@pytest.fixture(params=HOSTS_WITH_K8S, ids=lambda p: p.stem)
def host(request):
    entries = _k8s_entries(request.param)
    return request.param, entries, _index(entries)


def test_the_render_found_traefik_crd_roles_at_all():
    # An empty set would make every comparison below vacuously pass -- the "unreadable
    # input and an empty result are indistinguishable" shape. Assert a concrete floor and
    # named members, not just a count: a count can hold while a specific role drops out.
    assert len(TRAEFIK_CRD_ROLES) > 10, TRAEFIK_CRD_ROLES
    assert {"traefik", "authelia", "crowdsec", "freshrss"} <= TRAEFIK_CRD_ROLES


def _derived_traefik_edge(entries: list[dict]) -> set[str]:
    """The roles build_k8s_dep_map actually gives a traefik edge, for this host's entries.

    Read off the map the deploy builds rather than by re-asking its predicates: a test that
    reimplemented `_entry_is_routed or _role_renders_traefik_crd` would keep agreeing with
    itself after the derivation changed.
    """
    dep_map = build_k8s_dep_map(entries, str(ANSIBLE))
    return {name for name, deps in dep_map.items() if "traefik" in deps}


# Roles the derivation claims an edge for while the render shows no Traefik CRD in the role.
# Over-derivation is the safe direction and under-derivation is not: an extra edge costs an
# ordering constraint that is already true, while a missing one applies a Traefik CRD before
# traefik owns the CRDs. Each name carries the reason it is one-directional.
OVER_DERIVED_TRAEFIK_EDGE = {
    "livesync": (
        "its route is a file-provider router in the traefik role "
        "(templates/livesync-gate-secret.yaml.j2), not an IngressRoute of its own, so the "
        "entry's hostname derives an edge no manifest of livesync's renders. The role's own "
        "CLAUDE.md already states the traefik dependency"
    ),
}


def test_the_derived_traefik_edge_agrees_with_the_render(host):
    """build_k8s_dep_map's deploy-time derivation must match the render, for every role.

    The derivation reads the containers_list entry and textually scans the role's own
    templates, standing in for a real Jinja render so the deploy doesn't pay for one. That's
    only sound if it agrees with the render -- except for OVER_DERIVED_TRAEFIK_EDGE, where an
    extra edge is the safe direction and a missing one is still a failure.
    """
    _path, entries, _idx = host
    derived = _derived_traefik_edge(entries)
    for c in entries:
        name = c["name"]
        if name == "traefik" or name in K8S_CRD_EDGE_EXEMPT:
            continue
        rendered = name in TRAEFIK_CRD_ROLES
        if name in OVER_DERIVED_TRAEFIK_EDGE:
            assert (name in derived) >= rendered, (
                f"{name}: build_k8s_dep_map derives no traefik edge while the render says it "
                "renders a Traefik CRD. The deploy would apply that CRD before traefik owns "
                "the CRDs."
            )
            continue
        assert (name in derived) == rendered, (
            f"{name}: build_k8s_dep_map derives traefik edge={name in derived}, render says "
            f"{rendered}. Either the entry lost its hostname or the role gained a Traefik CRD "
            "nothing derives an edge from."
        )


def test_every_over_derived_role_is_a_real_role_that_still_gets_the_edge(host):
    """The exemption list must not outlive what it exempts.

    A name that no longer exists, or one the derivation no longer gives an edge, silently
    widens the test above into an exemption for nothing.
    """
    _path, entries, _idx = host
    names = {c["name"] for c in entries}
    derived = _derived_traefik_edge(entries)
    for name, reason in OVER_DERIVED_TRAEFIK_EDGE.items():
        if name not in names:
            continue
        assert reason.strip(), f"{name} is exempted with no reason"
        assert name in derived, (
            f"{name} is listed in OVER_DERIVED_TRAEFIK_EDGE but build_k8s_dep_map derives no "
            "traefik edge for it at all — drop the exemption."
        )


def test_a_routed_entry_that_ships_no_route_template_still_gets_the_edge():
    """The red-proof for the shared-default route: the roles whose IngressRoute is the shared default.

    `_role_renders_traefik_crd` returns False for a role with no templates directory at all,
    so an entry-blind derivation would return an empty dep list here.
    """
    entry = {"name": "widget", "hostname": "widget", "port": 80}
    assert build_k8s_dep_map([entry], str(ANSIBLE))["widget"] == ["traefik"]

    unrouted = {"name": "widget", "port": 80}
    assert build_k8s_dep_map([unrouted], str(ANSIBLE))["widget"] == []


def test_traefik_is_deployed(host):
    _path, _entries, idx = host
    assert "traefik" in idx, "a host with k8s workloads must deploy traefik"


def test_crowdsec_still_needs_the_crd_edge_exemption(host):
    """An exemption that no longer applies is a licence nobody revoked."""
    _path, _entries, idx = host
    if "crowdsec" not in idx:
        pytest.skip("host does not deploy crowdsec")
    assert "crowdsec" in TRAEFIK_CRD_ROLES, (
        "crowdsec no longer renders a Traefik CRD -- delete it from K8S_CRD_EDGE_EXEMPT "
        "in toposort.py so a real traefik edge is derived for it again."
    )


def test_derived_order_matches_todays_hand_ordered_list(host):
    """The toposort is a no-op on the live inventory: see the module docstring for why."""
    _path, entries, _idx = host
    original = [c["name"] for c in entries]
    assert _sorted_names(entries) == original, (
        "toposorting containers_list changed the order the hand-maintained list already "
        "has. Either a derived edge points the wrong way, or the hand-ordered list itself "
        "violated a constraint the old position-based test would have caught."
    )


def test_shuffled_list_still_sorts_to_respect_every_edge(host):
    """The real guard: however containers_list gets reordered, the sort recovers the rules."""
    _path, entries, _idx = host
    if "traefik" not in {c["name"] for c in entries}:
        pytest.skip("host does not deploy traefik")
    rng = random.Random(f"k8s-toposort-{_path.stem}")
    shuffled = list(entries)
    rng.shuffle(shuffled)

    sorted_names = _sorted_names(shuffled)
    idx = {name: i for i, name in enumerate(sorted_names)}

    late_crd = [
        name
        for name in sorted_names
        if name in TRAEFIK_CRD_ROLES
        and name not in K8S_CRD_EDGE_EXEMPT
        and idx[name] < idx["traefik"]
    ]
    assert not late_crd, f"{late_crd} sorted before traefik despite rendering its CRDs"

    if "authelia" in idx:
        late_authelia = [
            c["name"]
            for c in shuffled
            if c.get("use_authelia") and idx[c["name"]] < idx["authelia"]
        ]
        assert not late_authelia, (
            f"{late_authelia} sorted before authelia despite use_authelia: true"
        )

    if "crowdsec" in idx:
        assert idx["crowdsec"] < idx["traefik"], (
            "crowdsec sorted after traefik despite the LAPI machine-credential depends_on"
        )


# Every pairwise ordering a role doc or a containers_list comment states in prose, as the
# `depends_on:` edge that carries it. Without the edge these hold by hand position alone,
# which the stable sort preserves and nothing checks: dropping the edge, or moving the entry,
# would pass every test above. The value names where the prose lives, so a reader of a red
# failure knows which doc to reconcile.
DOCUMENTED_ORDERINGS = {
    ("mosquitto", "zigbee2mqtt"): "roles/k8s/mosquitto/CLAUDE.md",
    ("media-volume", "sonarr"): "roles/k8s/media-volume/CLAUDE.md",
    ("media-volume", "qbittorrent"): "roles/k8s/media-volume/CLAUDE.md",
    ("media-volume", "radarr"): "roles/k8s/media-volume/CLAUDE.md",
    ("media-volume", "bazarr"): "roles/k8s/media-volume/CLAUDE.md",
    ("media-volume", "jellyfin"): "roles/k8s/media-volume/CLAUDE.md",
    ("media-volume", "tdarr"): "roles/k8s/media-volume/CLAUDE.md",
    ("media-volume", "janitorr"): "roles/k8s/media-volume/CLAUDE.md",
    ("loki-homelab", "game-stats"): "roles/k8s/game-stats/CLAUDE.md",
    ("sonarr", "janitorr"): "host_vars/daniel-box.yml (janitorr comment)",
    ("radarr", "janitorr"): "host_vars/daniel-box.yml (janitorr comment)",
    ("jellyfin", "janitorr"): "host_vars/daniel-box.yml (janitorr comment)",
}


@pytest.mark.parametrize(
    ("before", "after"), sorted(DOCUMENTED_ORDERINGS), ids=lambda s: s
)
def test_documented_pairwise_ordering_survives_an_adversarial_list(host, before, after):
    """The derived order keeps `before` ahead of `after` when the list itself says otherwise.

    The hand-ordered list already satisfies every pair, and toposort_containers is stable,
    so sorting it proves nothing about the edge. `after` is moved to the head and `before`
    to the tail: only an edge in build_k8s_dep_map can put them back.
    """
    # fact: ansible/roles/k8s/mosquitto/CLAUDE.md#At a glance
    # fact: ansible/roles/k8s/media-volume/CLAUDE.md#At a glance
    # fact: ansible/roles/k8s/game-stats/CLAUDE.md#At a glance
    _path, entries, idx = host
    if before not in idx or after not in idx:
        pytest.skip(f"host does not deploy both {before} and {after}")
    by_name = {c["name"]: c for c in entries}
    # The edge must be DECLARED, not merely implied: a pair held only by a transitive edge
    # elsewhere would pass the sort below whatever this entry says, which is vacuous.
    assert before in by_name[after].get("depends_on", []), (
        f"{after}'s containers_list entry declares no `depends_on: [{before}]`; "
        f"{DOCUMENTED_ORDERINGS[(before, after)]} says it must."
    )
    others = [c for c in entries if c["name"] not in (before, after)]
    adversarial = [by_name[after], *others, by_name[before]]

    order = _sorted_names(adversarial)
    assert order.index(before) < order.index(after), (
        f"{after} sorted before {before}; {DOCUMENTED_ORDERINGS[(before, after)]} says it "
        f"must not. The `depends_on:` on {after}'s containers_list entry is what carries "
        "that, and it is gone or points elsewhere."
    )
