"""`ui_login.py`: minting the Authelia session a headless browser browses with.

Every rule here is an accept/reject pair. Both failure modes this script guards against are
invisible from the passing side alone: Authelia answers HTTP 200 to a *wrong* password, and
answers HTTP 302 to an *unauthenticated* request. A check that only ever sees the good case
would score both as success.
"""

import base64
import json
import os
import re
import sys

# `scripts/diagnostics` is deliberately absent from `pythonpath` in pyproject.toml, so each test
# here puts its own parent directory on `sys.path` — the same insert its siblings carry. Without
# it this module imported only when a sibling that HAS the insert happened to be collected first,
# which held for the whole suite and broke the moment CI sharded it (#1270).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ui_login

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
AUTHELIA_DEFAULTS = os.path.join(
    REPO_ROOT, "ansible", "roles", "k8s", "authelia", "defaults", "main.yml"
)


def test_cookie_name_matches_the_authelia_role_default():
    """The one fact this script cannot derive at runtime.

    A rename in the role would otherwise leave the browser holding a cookie Authelia stopped issuing
    — which presents as a silent redirect to the login portal, not as an error.
    """
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
    """The case that makes a status-code-only check useless.

    302 to the portal is exactly what an unauthenticated request looks like, and 3xx would otherwise
    read as success.
    """
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


# --- classify_state: does Authelia still honour the cookie ---------------------------


def _state(level, status="OK"):
    return {"status": status, "data": {"authentication_level": level}}


def test_one_factor_session_is_accepted():
    valid, detail = ui_login.classify_state(_state(1))
    assert valid is True
    assert "1" in detail


def test_two_factor_session_is_accepted():
    assert ui_login.classify_state(_state(2))[0] is True


def test_two_factor_requirement_accepts_a_level_two_session():
    assert ui_login.classify_state(_state(2), required_level=2)[0] is True


def test_two_factor_requirement_rejects_a_one_factor_session():
    """The guard that keeps a valid-but-insufficient cookie from reading green.

    A level-1 session is genuinely authenticated and still bounces off code-server, n8n and
    longhorn, so `authenticated` alone is the wrong question for the two_factor jar.
    """
    valid, detail = ui_login.classify_state(_state(1), required_level=2)
    assert valid is False
    assert "one_factor" in detail


def test_the_two_tiers_never_share_a_state_file():
    """`ui_mcp.sh` loads a jar unconditionally, so an admin-capable session sharing the
    default path would put two_factor auth behind every ordinary page load."""
    assert ui_login.state_path(two_factor=True) != ui_login.state_path(two_factor=False)
    assert ui_login.state_path() == ui_login.STATE_PATH


def test_an_expired_two_factor_session_reports_minutes_not_days():
    """A two_factor session lives an hour, so a days-rounded message would read
    'expired 0d ago' for every real expiry and tell nobody anything."""
    state = ui_login.build_storage_state("tok", "example.com", 0)
    assert ui_login.local_state_problem(state, now=600) == "expired 10m ago"


def test_deauthenticated_cookie_is_flagged():
    """The whole point of asking Authelia.

    It answers HTTP 200 with level 0 to a cookie it no longer honours — after a restart or an
    authelia_secret rotation — while the expiry stamped in the local file goes on reading valid for
    weeks.
    """
    valid, detail = ui_login.classify_state(_state(0))
    assert valid is False
    assert "authentication_level 0" in detail


def test_non_ok_status_is_flagged():
    valid, detail = ui_login.classify_state(_state(1, status="KO"))
    assert valid is False
    assert "KO" in detail


def test_missing_authentication_level_is_flagged():
    valid, detail = ui_login.classify_state({"status": "OK", "data": {}})
    assert valid is False
    assert "authentication_level" in detail


def test_unparseable_body_is_flagged():
    """check() passes None when the response is not JSON — a captive portal or an error
    page must not read as a live session."""
    assert ui_login.classify_state(None)[0] is False


# --- local_state_problem: the cheap pre-filter, not the verdict ----------------------


def test_a_dated_session_file_has_no_local_problem():
    state = ui_login.build_storage_state("tok", "example.com", 2000)
    assert ui_login.local_state_problem(state, now=1000) is None


def test_an_expired_session_file_is_flagged_locally():
    state = ui_login.build_storage_state("tok", "example.com", 1000)
    assert "expired" in ui_login.local_state_problem(state, now=1000 + 86400)


def test_a_cookieless_state_file_is_flagged_locally():
    assert "no cookies" in ui_login.local_state_problem({"cookies": []}, now=0)


def test_local_check_cannot_see_a_revoked_cookie():
    """Pins the reason check() calls Authelia at all.

    A cookie revoked server-side leaves a file this function is happy with, so passing here must
    never stand as the verdict.
    """
    state = ui_login.build_storage_state("revoked-server-side", "example.com", 2**31)
    assert ui_login.local_state_problem(state, now=0) is None
    assert ui_login.classify_state(_state(0))[0] is False


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
    """The leading dot is what makes one login cover every service route.

    Without it the cookie would only be sent to the portal host itself.
    """
    state = ui_login.build_storage_state("tok", "example.com", 0)
    assert state["cookies"][0]["domain"] == ".local.example.com"


def test_portal_host_is_the_authelia_url_of_the_lan_cookie():
    assert ui_login.portal_host("example.com") == "auth.local.example.com"


# --- derive_totp: the RFC's own vectors, and the parameters both sides must agree on ---

# RFC 6238 Appendix B. Its SHA1 vectors use the ASCII seed "12345678901234567890"; encoding
# it here rather than pasting the base32 keeps the provenance visible, and keeps gitleaks from
# reading a high-entropy literal as a real credential. The RFC prints 8-digit codes, so these
# assert against `digits=8` — the vector is the point, not the width `claude-ui` uses.
RFC6238_SECRET = base64.b32encode(b"12345678901234567890").decode()
RFC6238_SHA1_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


def derive_totp_at(now):
    return ui_login.derive_totp(RFC6238_SECRET, now, digits=8)


def test_derive_totp_matches_the_rfc6238_vectors():
    for now, expected in RFC6238_SHA1_VECTORS:
        assert derive_totp_at(now) == expected, f"wrong code at t={now}"


def test_derive_totp_rejects_a_shifted_counter():
    """The reject half: a code from the previous window must not equal this one.

    Without it, a `derive_totp` that ignored `now` entirely — returning a constant — would
    still satisfy a single-vector test.
    """
    for now, expected in RFC6238_SHA1_VECTORS:
        assert derive_totp_at(now - ui_login.TOTP_PERIOD) != expected


def test_derive_totp_accepts_an_unpadded_secret():
    """Authelia stores the shared secret unpadded, so padding must not be required."""
    padded = RFC6238_SECRET + "=" * (-len(RFC6238_SECRET) % 8)
    assert ui_login.derive_totp(RFC6238_SECRET, 59) == ui_login.derive_totp(padded, 59)


def test_claude_user_matches_the_authelia_role_default():
    """The second fact this script cannot derive at runtime, alongside the cookie name.

    A rename in the role leaves this script posting a username Authelia has no user for,
    which Authelia answers with HTTP 200 and no cookie — the same shape as a wrong password.
    """
    with open(AUTHELIA_DEFAULTS) as f:
        match = re.search(r"^authelia_k8s_claude_user:\s*(\S+)", f.read(), re.M)
    assert match, f"authelia_k8s_claude_user not found in {AUTHELIA_DEFAULTS}"
    assert ui_login.CLAUDE_USER == match.group(1)


def test_totp_parameters_match_the_seeding_task():
    """The derivation and the registration must use one set of TOTP parameters.

    They are stated on both sides rather than defaulted, so this is what holds them
    together: a code derived at a different period or width is simply rejected, with
    nothing in the failure naming the mismatch.
    """
    tasks = os.path.join(
        REPO_ROOT, "ansible", "roles", "k8s", "authelia", "tasks", "main.yml"
    )
    with open(tasks) as f:
        seed = f.read()
    assert "authelia storage user totp generate" in seed, (
        "the TOTP seeding task is gone; derive_totp now has nothing to agree with"
    )
    assert f"--digits {ui_login.TOTP_DIGITS}" in seed
    assert f"--period {ui_login.TOTP_PERIOD}" in seed
    assert "--algorithm SHA1" in seed
    assert ui_login.TOTP_ALGORITHM().name == "sha1"


# --- the remint hints must name the unattended path ----------------------------------

# Both tiers mint without a typed code since `claude-ui` arrived. A hint still naming
# `--totp <code>` is not merely stale — it sends the reader to a phone to solve a problem a
# flag solves, which is the exact failure the dedicated identity was added to remove. These
# strings are the only place a person meets that instruction, so they get a guard.


def test_the_two_factor_hint_names_the_unattended_flag():
    assert ui_login.mint_hint(two_factor=True) == "--two-factor"


def test_no_hint_sends_the_reader_to_a_typed_code():
    """The reject half. `--totp` survives as break-glass and keeps its argparse help; what
    must not survive is a hint offering it as the way to mint."""
    for two_factor in (True, False):
        assert "--totp" not in ui_login.mint_hint(two_factor)


def test_an_insufficient_session_is_not_told_to_type_a_code():
    _, detail = ui_login.classify_state(_state(1), required_level=2)
    assert "--totp" not in detail
    assert "--two-factor" in detail
