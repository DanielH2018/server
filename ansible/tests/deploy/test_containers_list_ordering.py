#!/usr/bin/env python3
"""The k8s play deploys in list order, so list order is a correctness constraint.

Unlike the Docker play, the k8s play does no toposort -- `containers_list` order in
host_vars IS the deploy order (deploy.yml:139-146, and tasks/k8s_batch.yml then batches
that order at k8s_rollout_batch_width). Until now the ordering rules lived only in a
comment above the list, which is the kind of guard that holds right up until someone
appends a service in the obvious place.

Two rules, and they fail very differently, which is why this test exists rather than a
toposort:

  traefik before anything rendering a Traefik CRD.
      Fails LOUDLY on a fresh cluster -- the CRD does not exist, `kubectl apply` exits
      non-zero, the task fails. Cheap to diagnose. Guarded here anyway because it is
      free once the render machinery is loaded for the rule below.

  authelia before anything with use_authelia: true.
      Fails SILENTLY. The IngressRoute applies cleanly whether or not the Middleware
      exists -- Traefik resolves middleware references at request time, not apply time --
      so the deploy is green and the route 500s later, at whatever hour someone first
      opens it. This is the rule that earns the file.

A third rule is asserted from the list itself: crowdsec before traefik. The traefik pod
carries a crowdsec agent sidecar that logs into the engine's LAPI, and the machine
credential is registered by a post-deploy task in the crowdsec role, so the engine must
exist before a traefik rollout starts that sidecar.

Run: uv run pytest ansible/tests/test_containers_list_ordering.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from _k8s_render import rendered_docs
from _helpers import HOST_VARS


# A role may render a Traefik CRD before traefik itself ONLY with a reason recorded here.
# Deliberately a written note rather than a bare set: the whole failure mode this file
# guards is an ordering decision that survives as folklore.
CRD_ORDER_EXEMPT = {
    "crowdsec": (
        "Must precede traefik for the LAPI machine credential (see the comment above "
        "containers_list in daniel-box.yml). Its own IngressRoute therefore applies "
        "before traefik installs the CRDs -- fine on the running cluster, where they "
        "already exist, and a documented first-run ordering cost on a rebuild. NB the "
        "inventory comment still claims this role 'uses no Traefik CRDs'; that stopped "
        "being true when the LAPI route landed."
    ),
}


def _k8s_entries(path: Path) -> list[dict]:
    loaded = yaml.safe_load(path.read_text()) or {}
    return [
        c
        for c in (loaded.get("containers_list") or [])
        if c.get("platform") == "k8s" and c.get("name")
    ]


def _host_files() -> list[Path]:
    return sorted(p for p in HOST_VARS.glob("*.yml") if not p.name.startswith("_"))


HOSTS_WITH_K8S = [p for p in _host_files() if _k8s_entries(p)]


def _roles_rendering_traefik_crds() -> set[str]:
    """Roles whose rendered manifests include any traefik.io/* object.

    Derived from the rendered output, not a filename or text scan: an IngressRoute that
    only appears under a Jinja conditional still counts, and a role that stops rendering
    one drops out without anyone editing this file.
    """
    return {
        role
        for role, _tpl, doc in rendered_docs()
        if doc and str(doc.get("apiVersion", "")).startswith("traefik.io/")
    }


TRAEFIK_CRD_ROLES = _roles_rendering_traefik_crds()


def _index(entries: list[dict]) -> dict[str, int]:
    return {c["name"]: i for i, c in enumerate(entries)}


@pytest.fixture(params=HOSTS_WITH_K8S, ids=lambda p: p.stem)
def host(request):
    entries = _k8s_entries(request.param)
    return request.param, entries, _index(entries)


def test_the_render_found_traefik_crd_roles_at_all():
    # An empty set would make every ordering assertion below vacuously pass -- the
    # "unreadable input and an empty result are indistinguishable" shape. Assert the
    # input loaded before trusting anything derived from it.
    assert len(TRAEFIK_CRD_ROLES) > 10, TRAEFIK_CRD_ROLES
    assert "traefik" in TRAEFIK_CRD_ROLES


def test_traefik_is_deployed(host):
    _path, _entries, idx = host
    assert "traefik" in idx, "a host with k8s workloads must deploy traefik"


def test_crowdsec_precedes_traefik(host):
    _path, _entries, idx = host
    if "crowdsec" not in idx:
        pytest.skip("host does not deploy crowdsec")
    assert idx["crowdsec"] < idx["traefik"], (
        "crowdsec must precede traefik: the traefik pod's crowdsec agent sidecar "
        "authenticates to the LAPI with a machine credential the crowdsec role "
        "registers in a post-deploy task."
    )


def test_traefik_precedes_every_role_rendering_its_crds(host):
    _path, entries, idx = host
    late = [
        c["name"]
        for c in entries
        if c["name"] in TRAEFIK_CRD_ROLES
        and c["name"] not in CRD_ORDER_EXEMPT
        and idx[c["name"]] < idx["traefik"]
    ]
    assert not late, (
        f"{late} render a Traefik CRD but are listed before traefik, which installs "
        f"those CRDs. On a fresh cluster their kubectl apply fails. Move them after "
        f"traefik, or add an entry to CRD_ORDER_EXEMPT with the reason."
    )


def test_authelia_precedes_every_service_that_uses_it(host):
    _path, entries, idx = host
    if "authelia" not in idx:
        pytest.skip("host does not deploy authelia")
    late = [
        c["name"]
        for c in entries
        if c.get("use_authelia") and idx[c["name"]] < idx["authelia"]
    ]
    assert not late, (
        f"{late} set use_authelia: true but are listed before authelia. This does NOT "
        f"fail the deploy -- the IngressRoute applies fine and Traefik resolves the "
        f"middleware at request time -- so the route would 500 for real users while "
        f"every deploy reads green. Move them after authelia."
    )


@pytest.mark.parametrize("role", sorted(CRD_ORDER_EXEMPT))
def test_every_crd_exemption_is_still_needed(role):
    """An exemption that no longer applies is a licence nobody revoked."""
    needed = False
    for path in HOSTS_WITH_K8S:
        idx = _index(_k8s_entries(path))
        if "traefik" not in idx or role not in idx:
            continue
        if idx[role] < idx["traefik"] and role in TRAEFIK_CRD_ROLES:
            needed = True
    assert needed, (
        f"'{role}' is in CRD_ORDER_EXEMPT but no longer renders a Traefik CRD before "
        f"traefik. Delete the exemption so the next reorder is checked."
    )
