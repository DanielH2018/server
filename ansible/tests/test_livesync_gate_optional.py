"""Traefik must render without the LiveSync token gate for a cluster that has no backends for it.

Every router in `livesync-gate-secret.yaml.j2` routes to CouchDB or homelab-mcp, and neither
is in the staging subset. The Secret also reads `homelab_mcp_token` and `livesync_sync_token`,
which staging's secrets file does not carry — a census of the traefik role on 2026-08-28 found
those two to be the ONLY variables still unaccounted for once ACME and CrowdSec are gated, so
this flag is what lets Traefik deploy to a staging cluster at all.

Slice 2a recorded that this needed no flag, on the reasoning that generated staging tokens
would render harmless routes. That was wrong on both halves. The routers name backends the
cluster does not run, and staging's secrets file is encrypted to daniel-server's key alone and
holds one key by design (docs/staging-cluster.md, Decision 5), so there is nowhere for a
generated token to go.

The static config's file provider and the Secret are two halves of one mechanism: the provider
names `/etc/traefik-file/livesync-gate.yml`, which only the Secret's volume supplies. Gating
one without the other leaves Traefik reading a path nothing mounts.
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

_ROLE = "traefik"
_FLAG = "traefik_k8s_manage_livesync_gate"
_MOUNT_PATH = "/etc/traefik-file"


def _render(template: str, manage: bool) -> str:
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)}
    base = resolve_vars(base, base)
    role_dir = K8S_ROLES / _ROLE
    ctx = {
        **base,
        **role_defaults(_ROLE, base),
        "container_item": k8s_entries()[_ROLE],
        _FLAG: manage,
    }
    env = make_env([role_dir / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, template, ctx)
    assert err is None, f"{template} failed to render with {_FLAG}={manage}: {err}"
    return rendered


def _pod_spec(manage: bool) -> dict:
    return yaml.safe_load(_render("deployment.yaml.j2", manage))["spec"]["template"][
        "spec"
    ]


def _static_config(manage: bool) -> dict:
    doc = yaml.safe_load(_render("static-config.yaml.j2", manage))
    return yaml.safe_load(doc["data"]["traefik.yml"])


@pytest.mark.parametrize("template", ["deployment.yaml.j2", "static-config.yaml.j2"])
@pytest.mark.parametrize("manage", [True, False])
def test_both_branches_parse_as_yaml(template: str, manage: bool) -> None:
    assert yaml.safe_load(_render(template, manage)) is not None


def test_the_file_provider_is_declared_with_the_gate_on_and_gone_with_it_off() -> None:
    providers = _static_config(True)["providers"]
    assert providers["file"]["filename"].startswith(_MOUNT_PATH)

    providers = _static_config(False)["providers"]
    assert "file" not in providers
    # kubernetesCRD is what every IngressRoute in the fleet depends on; gating the file
    # provider must not take the providers block with it.
    assert "kubernetesCRD" in providers


def test_the_gate_volume_ships_with_it_on_and_not_with_it_off() -> None:
    assert "traefik-livesync-gate" in {v["name"] for v in _pod_spec(True)["volumes"]}
    assert "traefik-livesync-gate" not in {
        v["name"] for v in _pod_spec(False)["volumes"]
    }


@pytest.mark.parametrize("manage", [True, False])
def test_the_provider_and_its_mount_are_gated_together(manage: bool) -> None:
    """Half-gating leaves Traefik reading a path nothing mounts, which it does not report."""
    traefik = next(c for c in _pod_spec(manage)["containers"] if c["name"] == "traefik")
    mounted = {m["mountPath"] for m in traefik["volumeMounts"]}
    declares_provider = "file" in _static_config(manage)["providers"]
    assert declares_provider == (_MOUNT_PATH in mounted), (
        f"the file provider and its {_MOUNT_PATH} mount disagree with {_FLAG}={manage}"
    )


@pytest.mark.parametrize("manage", [True, False])
def test_every_mount_resolves_to_a_declared_volume(manage: bool) -> None:
    spec = _pod_spec(manage)
    declared = {v["name"] for v in spec["volumes"]}
    for container in spec.get("initContainers", []) + spec["containers"]:
        for mount in container.get("volumeMounts", []):
            assert mount["name"] in declared, (
                f"{container['name']} mounts {mount['name']}, which no volume declares "
                f"({_FLAG}={manage})"
            )


def test_prod_manages_the_livesync_gate() -> None:
    """The flag defaults on, so no cluster loses its token gate by omission."""
    assert load_yaml(K8S_ROLES / _ROLE / "defaults" / "main.yml")[_FLAG] is True
