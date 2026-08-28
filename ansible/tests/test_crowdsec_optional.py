"""Traefik and Authelia must render without CrowdSec for a cluster that does not run it.

The `crowdsec` role is not in the staging cluster's subset, so `crowdsec:8080` does not
resolve there. An ungated agent sidecar crashloops against a LAPI that is not listening and
the pod never reaches Ready — a false failure, which is the class docs/staging-cluster.md
Decision 5 exists to avoid.

The two surfaces are asymmetric and each has its own flag. Traefik carries the bouncer
plugin, the entrypoint Middleware that invokes it, an agent sidecar and five volumes;
Authelia carries only an agent, its seed init container and four volumes. Neither flag is
shared, because one variable would have to live in group_vars/all.yml — a path the GitOps
deployer treats as broad.

Two shapes are what the pairs below exist to catch, and neither is visible from a passing
render. A mount left behind by a volume that dropped out is valid YAML and is rejected only
at admission. A dangling `crowdsec` Middleware reference on an entrypoint is worse: Traefik
disables plugins silently when the bouncer is gone, so the reference resolves to nothing and
every request through that entrypoint fails while the pod reads healthy.

What this suite does NOT prove: that either pod reaches Ready with CrowdSec off. Nothing
here starts a container. That evidence comes from the staging bring-up.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts"))

from validate_k8s_manifests import (  # noqa: E402 — needs the path insert above
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

_FLAGS = {
    "traefik": "traefik_k8s_manage_crowdsec",
    "authelia": "authelia_k8s_manage_crowdsec",
}


def _render(role: str, template: str, **overrides) -> str:
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)}
    base = resolve_vars(base, base)
    role_dir = K8S_ROLES / role
    ctx = {
        **base,
        **role_defaults(role, base),
        "container_item": k8s_entries()[role],
        **overrides,
    }
    env = make_env([role_dir / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, template, ctx)
    assert err is None, f"{role}/{template} failed to render with {overrides}: {err}"
    return rendered


def _docs(role: str, template: str, manage: bool) -> list[dict]:
    text = _render(role, template, **{_FLAGS[role]: manage})
    return [d for d in yaml.safe_load_all(text) if d is not None]


def _pod_spec(role: str, manage: bool, **extra) -> dict:
    text = _render(role, "deployment.yaml.j2", **{_FLAGS[role]: manage, **extra})
    return yaml.safe_load(text)["spec"]["template"]["spec"]


def _static_config(manage: bool) -> dict:
    doc = _docs("traefik", "static-config.yaml.j2", manage)[0]
    return yaml.safe_load(doc["data"]["traefik.yml"])


@pytest.mark.parametrize(
    ("role", "template"),
    [
        ("traefik", "deployment.yaml.j2"),
        ("traefik", "static-config.yaml.j2"),
        ("traefik", "dynamic.yaml.j2"),
        ("authelia", "deployment.yaml.j2"),
    ],
)
@pytest.mark.parametrize("manage", [True, False])
def test_both_branches_parse_as_yaml(role: str, template: str, manage: bool) -> None:
    """A stray conditional shifts indentation, which shows up here and nowhere else."""
    assert _docs(role, template, manage)


@pytest.mark.parametrize("role", sorted(_FLAGS))
def test_nothing_names_crowdsec_once_the_flag_is_off(role: str) -> None:
    """The completeness rule: no CrowdSec VARIABLE may still be read with the flag off.

    A text scan for the word cannot express this. Both roles carry prose that legitimately
    names CrowdSec — the traefik-logs mount, the access-log-rotate rationale, Authelia's
    log-path comment — and Authelia's sits inside a block scalar, so it survives parsing as
    string content rather than as a comment.

    Instead every CrowdSec variable is given a sentinel value. A reference that renders is a
    reference that survived, wherever it hides. `crowdsec-secret.yaml.j2` is excluded because
    the flag retires it by dropping it from manifests_secret_files, not by gating its body.
    """
    sentinel = "SURVIVING-CROWDSEC-REFERENCE"
    role_vars = role_defaults(role, {})
    crowdsec_vars = {
        name: sentinel
        for name in list(role_vars) + ["crowdsec_k8s_image", "crowdsec_k8s_lapi_port"]
        # The flag's own name matches, and a sentinel STRING is truthy — setting it here
        # would silently switch the subsystem back on and the check would pass vacuously.
        if ("crowdsec" in name or "bouncer" in name) and name != _FLAGS[role]
    }
    assert crowdsec_vars, (
        f"{role} names no CrowdSec variable — the sentinel proves nothing"
    )

    for template in sorted(
        p.name
        for p in (K8S_ROLES / role / "templates").glob("*.yaml.j2")
        if p.name != "crowdsec-secret.yaml.j2"
    ):
        on = _render(role, template, **{_FLAGS[role]: True, **crowdsec_vars})
        off = _render(role, template, **{_FLAGS[role]: False, **crowdsec_vars})
        assert sentinel not in off, (
            f"{role}/{template} still reads a CrowdSec variable with {_FLAGS[role]} off"
        )
        if template in {
            "deployment.yaml.j2",
            "dynamic.yaml.j2",
            "static-config.yaml.j2",
        }:
            assert sentinel in on, (
                f"{role}/{template} reads no CrowdSec variable even with the flag ON — "
                "the sentinel is not reaching this template, so the check above is inert"
            )


def test_the_middleware_ships_with_crowdsec_on_and_not_with_it_off() -> None:
    names = [d["metadata"]["name"] for d in _docs("traefik", "dynamic.yaml.j2", True)]
    assert "crowdsec" in names

    names = [d["metadata"]["name"] for d in _docs("traefik", "dynamic.yaml.j2", False)]
    assert "crowdsec" not in names
    assert names, "gating the Middleware must not empty dynamic.yaml"


def test_the_bouncer_plugin_is_declared_with_crowdsec_on_and_gone_with_it_off() -> None:
    assert "bouncer" in _static_config(True)["experimental"]["plugins"]
    # The whole `experimental` key goes: the bouncer is its only entry, and Traefik disables
    # plugins silently rather than failing when one cannot be resolved.
    assert "experimental" not in _static_config(False)


def test_no_entrypoint_references_the_middleware_once_it_is_gone() -> None:
    """A dangling reference here fails requests while leaving the pod healthy."""
    points = _static_config(True)["entryPoints"]
    assert any("crowdsec" in m for m in points["http"]["http"]["middlewares"])
    assert any("crowdsec" in m for m in points["https"]["http"]["middlewares"])

    points = _static_config(False)["entryPoints"]
    # http's chain held crowdsec alone, so the key goes; https keeps its other two.
    assert "middlewares" not in points["http"]["http"]
    https_chain = points["https"]["http"]["middlewares"]
    assert not any("crowdsec" in m for m in https_chain)
    assert any("default-headers" in m for m in https_chain)
    assert any("compress" in m for m in https_chain)


@pytest.mark.parametrize("role", sorted(_FLAGS))
def test_the_agent_sidecar_ships_with_crowdsec_on_and_not_with_it_off(
    role: str,
) -> None:
    names = [c["name"] for c in _pod_spec(role, True)["containers"]]
    assert "crowdsec-agent" in names

    names = [c["name"] for c in _pod_spec(role, False)["containers"]]
    assert "crowdsec-agent" not in names
    assert names, "gating the sidecar must not empty containers"


def test_authelia_drops_the_init_container_key_rather_than_emptying_it() -> None:
    assert "crowdsec-config-install" in [
        c["name"] for c in _pod_spec("authelia", True)["initContainers"]
    ]
    # CrowdSec's is the only one, so the key goes rather than being left with nothing under it.
    assert "initContainers" not in _pod_spec("authelia", False)


def test_traefik_keeps_its_init_container_key_while_acme_still_needs_it() -> None:
    """Traefik has two conditional init containers, so the key follows their disjunction."""
    spec = _pod_spec("traefik", False, traefik_k8s_manage_acme=True)
    assert [c["name"] for c in spec["initContainers"]] == ["fix-acme-permissions"]

    spec = _pod_spec("traefik", False, traefik_k8s_manage_acme=False)
    assert "initContainers" not in spec


@pytest.mark.parametrize("role", sorted(_FLAGS))
@pytest.mark.parametrize("manage", [True, False])
def test_every_mount_resolves_to_a_declared_volume(role: str, manage: bool) -> None:
    """The half-gated failure: a mount left behind by a volume that dropped out."""
    spec = _pod_spec(role, manage)
    declared = {v["name"] for v in spec["volumes"]}
    for container in spec.get("initContainers", []) + spec["containers"]:
        for mount in container.get("volumeMounts", []):
            assert mount["name"] in declared, (
                f"{role}/{container['name']} mounts {mount['name']}, which no volume "
                f"declares ({_FLAGS[role]}={manage})"
            )


@pytest.mark.parametrize(
    ("role", "volume", "writer"),
    [("traefik", "traefik-logs", "traefik"), ("authelia", "authelia-logs", "authelia")],
)
def test_the_shared_log_volume_survives_without_crowdsec(
    role: str, volume: str, writer: str
) -> None:
    """The agent tails these, but it is not what writes them.

    Traefik's accessLog and Authelia's `log.file_path` both name a path on this volume, and
    access-log-rotate tails Traefik's. Gating them out with the agent would take the log with
    the reader.
    """
    spec = _pod_spec(role, False)
    assert volume in {v["name"] for v in spec["volumes"]}
    container = next(c for c in spec["containers"] if c["name"] == writer)
    assert volume in {m["name"] for m in container["volumeMounts"]}


@pytest.mark.parametrize("role", sorted(_FLAGS))
def test_prod_manages_crowdsec(role: str) -> None:
    """Both flags default on, so no cluster loses the WAF or its signals by omission."""
    assert load_yaml(K8S_ROLES / role / "defaults" / "main.yml")[_FLAGS[role]] is True
