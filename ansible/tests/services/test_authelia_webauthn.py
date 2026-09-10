"""WebAuthn on this portal is an OPTIONAL second factor, and Authelia is strict about keys.

Two invariants, and neither is visible from a green deploy:

- **`enable_passkey_login` stays false.** It is the first-factor passwordless flow, not a
  second factor. Turning it on changes what `one_factor` means for every rule in
  `access_control` — a route this portal guards at one factor becomes reachable with a
  credential the operator enrolled as a *second* one — rather than adding a choice at the
  second-factor step, which is all #1502 asked for.
- **Every key under `webauthn` is one Authelia 4.39.21 declares.** Authelia refuses to start
  on a key it does not recognise, and the pinned version's schema is narrower than the latest
  docs: `metadata`, `filtering.prohibit_backup_eligibility` and the experimental passkey
  toggles are the ones a copy-paste from the docs site brings in. This pod rolls under
  `Recreate` in front of most public routes, so that failure lands with the old pod gone —
  the same shape `test_smtp_wiring.py` guards for `disable_startup_check`, and equally
  invisible to `validate/k8s_manifests.py`, which only asks whether the YAML parses.

Each rule is a predicate with a passing and a rejecting input, then applied to the real
rendered manifest behind a non-vacuity assertion.
"""

import pytest
from _k8s_render import rendered_docs
from lib import yaml_fast

# The keys the pinned image's own `config.template.yml` declares under `webauthn`. Read off
# https://raw.githubusercontent.com/authelia/authelia/v4.39.21/config.template.yml — bump this
# set from that file when `authelia_k8s_image` moves, never from the docs site, which
# documents the unreleased schema.
PINNED_WEBAUTHN_KEYS = frozenset(
    {
        "disable",
        "enable_passkey_login",
        "experimental_enable_passkey_uv_two_factors",
        "experimental_enable_passkey_upgrade",
        "display_name",
        "attestation_conveyance_preference",
        "timeout",
        "filtering",
        "selection_criteria",
        "metadata",
    }
)
PINNED_SELECTION_CRITERIA_KEYS = frozenset(
    {"attachment", "discoverability", "user_verification"}
)


def webauthn_is_an_optional_second_factor(webauthn):
    """True when WebAuthn is available and is not wired as a first factor."""
    return webauthn.get("disable") is False and not webauthn.get("enable_passkey_login")


def keys_are_known_to_the_pinned_version(webauthn):
    """True when no key here is one Authelia 4.39.21 would reject at startup."""
    if not set(webauthn) <= PINNED_WEBAUTHN_KEYS:
        return False
    selection = webauthn.get("selection_criteria") or {}
    return set(selection) <= PINNED_SELECTION_CRITERIA_KEYS


GOOD_WEBAUTHN: dict[str, object] = {
    "disable": False,
    "enable_passkey_login": False,
    "display_name": "Authelia example.com",
    "attestation_conveyance_preference": "indirect",
    "timeout": "60 seconds",
    "selection_criteria": {"attachment": "", "user_verification": "preferred"},
}


def test_optional_second_factor_is_clean():
    assert webauthn_is_an_optional_second_factor(GOOD_WEBAUTHN)


def test_passkey_login_is_flagged():
    """Passwordless login weakens `one_factor` everywhere rather than adding a second factor."""
    assert not webauthn_is_an_optional_second_factor(
        {**GOOD_WEBAUTHN, "enable_passkey_login": True}
    )


def test_disabled_webauthn_is_flagged():
    assert not webauthn_is_an_optional_second_factor({**GOOD_WEBAUTHN, "disable": True})


def test_pinned_keys_are_clean():
    assert keys_are_known_to_the_pinned_version(GOOD_WEBAUTHN)


def test_key_from_a_newer_schema_is_flagged():
    """A key from a newer schema renders and parses; 4.39.21 refuses to boot on it."""
    assert not keys_are_known_to_the_pinned_version(
        {
            **GOOD_WEBAUTHN,
            "selection_criteria": {
                "user_verification": "preferred",
                "residency": "preferred",
            },
        }
    )


# --- applied to the real rendered manifest ------------------------------------------


@pytest.fixture(scope="module")
def authelia_webauthn():
    docs = list(rendered_docs())
    for role, _tpl, doc in docs:
        if role != "authelia" or doc.get("kind") != "Secret":
            continue
        if (doc.get("metadata") or {}).get("name") != "authelia-config":
            continue
        raw = (doc.get("stringData") or {}).get("configuration.yml")
        webauthn = (yaml_fast.safe_load(raw or "") or {}).get("webauthn")
        assert webauthn, (
            "the rendered Authelia config carries no webauthn block, so every rule below "
            "would assert over an empty mapping"
        )
        return webauthn
    pytest.fail("no rendered authelia-config Secret")


def test_rendered_webauthn_is_an_optional_second_factor(authelia_webauthn):
    assert webauthn_is_an_optional_second_factor(authelia_webauthn), (
        f"webauthn must be available and second-factor only; got "
        f"disable={authelia_webauthn.get('disable')!r} "
        f"enable_passkey_login={authelia_webauthn.get('enable_passkey_login')!r}"
    )


def test_rendered_webauthn_keys_are_known_to_the_pinned_version(authelia_webauthn):
    assert keys_are_known_to_the_pinned_version(authelia_webauthn), (
        f"webauthn carries a key Authelia 4.39.21 does not declare: "
        f"{sorted(set(authelia_webauthn) - PINNED_WEBAUTHN_KEYS)} "
        f"{sorted(set(authelia_webauthn.get('selection_criteria') or {}) - PINNED_SELECTION_CRITERIA_KEYS)}. "
        f"It renders, it parses, and the pod refuses to start on it"
    )
