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
from lib.k8s_roles import MANIFESTS_PRUNE_DEFAULT
from lib.render_context import render_context
from renderer.renderer_lib.task_inputs import include_vars


from validate.k8s_manifests import (
    SHARED_TPL,
    k8s_entries,
    make_env,
    make_lookup,
    register_ansible_filters,
)

ROLE = ANSIBLE / "roles" / "k8s" / "navidrome"


def _render(template: str) -> str:
    ctx = render_context(
        ROLE, overrides={"container_item": k8s_entries()["navidrome"]}, strict=True
    )
    env = make_env([ROLE / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    return env.get_template(template).render(**ctx)


def _routes() -> dict[str, dict]:
    docs = yaml_fast.safe_load_all(_render("ingressroute.yaml.j2"))
    return {d["metadata"]["name"]: d for d in docs if d}


def test_the_route_renders_when_navidrome_has_a_pod():
    """The accept half: the template always renders the route, and the list carries the gate.

    Raising the replica count is the documented way to bring the workload back, and the route
    it lists must then be a real one.
    """
    routes = _routes()
    # One object per host, plus the public name's Subsonic API bypass.
    assert list(routes) == ["navidrome", "navidrome-public", "navidrome-public-rest"]
    for route in routes.values():
        assert route["kind"] == "IngressRoute"
        assert route["spec"]["routes"], "an IngressRoute with no routes matches nothing"


def test_navidrome_trusts_no_user_header_while_rest_skips_authelia():
    """Navidrome honours its reverse-proxy user header on /rest/ too.

    With /rest/ unauthenticated at the edge, enabling that header would let any caller name
    any user, so the deployment must not set either reverse-proxy switch.
    """
    [deployment] = [
        d for d in yaml_fast.safe_load_all(_render("deployment.yaml.j2")) if d
    ]
    [container] = deployment["spec"]["template"]["spec"]["containers"]
    env = {e["name"] for e in container["env"]}
    assert not env & {"ND_REVERSEPROXYWHITELIST", "ND_REVERSEPROXYUSERHEADER"}, env


def _manifests_files(replicas: int) -> list[str]:
    expr = include_vars(ROLE)["manifests_files"]
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
    assert include_vars(ROLE).get("manifests_prune", MANIFESTS_PRUNE_DEFAULT) is True
