"""karakeep's public no-Authelia bypass must not reach next-auth.

`bridge_bypass_prefixes` on the karakeep entry renders one forward-auth-free IngressRoute per
prefix on the public hostname, for the browser extension and the mobile app — Bearer API-key
callers that cannot pass 2FA. Until #1929 the single prefix was `/api/`, one segment wider than
anything those callers need: it also matched next-auth's `/api/auth/*` (the app's password
login, which the app does not throttle) and the cookie-authenticated children beside it, so on
the internet the app password was the only gate. The fix is an allow-list of the key-gated
prefixes; this guard keeps `/api/auth/` out of it however the list is next edited.

Three invariants, each a predicate with a passing and a rejecting input, then applied to the
rendered routes behind a non-vacuity assertion:

- **No bypass prefix covers `/api/auth/`.** Traefik's `PathPrefix` is a plain string prefix,
  so `/api/` covers it and `/api/v1/` does not.
- **The bypass still covers what the clients call.** Narrowing to `/api/v1/` alone — the fix
  the issue prescribed — would gate the extension's tRPC calls and the mobile upload, which
  fail silently behind Authelia's 302.
- **No monitoring prefix covers `/api/auth/` either.** The `.local` `karakeep-monitoring`
  route (homepage's widget; ClientIP-gated to the bridge IP and the pod CIDR, no Authelia)
  carried `/api/` after #1929 closed the public name, so the password form stayed open to
  every pod (#2018). The first invariant reads the public routes only and never saw it.

Run: uv run pytest ansible/tests/services/test_karakeep_public_bypass.py
"""

import re

import pytest
from _k8s_render import rendered_docs

# What next-auth answers on in the pinned image (apps/web/app/api/auth/[...nextauth]).
NEXT_AUTH_PATH = "/api/auth/csrf"
# One request path per session-less caller, from the 0.32.0 sources: the extension's tRPC
# batch URL, the mobile upload POST (no trailing slash, no subpath), and a mobile asset read.
CLIENT_PATHS = (
    "/api/trpc/bookmarks.getBookmarks?batch=1",
    "/api/assets",
    "/api/assets/0f3c2e1a-asset-id",
    "/api/v1/bookmarks",
)
_PREFIX = re.compile(r"PathPrefix\(`([^`]+)`\)")


# --- the rules, as predicates -------------------------------------------------------


def prefix_covers(prefix: str, path: str) -> bool:
    """True when Traefik's PathPrefix(`prefix`) matches `path` — a plain string prefix."""
    return path.startswith(prefix)


def test_the_old_wide_prefix_is_flagged():
    """The literal pre-#1929 state."""
    assert prefix_covers("/api/", NEXT_AUTH_PATH)


def test_a_key_gated_prefix_is_clean():
    assert not prefix_covers("/api/v1/", NEXT_AUTH_PATH)


def test_the_issue_prescribed_narrowing_loses_the_clients():
    """`/api/v1/` alone is what #1929 asked for; the extension and mobile app never call it."""
    assert not any(prefix_covers("/api/v1/", p) for p in CLIENT_PATHS[:3])


# --- applied to the tree ------------------------------------------------------------


# The ClientIP-gated .local route the third invariant must find. A rename or a template move
# would otherwise leave that fixture empty, and `not covering` over nothing passes.
MONITORING_ROUTE = "karakeep-monitoring"


def _no_forward_auth_prefixes(*, client_ip_gated: bool) -> dict[str, str]:
    """IngressRoute name -> its PathPrefix, for every karakeep route carrying no forward-auth.

    `client_ip_gated` splits the two shapes: the public bypass has neither authelia nor a
    source restriction; the monitoring route has a ClientIP() clause instead of authelia.
    """
    found: dict[str, str] = {}
    for role, _tpl, doc in rendered_docs():
        if role != "karakeep" or doc.get("kind") != "IngressRoute":
            continue
        for route in doc["spec"]["routes"]:
            middlewares = {m["name"] for m in route.get("middlewares", [])}
            if "authelia" in middlewares:
                continue
            if ("ClientIP(" in route["match"]) != client_ip_gated:
                continue
            found[doc["metadata"]["name"]] = _PREFIX.search(route["match"]).group(1)
    return found


@pytest.fixture(scope="module")
def bypass_prefixes() -> dict[str, str]:
    """The public-host routes with neither authelia nor a source restriction."""
    found = _no_forward_auth_prefixes(client_ip_gated=False)
    assert found, "no forward-auth-free karakeep route rendered — the bypass is gone"
    return found


@pytest.fixture(scope="module")
def monitoring_prefixes() -> dict[str, str]:
    """The .local routes gated by ClientIP() alone."""
    found = _no_forward_auth_prefixes(client_ip_gated=True)
    assert MONITORING_ROUTE in found, (
        f"{MONITORING_ROUTE} did not render as a ClientIP-gated route; found {sorted(found)}"
    )
    return found


def test_no_bypass_prefix_reaches_next_auth(bypass_prefixes):
    covering = {
        n: p for n, p in bypass_prefixes.items() if prefix_covers(p, NEXT_AUTH_PATH)
    }
    assert not covering, f"public karakeep route(s) reach next-auth: {covering}"


def test_no_monitoring_prefix_reaches_next_auth(monitoring_prefixes):
    covering = {
        n: p for n, p in monitoring_prefixes.items() if prefix_covers(p, NEXT_AUTH_PATH)
    }
    assert not covering, f".local karakeep route(s) reach next-auth: {covering}"


def test_the_bypass_still_covers_every_client_path(bypass_prefixes):
    uncovered = [
        path
        for path in CLIENT_PATHS
        if not any(prefix_covers(p, path) for p in bypass_prefixes.values())
    ]
    assert not uncovered, f"session-less client paths now behind Authelia: {uncovered}"
