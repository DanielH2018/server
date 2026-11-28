"""Authelia's `vault-mcp` OIDC client admits the operator alone, behind 2FA, PKCE and consent.

The client is the only way into the vault connector on its public route, so its policy and
grant settings are the gate. `authelia_k8s_vault_mcp_client` is the switch that cuts the
connector off.
"""

from lib import yaml_fast
from _k8s_render import render_role_template

OPERATOR = "operator-under-test"


def _config(**overrides):
    secret = yaml_fast.safe_load(
        render_role_template(
            "authelia",
            "config-secret.yaml.j2",
            {"authelia_user": OPERATOR, **overrides},
        )
    )
    return yaml_fast.safe_load(secret["stringData"]["configuration.yml"])


def _vault_mcp_clients(oidc):
    return [c for c in oidc["clients"] if c["client_id"] == "vault-mcp"]


def test_the_vault_mcp_client_admits_only_the_operator_at_two_factor():
    oidc = _config()["identity_providers"]["oidc"]
    (client,) = _vault_mcp_clients(oidc)
    policy = oidc["authorization_policies"][client["authorization_policy"]]
    assert policy == {
        "default_policy": "deny",
        "rules": [{"policy": "two_factor", "subject": f"user:{OPERATOR}"}],
    }


def test_the_vault_mcp_client_needs_s256_pkce_and_explicit_consent():
    (client,) = _vault_mcp_clients(_config()["identity_providers"]["oidc"])
    assert {
        key: client[key]
        for key in (
            "public",
            "require_pkce",
            "pkce_challenge_method",
            "grant_types",
            "token_endpoint_auth_method",
            "consent_mode",
        )
    } == {
        "public": False,
        "require_pkce": True,
        "pkce_challenge_method": "S256",
        "grant_types": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_method": "client_secret_post",
        "consent_mode": "explicit",
    }


def test_the_switch_off_renders_no_vault_mcp_client_policy_or_lifespan():
    oidc = _config(authelia_k8s_vault_mcp_client=False)["identity_providers"]["oidc"]
    assert _vault_mcp_clients(oidc) == []
    assert "vault_mcp" not in (oidc.get("authorization_policies") or {})
    assert "vault_mcp" not in oidc["lifespans"]["custom"]
    assert [c["client_id"] for c in oidc["clients"]], "no other client rendered"
