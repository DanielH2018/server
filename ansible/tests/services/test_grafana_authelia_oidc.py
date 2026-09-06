"""Grafana's Authelia OIDC login: the two settings pairs that fail silently when they drift.

Grafana logs in through Authelia's OIDC provider (issue #1374) instead of asking for the admin
password a second time behind a route Authelia had already authenticated. Two couplings across
the two roles decide whether that works, and neither shows up as a broken pod:

- **PKCE.** `require_pkce: true` on the Authelia client and `GF_AUTH_GENERIC_OAUTH_USE_PKCE` on
  the Grafana Deployment are one setting written twice. Either one alone makes the authorize
  call fail, and the failure is a redirect back to a login page — indistinguishable from a
  session that simply lapsed.
- **The `groups` scope.** `GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH` reads `groups[*]`. Drop
  `groups` from the client's `scopes` and the claim is absent, every user silently lands
  Viewer, and nothing anywhere reports a problem.
- **The `groups` claim in the ID TOKEN.** Granting the scope is not enough. Authelia serves
  `groups` from the userinfo endpoint by default, Grafana evaluates the role path against the
  ID token first, and this role path always yields a value there because of its `|| 'Viewer'`
  branch — so userinfo is never consulted. Measured live 2026-09-06: a user in `admins` got
  `[{"orgId":1,"name":"Main Org.","role":"Viewer"}]` from `/api/user/orgs` behind a green pod,
  a green health gate and a passing `-m ui` suite. A `claims_policy` fixes it.

Each rule is a `..._is_clean` / `..._is_flagged` pair over a predicate, so a rule that stopped
matching fails its own test rather than passing vacuously. The pair is then applied to the
real rendered manifests, with a non-vacuity assertion that the `grafana` client was found at
all — a census that silently finds nothing would otherwise pass every check below.
"""

import pytest
from lib import yaml_fast
from _k8s_render import rendered_docs

CLIENT_ID = "grafana"


def oidc_provider(docs):
    """The rendered Authelia `identity_providers.oidc` block.

    The config is a YAML document nested inside a Secret's `stringData`, so it has to be
    loaded twice — the outer manifest, then the `configuration.yml` string it carries.
    """
    for role, _name, doc in docs:
        if role != "authelia" or not isinstance(doc, dict):
            continue
        raw = (doc.get("stringData") or {}).get("configuration.yml")
        if not raw:
            continue
        config = yaml_fast.safe_load(raw)
        return (config.get("identity_providers") or {}).get("oidc") or {}
    return {}


def oidc_clients(docs):
    """Every OIDC client block in the rendered Authelia config, keyed by client_id.

    The config is a YAML document nested inside a Secret's `stringData`, so it has to be
    loaded twice — the outer manifest, then the `configuration.yml` string it carries.
    """
    for role, _name, doc in docs:
        if role != "authelia" or not isinstance(doc, dict):
            continue
        raw = (doc.get("stringData") or {}).get("configuration.yml")
        if not raw:
            continue
        config = yaml_fast.safe_load(raw)
        providers = (config.get("identity_providers") or {}).get("oidc") or {}
        return {c["client_id"]: c for c in providers.get("clients") or []}
    return {}


def grafana_env(docs):
    """The Grafana container's env, as a name -> raw entry mapping."""
    for role, _name, doc in docs:
        if role != "claude-otel" or not isinstance(doc, dict):
            continue
        if doc.get("kind") != "Deployment" or doc["metadata"]["name"] != "grafana":
            continue
        container = doc["spec"]["template"]["spec"]["containers"][0]
        return {e["name"]: e for e in container.get("env") or []}
    return {}


def pkce_is_paired(client, env):
    """True when the client demands PKCE and Grafana is configured to send it, or neither."""
    wants = bool(client.get("require_pkce"))
    sends = (env.get("GF_AUTH_GENERIC_OAUTH_USE_PKCE") or {}).get("value") == "true"
    return wants == sends


def groups_scope_is_paired(client, env):
    """True when nothing reads a `groups` claim, or the client actually requests one."""
    role_path = (env.get("GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH") or {}).get(
        "value", ""
    )
    if "groups" not in role_path:
        return True
    return "groups" in (client.get("scopes") or [])


def groups_reach_the_id_token(provider, client, env):
    """True when a `groups`-reading role path is fed by a claim the ID token carries.

    Authelia serves `groups` from the userinfo endpoint by default. Grafana evaluates
    `role_attribute_path` against the ID token first and stops at the first value the
    expression yields — and an expression with a `|| 'Viewer'` fallback yields one even when
    the claim is missing, so the userinfo response is never consulted and everybody is a
    Viewer. Granting the scope is therefore NOT enough; a claims policy has to hydrate the
    claim into the ID token.
    """
    role_path = (env.get("GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH") or {}).get(
        "value", ""
    )
    if "groups" not in role_path:
        return True
    policy = (provider.get("claims_policies") or {}).get(client.get("claims_policy"))
    return bool(policy) and "groups" in (policy.get("id_token") or [])


# --- the predicates can go red -------------------------------------------------------------

_CLIENT = {"require_pkce": True, "scopes": ["openid", "groups"]}
_ENV = {
    "GF_AUTH_GENERIC_OAUTH_USE_PKCE": {"name": "x", "value": "true"},
    "GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH": {"name": "y", "value": "groups[*]"},
}


def test_pkce_pairing_is_clean():
    assert pkce_is_paired(_CLIENT, _ENV)


def test_pkce_pairing_is_flagged():
    assert not pkce_is_paired(_CLIENT, {})


def test_groups_scope_pairing_is_clean():
    assert groups_scope_is_paired(_CLIENT, _ENV)


def test_groups_scope_pairing_is_flagged():
    assert not groups_scope_is_paired({"scopes": ["openid"]}, _ENV)


_PROVIDER = {"claims_policies": {"with_groups": {"id_token": ["groups"]}}}


def test_id_token_groups_is_clean():
    assert groups_reach_the_id_token(
        _PROVIDER, {**_CLIENT, "claims_policy": "with_groups"}, _ENV
    )


def test_id_token_groups_is_flagged():
    """The exact live defect: scope granted, no claims policy, everyone lands Viewer."""
    assert not groups_reach_the_id_token(_PROVIDER, _CLIENT, _ENV)


# --- and the real manifests satisfy them ---------------------------------------------------


@pytest.fixture(scope="module")
def live():
    docs = list(rendered_docs())
    clients = oidc_clients(docs)
    assert CLIENT_ID in clients, (
        f"no `{CLIENT_ID}` OIDC client in the rendered Authelia config — found "
        f"{sorted(clients)}. Every assertion below would pass vacuously without it."
    )
    env = grafana_env(docs)
    assert env, "no Grafana Deployment env found in the rendered claude-otel manifests"
    return clients[CLIENT_ID], env, oidc_provider(docs)


def test_grafana_client_and_deployment_agree_on_pkce(live):
    client, env, _provider = live
    assert pkce_is_paired(client, env)


def test_grafana_client_and_deployment_agree_on_the_groups_scope(live):
    client, env, _provider = live
    assert groups_scope_is_paired(client, env)


def test_grafana_groups_claim_reaches_the_id_token(live):
    client, env, provider = live
    assert groups_reach_the_id_token(provider, client, env)


def test_grafana_client_policy_matches_the_route_it_fronts(live):
    """one_factor, the tier the `*.local.<domain>` access_control rule already applies.

    two_factor here would demand a TOTP the headless `-m ui` browser cannot answer, while
    every repo-side check stayed green.
    """
    client, _env, _provider = live
    assert client["authorization_policy"] == "one_factor"


def test_grafana_callback_is_registered_for_the_root_url(live):
    """Grafana builds ONE redirect_uri, from root_url, and Authelia matches it exactly."""
    client, env, _provider = live
    root_url = env["GF_SERVER_ROOT_URL"]["value"]
    assert root_url.rstrip("/") + "/login/generic_oauth" in client["redirect_uris"]


def test_grafana_keeps_its_own_login_form(live):
    """The public route cannot complete an OAuth round trip while root_url names the LAN host.

    Disabling the form would lock out `grafana.<domain>` and remove the break-glass path for
    an Authelia outage, so its absence from the env is deliberate and load-bearing.
    """
    _client, env, _provider = live
    assert "GF_AUTH_DISABLE_LOGIN_FORM" not in env
