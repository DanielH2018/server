"""navidrome renders a route only when it has pods, and deletes the live one when it does not.

Traefik's kubernetescrd provider re-reads every IngressRoute on each config refresh and logs
`no servers found for homelab/navidrome` whenever the EndpointSlice behind one is empty. With
the workload parked at `navidrome_k8s_replicas: 0` that fired every ~20s forever and buried
every other router error in the traefik log (issue #1323).

Both halves are tested because either alone is only half a retirement: rendering nothing leaves
the live IngressRoute serving, since `kubectl apply` only adds and updates.
"""

import sys

from _helpers import ANSIBLE, REPO
from lib import yaml_fast

_REPO = REPO
sys.path.insert(0, str(_REPO / "scripts"))

from validate.k8s_manifests import (  # noqa: E402 — needs the path insert above
    ALL_VARS,
    BASE_CONTEXT,
    SHARED_TPL,
    k8s_entries,
    load_yaml,
    make_env,
    make_lookup,
    register_ansible_filters,
    resolve_vars,
    role_defaults,
)

ROLE = ANSIBLE / "roles" / "k8s" / "navidrome"


def _render_route(replicas: int) -> str:
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)}
    base = resolve_vars(base, base)
    ctx = {
        **base,
        **role_defaults("navidrome", base),
        "container_item": k8s_entries()["navidrome"],
        "navidrome_k8s_replicas": replicas,
    }
    env = make_env([ROLE / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    return env.get_template("ingressroute.yaml.j2").render(**ctx)


def test_the_route_renders_when_navidrome_has_a_pod():
    """The accept half, and the only thing still schema-checking this route.

    `navidrome_k8s_replicas: 0` is what the repo commits, so validate/k8s_manifests.py now
    renders this template empty and stops seeing the route at all. Raising the replica count
    is the documented way to bring the workload back, and it must bring the route with it.
    """
    docs = [d for d in yaml_fast.safe_load_all(_render_route(1)) if d]
    assert len(docs) == 1, docs
    route = docs[0]
    assert route["kind"] == "IngressRoute"
    assert route["metadata"]["name"] == "navidrome"
    assert route["spec"]["routes"], "an IngressRoute with no routes matches nothing"


def test_the_route_is_absent_while_navidrome_is_parked():
    """The reject half. Empty is a valid manifest — `kubectl apply -f <dir>/` skips it."""
    assert [d for d in yaml_fast.safe_load_all(_render_route(0)) if d] == []


def test_the_role_deletes_the_live_route_while_parked():
    """Rendering nothing does not delete anything.

    The live IngressRoute predates any `homelab/role` label, so k8s/manifests' opt-in
    `manifests_prune` structurally cannot match it — the role reconciles its absence with an
    explicit delete instead. Guarded here because the two halves are separately deletable and
    the surviving half reads as a complete fix.
    """
    tasks = yaml_fast.safe_load((ROLE / "tasks" / "main.yml").read_text())
    delete = next(
        (
            t
            for t in tasks
            if "ingressroute" in str(t.get("ansible.builtin.command", ""))
        ),
        None,
    )
    assert delete, "no task deletes the live navidrome IngressRoute"
    cmd = " ".join(delete["ansible.builtin.command"]["cmd"].split())
    assert "kubectl delete ingressroute navidrome" in cmd
    # Without it the steady-state deploy fails once the route is already gone.
    assert "--ignore-not-found" in cmd
    # A dry run must not mutate the cluster, and a parked-only guard is what makes raising
    # the replica count restore the route rather than delete it on every deploy.
    when = " ".join(str(c) for c in delete["when"])
    assert "navidrome_k8s_replicas | int == 0" in when
    assert "k8s_dry_run" in when
    # `[config, deploy]` would be dropped by --skip-tags of either; it pairs with the apply.
    assert delete["tags"] == ["deploy"]
