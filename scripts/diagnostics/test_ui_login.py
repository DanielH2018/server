"""`ui_login.py`: minting the Authelia session a headless browser browses with.

Every rule here is an accept/reject pair. Both failure modes this script guards against are
invisible from the passing side alone: Authelia answers HTTP 200 to a *wrong* password, and
answers HTTP 302 to an *unauthenticated* request. A check that only ever sees the good case
would score both as success.
"""

import json
import os
import re

import ui_login

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUTHELIA_DEFAULTS = os.path.join(
    REPO_ROOT, "ansible", "roles", "k8s", "authelia", "defaults", "main.yml"
)


def test_cookie_name_matches_the_authelia_role_default():
    """The one fact this script cannot derive at runtime. A rename in the role would
    otherwise leave the browser holding a cookie Authelia stopped issuing — which presents
    as a silent redirect to the login portal, not as an error."""
    with open(AUTHELIA_DEFAULTS) as f:
        match = re.search(r"^authelia_k8s_cookie_name:\s*(\S+)", f.read(), re.M)
    assert match, f"authelia_k8s_cookie_name not found in {AUTHELIA_DEFAULTS}"
    assert ui_login.COOKIE_NAME == match.group(1)


# --- parse_set_cookie: a successful login vs. a rejected one -------------------------


def test_session_cookie_is_extracted():
    headers = (
        "HTTP/2 200\r\n"
        "content-type: application/json\r\n"
        f"set-cookie: {ui_login.COOKIE_NAME}=abc123; Path=/; HttpOnly; Secure\r\n"
    )
    assert ui_login.parse_set_cookie(headers) == "abc123"


def test_rejected_login_is_flagged():
    """Authelia answers 200 with {"status":"KO"} and sets no cookie on a bad password."""
    headers = "HTTP/2 200\r\ncontent-type: application/json\r\n"
    assert ui_login.parse_set_cookie(headers) is None


def test_an_unrelated_cookie_is_not_mistaken_for_the_session():
    headers = "HTTP/2 200\r\nset-cookie: some_other_cookie=zzz; Path=/\r\n"
    assert ui_login.parse_set_cookie(headers) is None


def test_cookie_attributes_are_stripped_from_the_value():
    headers = f"set-cookie: {ui_login.COOKIE_NAME}=v; Max-Age=60; SameSite=Lax\r\n"
    assert ui_login.parse_set_cookie(headers) == "v"


# --- classify_response: the backend vs. the portal -----------------------------------


def test_backend_reached_is_accepted():
    assert ui_login.classify_response(200, "")[0] is True


def test_portal_redirect_is_flagged():
    """The case that makes a status-code-only check useless: 302 to the portal is exactly
    what an unauthenticated request looks like, and 3xx would otherwise read as success."""
    ok, detail = ui_login.classify_response(
        302, "https://auth.local.example.com/?rd=..."
    )
    assert ok is False
    assert "portal" in detail


def test_a_non_portal_redirect_is_still_a_reachable_backend():
    """Plenty of apps 302 to their own login or dashboard; only the Authelia portal means
    the cookie failed."""
    assert (
        ui_login.classify_response(302, "https://sonarr.local.example.com/login")[0]
        is True
    )


def test_server_error_is_flagged():
    ok, detail = ui_login.classify_response(502, "")
    assert ok is False
    assert "502" in detail


# --- build_storage_state -------------------------------------------------------------


def test_storage_state_is_playwright_shaped():
    state = ui_login.build_storage_state("tok", "example.com", 1900000000)
    assert state["origins"] == []
    (cookie,) = state["cookies"]
    assert cookie["name"] == ui_login.COOKIE_NAME
    assert cookie["value"] == "tok"
    assert cookie["secure"] is True
    assert cookie["httpOnly"] is True
    assert cookie["expires"] == 1900000000
    json.dumps(state)  # Playwright reads this off disk; it must serialise.


def test_cookie_is_scoped_to_the_whole_lan_cookie_domain():
    """The leading dot is what makes one login cover every service route. Without it the
    cookie would only be sent to the portal host itself."""
    state = ui_login.build_storage_state("tok", "example.com", 0)
    assert state["cookies"][0]["domain"] == ".local.example.com"


def test_portal_host_is_the_authelia_url_of_the_lan_cookie():
    assert ui_login.portal_host("example.com") == "auth.local.example.com"
