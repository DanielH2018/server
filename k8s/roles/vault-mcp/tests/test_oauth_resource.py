import base64
import hashlib
import hmac
import json
from dataclasses import replace

import jwt
import pytest
import yaml
from _k8s_render import render_role_template
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from oauth_resource import ResourceServer

RESOURCE = "https://vault-mcp.example.com/mcp"
ISSUER = "https://auth.example.com"
KID = "authelia-rs256"
AUTHELIA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISSUED = 1_700_000_000  # 2023-11-14
STILL_VALID = 4_102_444_800  # 2100-01-01, so the token outlives the suite


def _claims(**changes):
    claims = {
        "iss": ISSUER,
        "aud": [RESOURCE],
        "sub": "operator",
        "client_id": "vault-mcp",
        "iat": ISSUED,
        "exp": STILL_VALID,
    }
    claims.update(changes)
    return {k: v for k, v in claims.items() if v is not None}


def _token(key=AUTHELIA_KEY, typ="at+jwt", **changes):
    return jwt.encode(
        _claims(**changes), key, algorithm="RS256", headers={"kid": KID, "typ": typ}
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _hs256_keyed_with_the_public_key() -> str:
    public_pem = AUTHELIA_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    signing_input = ".".join(
        [
            _b64(json.dumps({"alg": "HS256", "typ": "at+jwt", "kid": KID}).encode()),
            _b64(json.dumps(_claims()).encode()),
        ]
    )
    mac = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(mac)}"


@pytest.fixture
def oauth():
    return ResourceServer(
        resource=RESOURCE,
        issuer=ISSUER,
        signing_key=lambda token: AUTHELIA_KEY.public_key(),
    )


def test_an_access_token_authelia_issued_for_the_connector_is_valid(oauth):
    assert oauth.token_valid(f"Bearer {_token()}")
    assert oauth.token_valid(f"bearer {_token(typ='application/at+jwt')}")


@pytest.mark.parametrize(
    "header", [None, "", "Bearer", "Bearer ", "Basic dXNlcjpwYXNz"]
)
def test_a_request_without_a_bearer_token_is_refused(oauth, header):
    assert not oauth.token_valid(header)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(
            _token(key=OTHER_KEY), id="signed-by-another-key-under-authelias-kid"
        ),
        pytest.param(jwt.encode(_claims(), "", algorithm="none"), id="alg-none"),
        pytest.param(
            _hs256_keyed_with_the_public_key(), id="hs256-keyed-with-the-public-key"
        ),
        pytest.param(_token(aud="vault-mcp"), id="an-id-token-audience"),
        pytest.param(
            _token(aud=["https://other.example.com/mcp"]), id="another-resource"
        ),
        pytest.param(_token(iss="https://auth.local.example.com"), id="another-issuer"),
        pytest.param(_token(exp=ISSUED + 3600), id="expired"),
        pytest.param(_token(exp=None), id="no-expiry"),
        pytest.param(_token(typ="JWT"), id="an-id-token-type"),
        pytest.param("not.a.jwt", id="garbage"),
    ],
)
def test_a_forged_or_misissued_token_is_refused(oauth, token):
    assert not oauth.token_valid(f"Bearer {token}")


def test_a_token_is_refused_when_authelias_keys_cannot_be_fetched(oauth):
    def unreachable(token):
        raise jwt.PyJWKClientConnectionError("connection refused")

    assert not replace(oauth, signing_key=unreachable).token_valid(f"Bearer {_token()}")


def test_the_metadata_names_the_connector_and_authelia_at_both_paths(oauth):
    assert oauth.metadata() == {
        "resource": RESOURCE,
        "authorization_servers": [ISSUER],
        "scopes_supported": ["openid", "offline_access"],
        "bearer_methods_supported": ["header"],
    }
    assert oauth.metadata_paths() == (
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    )
    assert oauth.challenge() == (
        'Bearer error="invalid_token", resource_metadata='
        '"https://vault-mcp.example.com/.well-known/oauth-protected-resource/mcp"'
    )


@pytest.mark.parametrize(
    "unset", ["VAULT_MCP_RESOURCE_URL", "VAULT_MCP_ISSUER", "VAULT_MCP_JWKS_URL"]
)
def test_the_server_refuses_to_start_without_its_oauth_settings(unset):
    env = {
        "VAULT_MCP_RESOURCE_URL": RESOURCE,
        "VAULT_MCP_ISSUER": ISSUER,
        "VAULT_MCP_JWKS_URL": "https://auth.local.example.com/jwks.json",
    }
    with pytest.raises(SystemExit, match=unset):
        ResourceServer.from_env({**env, unset: ""})


def test_authelia_grants_the_audience_and_issuer_the_rendered_server_requires():
    # fact: k8s/roles/authelia/CLAUDE.md#OIDC clients
    pod = yaml.safe_load(render_role_template("vault-mcp", "deployment.yaml.j2"))[
        "spec"
    ]["template"]
    (server,) = [c for c in pod["spec"]["containers"] if c["name"] == "vault-mcp"]
    oauth = ResourceServer.from_env({e["name"]: e.get("value") for e in server["env"]})
    config = yaml.safe_load(
        yaml.safe_load(render_role_template("authelia", "config-secret.yaml.j2"))[
            "stringData"
        ]["configuration.yml"]
    )
    (client,) = [
        c
        for c in config["identity_providers"]["oidc"]["clients"]
        if c["client_id"] == "vault-mcp"
    ]
    assert client["audience"] == [oauth.resource]
    assert client["scopes"] == oauth.metadata()["scopes_supported"]
    assert client["access_token_signed_response_alg"] == "RS256"
    assert client["redirect_uris"] == ["https://claude.ai/api/mcp/auth_callback"]
    assert config["session"]["cookies"][1]["authelia_url"] == oauth.issuer
