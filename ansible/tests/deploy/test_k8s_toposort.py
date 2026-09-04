#!/usr/bin/env python3
"""The k8s play toposorts containers_list; this guards that the sort is actually correct.

Until this file, the k8s play deployed in raw list order (deploy.yml, and
tasks/k8s_batch.yml batching that order at k8s_rollout_batch_width) and a hand-written
comment above containers_list carried the ordering rules -- the kind of guard that holds
right up until someone appends a service in the obvious place. deploy.yml now calls
build_k8s_dep_map + toposort_containers (ansible/filter_plugins/toposort.py), the same
toposort_containers the Docker play already used, so the two rules that used to be
enforced by hand position are now graph edges instead:

  traefik <- every role rendering a Traefik CRD.
      Derived from the role's own templates (_role_renders_traefik_crd), not hand-listed.
      Fails LOUDLY without the edge, on a fresh cluster: the CRD does not exist, `kubectl
      apply` exits non-zero, the task fails.

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

test_derived_order_matches_todays_hand_ordered_list is the cheapest proof the change is
sound: today's hand-ordered list is already a valid topological order (the assertions this
file used to make prove that), and toposort_containers is a stable sort, so running it
over today's list must reproduce today's list unchanged. If it doesn't, an edge points the
wrong way.

Run: uv run pytest ansible/tests/deploy/test_k8s_toposort.py
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from lib import yaml_fast

from _helpers import ANSIBLE, HOST_VARS
from _k8s_render import rendered_docs
from toposort import (
    K8S_CRD_EDGE_EXEMPT,
    _role_renders_traefik_crd,
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
    one drops out without anyone editing this file. Compared below against the textual
    scan build_k8s_dep_map actually runs at deploy time, which has to agree with this on
    every role or the derived edges are wrong.
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


def test_textual_scan_agrees_with_the_render(host):
    """build_k8s_dep_map's deploy-time detector must match the render, for every role.

    The detector textually scans a role's own templates for the traefik.io apiVersion or
    the shared ingressroute.yml.j2 macro import, standing in for a real Jinja render so the
    deploy doesn't pay for one. That's only sound if it agrees with the render exactly.
    """
    _path, entries, _idx = host
    for c in entries:
        name = c["name"]
        templates_dir = ANSIBLE / "roles" / "k8s" / name / "templates"
        detected = _role_renders_traefik_crd(str(templates_dir))
        assert detected == (name in TRAEFIK_CRD_ROLES), (
            f"{name}: textual scan says renders-Traefik-CRD={detected}, render says "
            f"{name in TRAEFIK_CRD_ROLES}. build_k8s_dep_map would derive the wrong edge."
        )


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
