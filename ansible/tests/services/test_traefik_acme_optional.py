"""Traefik must render a valid Deployment and static config with ACME switched off.

Prod issues certificates through the `cloudflare` ACME DNS-01 resolver. A staging cluster has
no Cloudflare token and must never issue against the real domain's account or rate limit, so
`traefik_k8s_manage_acme` is false there (docs/archive/staging-cluster.md, Decision 4).

ACME is not one block. It spans a resolver in the static config, a `fix-acme-permissions` init
container, the `CF_*` env pair, two volumeMounts, two volumes, a PVC and a Secret — so the
plausible failure is a half-gated template that still names a volume nothing defines, or an
`env:` key with no list under it. Both render as text and are rejected only at admission, which
staging reaches long after the deploy reads green.

Every rule here is a pair — one input it must accept, one it must reject — because a check that
has only ever been observed passing carries no evidence it can fail.

The checks every opt-out flag shares — both branches parse, every mount resolves, the flag
defaults on — are `ansible/tests/k8s/test_staging_opt_out_flags_render.py`'s.
"""

from lib import yaml_fast

from _k8s_render import _inventory_base, render_role_template
from validate.k8s_manifests import K8S_ROLES, load_yaml, resolve_vars

_ROLE = "traefik"
_FLAG = "traefik_k8s_manage_acme"


def _render(template: str, manage_acme: bool) -> str:
    return render_role_template(_ROLE, template, {_FLAG: manage_acme})


def _claims(manage_acme: bool) -> list:
    # Resolved here rather than through render_role_template: `k8s_claims` is a role default
    # computed from the flag, and resolved defaults outrank overrides laid over them, so the flag
    # has to change before the defaults resolve.
    defaults = {
        **load_yaml(K8S_ROLES / _ROLE / "defaults" / "main.yml"),
        _FLAG: manage_acme,
    }
    return resolve_vars(defaults, _inventory_base())["k8s_claims"]


def _deployment(manage_acme: bool) -> dict:
    return yaml_fast.safe_load(_render("deployment.yaml.j2", manage_acme))


def _pod_spec(manage_acme: bool) -> dict:
    return _deployment(manage_acme)["spec"]["template"]["spec"]


def test_resolver_is_declared_with_acme_on_and_absent_with_it_off() -> None:
    config = yaml_fast.safe_load(_render("static-config.yaml.j2", True))["data"][
        "traefik.yml"
    ]
    assert "certificatesResolvers" in config

    config = yaml_fast.safe_load(_render("static-config.yaml.j2", False))["data"][
        "traefik.yml"
    ]
    assert "certificatesResolvers" not in config


def test_acme_init_container_ships_with_acme_on_and_not_with_it_off() -> None:
    names = [c["name"] for c in _pod_spec(True)["initContainers"]]
    assert "fix-acme-permissions" in names

    names = [c["name"] for c in _pod_spec(False)["initContainers"]]
    assert "fix-acme-permissions" not in names
    assert names, (
        "gating ACME must not empty initContainers — crowdsec's still belongs there"
    )


def test_cloudflare_env_is_present_with_acme_on_and_the_key_is_gone_with_it_off() -> (
    None
):
    traefik = next(c for c in _pod_spec(True)["containers"] if c["name"] == "traefik")
    assert {"CF_API_EMAIL", "CF_DNS_API_TOKEN_FILE"} <= {
        e["name"] for e in traefik["env"]
    }

    traefik = next(c for c in _pod_spec(False)["containers"] if c["name"] == "traefik")
    # Not an empty list: `env:` with nothing under it parses as null, and the API server
    # rejects that on a container spec. The whole key has to go.
    assert "env" not in traefik


def test_acme_volumes_are_declared_with_acme_on_and_absent_with_it_off() -> None:
    declared = {v["name"] for v in _pod_spec(True)["volumes"]}
    assert {"traefik-acme", "traefik-cloudflare"} <= declared

    declared = {v["name"] for v in _pod_spec(False)["volumes"]}
    assert not {"traefik-acme", "traefik-cloudflare"} & declared


def test_acme_claim_is_declared_with_acme_on_and_absent_with_it_off() -> None:
    assert _claims(True) == [
        {"name": "traefik-acme", "storage_class": "longhorn", "size": "128Mi"}
    ]

    assert _claims(False) == []
