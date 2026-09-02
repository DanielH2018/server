"""Traefik must render a valid Deployment and static config with ACME switched off.

Prod issues certificates through the `cloudflare` ACME DNS-01 resolver. A staging cluster has
no Cloudflare token and must never issue against the real domain's account or rate limit, so
`traefik_k8s_manage_acme` is false there (docs/staging-cluster.md, Decision 4).

ACME is not one block. It spans a resolver in the static config, a `fix-acme-permissions` init
container, the `CF_*` env pair, two volumeMounts, two volumes, a PVC and a Secret — so the
plausible failure is a half-gated template that still names a volume nothing defines, or an
`env:` key with no list under it. Both render as text and are rejected only at admission, which
staging reaches long after the deploy reads green.

Every rule here is a pair — one input it must accept, one it must reject — because a check that
has only ever been observed passing carries no evidence it can fail.
"""

from __future__ import annotations

import sys

import pytest
import yaml
from _helpers import REPO

_REPO = REPO
sys.path.insert(0, str(_REPO / "scripts"))

from validate.k8s_manifests import (  # noqa: E402 — needs the path insert above
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_ROLES,
    SHARED_TPL,
    k8s_entries,
    load_yaml,
    make_env,
    make_lookup,
    register_ansible_filters,
    render_or_error,
    resolve_vars,
    role_defaults,
)

_ROLE = "traefik"
_FLAG = "traefik_k8s_manage_acme"


def _render(template: str, manage_acme: bool) -> str:
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)}
    base = resolve_vars(base, base)
    role_dir = K8S_ROLES / _ROLE
    ctx = {
        **base,
        **role_defaults(_ROLE, base),
        "container_item": k8s_entries()[_ROLE],
        _FLAG: manage_acme,
    }
    env = make_env([role_dir / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, template, ctx)
    assert rendered is not None, (
        f"{template} failed to render with {_FLAG}={manage_acme}: {err}"
    )
    return rendered


def _deployment(manage_acme: bool) -> dict:
    return yaml.safe_load(_render("deployment.yaml.j2", manage_acme))


def _pod_spec(manage_acme: bool) -> dict:
    return _deployment(manage_acme)["spec"]["template"]["spec"]


@pytest.mark.parametrize("template", ["deployment.yaml.j2", "static-config.yaml.j2"])
@pytest.mark.parametrize("manage_acme", [True, False])
def test_both_branches_parse_as_yaml(template: str, manage_acme: bool) -> None:
    """A stray conditional shifts indentation, which shows up here and nowhere else."""
    assert yaml.safe_load(_render(template, manage_acme)) is not None


def test_resolver_is_declared_with_acme_on_and_absent_with_it_off() -> None:
    config = yaml.safe_load(_render("static-config.yaml.j2", True))["data"][
        "traefik.yml"
    ]
    assert "certificatesResolvers" in config

    config = yaml.safe_load(_render("static-config.yaml.j2", False))["data"][
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


@pytest.mark.parametrize("manage_acme", [True, False])
def test_every_mount_resolves_to_a_declared_volume(manage_acme: bool) -> None:
    """The half-gated failure: a mount left behind by a volume that dropped out.

    Nothing upstream catches it — the manifest is valid YAML and applies cleanly; the pod
    simply never starts.
    """
    spec = _pod_spec(manage_acme)
    declared = {v["name"] for v in spec["volumes"]}
    for container in spec.get("initContainers", []) + spec["containers"]:
        for mount in container.get("volumeMounts", []):
            assert mount["name"] in declared, (
                f"{container['name']} mounts {mount['name']}, which no volume declares "
                f"({_FLAG}={manage_acme})"
            )


def test_acme_volumes_are_declared_with_acme_on_and_absent_with_it_off() -> None:
    declared = {v["name"] for v in _pod_spec(True)["volumes"]}
    assert {"traefik-acme", "traefik-cloudflare"} <= declared

    declared = {v["name"] for v in _pod_spec(False)["volumes"]}
    assert not {"traefik-acme", "traefik-cloudflare"} & declared


def test_prod_manages_acme() -> None:
    """The flag defaults on, so no cluster loses certificate issuance by omission."""
    defaults = load_yaml(K8S_ROLES / _ROLE / "defaults" / "main.yml")
    assert defaults[_FLAG] is True
    assert defaults["traefik_k8s_manage_cloudflare_drift_check"] is True
