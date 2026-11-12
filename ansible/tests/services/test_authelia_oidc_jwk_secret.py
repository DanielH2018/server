"""authelia's OIDC signing key lives in Secret authelia-oidc-jwk, behind authelia_k8s_manage_oidc.

The key is a multi-line PEM block, so no Flux placeholder can carry it, and target slice R2's
PR 7 moved it out of authelia-config. Three things have to agree for the portal to start:

- authelia passes explicit file lists, and a passed list wins whole, so a template the
  `manifests_secret_files` expression does not name is never rendered. The Deployment would
  then mount a Secret no deploy wrote. The lists are evaluated through the renderer's
  environment, because `lib.k8s_roles.resolved_manifest_files` counts every basename in a
  conditional branch and would pass with the lever's arm broken.
- The Deployment's volume, mount and `X_AUTHELIA_CONFIG_FILTERS` follow the same lever.
- With the filter on, Authelia runs Go templating over the whole configuration.yml, so a real
  value holding `{{` would stop it starting. `cluster_vars.py write` refuses such a value, and
  `scripts/secrets_mgmt/tests/test_cluster_vars_write.py` holds that refusal.
"""

from typing import cast

import pytest
from _helpers import ROLES, load_defaults
from _k8s_render import render_role_template
from renderer.renderer_lib.environment import evaluate, renderer_env

ROLE_DIR = ROLES / "k8s" / "authelia"
LEVER = "authelia_k8s_manage_oidc"
JWK_SECRET = "oidc-jwk-secret.yaml"


def _lists(lever: bool) -> tuple[list[str], list[str]]:
    """`(manifests_files, manifests_secret_files)` as the include passes them, at `lever`."""
    defaults = load_defaults(ROLE_DIR)
    variables = {
        name: defaults[name]
        for name in (
            "authelia_k8s_manage_crowdsec",
            "authelia_k8s_redis_sessions",
        )
    } | {LEVER: lever}
    env = renderer_env(ROLE_DIR / "templates")
    files, secret = (
        list(cast(list[str], evaluate(env, defaults[key], variables)))
        for key in ("manifests_files", "manifests_secret_files")
    )
    return files, secret


def test_the_lever_on_lists_the_jwk_secret_as_a_secret_file():
    files, secret = _lists(True)
    assert JWK_SECRET in secret
    assert JWK_SECRET not in files
    assert "config-secret.yaml" in secret


def test_the_lever_off_lists_the_jwk_secret_nowhere():
    files, secret = _lists(False)
    assert JWK_SECRET not in secret + files
    assert "config-secret.yaml" in secret


# `-e authelia_k8s_manage_oidc=false` arrives as a string, which the list reads through `| bool`.
@pytest.mark.parametrize("off_value", [False, "false"])
def test_the_deployment_mounts_the_jwk_secret_only_with_the_lever_on(off_value):
    on = render_role_template("authelia", "deployment.yaml.j2")
    off = render_role_template("authelia", "deployment.yaml.j2", {LEVER: off_value})
    for marker in ("secretName: authelia-oidc-jwk", "X_AUTHELIA_CONFIG_FILTERS"):
        assert marker in on
        assert marker not in off
