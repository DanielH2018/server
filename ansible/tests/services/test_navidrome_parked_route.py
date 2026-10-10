"""navidrome lists its route only when it has pods, and the armed prune deletes it when not.

Traefik's kubernetescrd provider re-reads every IngressRoute on each config refresh and logs
`no servers found for homelab/navidrome` whenever the EndpointSlice behind one is empty. With
the workload parked at `navidrome_k8s_replicas: 0` that fired every ~20s forever and buried
every other router error in the traefik log.

Both halves are tested because either alone is only half a retirement: dropping the name
leaves the live IngressRoute serving unless the role arms `manifests_prune`, since
`kubectl apply` only adds and updates.
"""

from _helpers import ANSIBLE
from lib import yaml_fast
from lib.render_context import render_context


from validate.k8s_manifests import (
    SHARED_TPL,
    k8s_entries,
    make_env,
    make_lookup,
    register_ansible_filters,
)

ROLE = ANSIBLE / "roles" / "k8s" / "navidrome"


def _render_route() -> str:
    ctx = render_context(
        ROLE, overrides={"container_item": k8s_entries()["navidrome"]}, strict=True
    )
    env = make_env([ROLE / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    return env.get_template("ingressroute.yaml.j2").render(**ctx)


def test_the_route_renders_when_navidrome_has_a_pod():
    """The accept half: the template always renders the route, and the list carries the gate.

    Raising the replica count is the documented way to bring the workload back, and the route
    it lists must then be a real one.
    """
    docs = [d for d in yaml_fast.safe_load_all(_render_route()) if d]
    # One object per host: the `.local.` route and its public twin.
    assert [d["metadata"]["name"] for d in docs] == ["navidrome", "navidrome-public"], (
        docs
    )
    for route in docs:
        assert route["kind"] == "IngressRoute"
        assert route["spec"]["routes"], "an IngressRoute with no routes matches nothing"


def _deploy_vars() -> dict:
    tasks = yaml_fast.safe_load((ROLE / "tasks" / "main.yml").read_text())
    (deploy,) = [t for t in tasks if "ansible.builtin.include_role" in t]
    return deploy["vars"]


def _manifests_files(replicas: int) -> list[str]:
    expr = _deploy_vars()["manifests_files"]
    env = make_env([])
    return yaml_fast.safe_load(
        env.from_string(expr).render(navidrome_k8s_replicas=replicas).replace("'", '"')
    )


def test_the_route_is_listed_only_while_navidrome_has_a_pod():
    """The reject half: a parked role names no route, so the prune deletes the live one."""
    assert "ingressroute.yaml" in _manifests_files(1)
    assert "ingressroute.yaml" not in _manifests_files(0)
    # The prune needs something to keep, and the workload itself must stay listed.
    assert {"deployment.yaml", "service.yaml"} <= set(_manifests_files(0))


def test_the_role_arms_the_prune_that_deletes_the_parked_route():
    """Dropping a name from manifests_files deletes nothing unless the prune is armed."""
    # An include that omits the flag takes the k8s/manifests default, which is armed.
    defaults = yaml_fast.safe_load(
        (ANSIBLE / "roles/k8s/manifests/defaults/main.yml").read_text()
    )
    assert _deploy_vars().get("manifests_prune", defaults["manifests_prune"]) is True
