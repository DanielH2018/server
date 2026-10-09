"""Traefik must render without the LiveSync token gate for a cluster that has no backends for it.

Every router in `livesync-gate-secret.yaml.j2` routes to CouchDB or homelab-mcp, and neither
is in the staging subset. The Secret also reads `homelab_mcp_token` and `livesync_sync_token`,
which staging's secrets file does not carry — those two are the ONLY variables left
unaccounted for once ACME and CrowdSec are gated, so this flag is what lets Traefik deploy to a
staging cluster at all.

Generated staging tokens would not make the flag unnecessary. The routers name backends the
cluster does not run, and staging's secrets file is encrypted to daniel-server's key alone and
holds one key by design (docs/archive/staging-cluster.md, Decision 5), so there is nowhere for a
generated token to go.

The static config's file provider and the Secret are two halves of one mechanism: the provider
names `/etc/traefik-file/livesync-gate.yml`, which only the Secret's volume supplies. Gating
one without the other leaves Traefik reading a path nothing mounts.

The checks every opt-out flag shares — both branches parse, every mount resolves, the flag
defaults on — are `ansible/tests/k8s/test_staging_opt_out_flags_render.py`'s.
"""

import pytest

from lib import yaml_fast

from _k8s_render import render_role_template, traefik_static_config

_ROLE = "traefik"
_FLAG = "traefik_k8s_manage_livesync_gate"
_MOUNT_PATH = "/etc/traefik-file"


def _render(template: str, manage: bool) -> str:
    return render_role_template(_ROLE, template, {_FLAG: manage})


def _pod_spec(manage: bool) -> dict:
    return yaml_fast.safe_load(_render("deployment.yaml.j2", manage))["spec"][
        "template"
    ]["spec"]


def _static_config(manage: bool) -> dict:
    return traefik_static_config({_FLAG: manage})


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
