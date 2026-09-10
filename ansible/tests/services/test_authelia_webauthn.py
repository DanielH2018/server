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

# The keys this role deliberately writes, and the whole set it may write. Deliberately NARROWER
# than the surface Authelia 4.39.21 accepts: a rule that admitted every key the pinned version
# declares would admit `metadata` and `filtering` too, which is exactly the copy-paste from the
# docs site this file exists to reject — the docs describe a wider schema than any one release
# validates, and the failure is at startup rather than at render.
#
# Adding a key here is the deliberate act: check it against the tag `authelia_k8s_image` names,
# in that tag's own `config.template.yml`
# (https://raw.githubusercontent.com/authelia/authelia/v4.39.21/config.template.yml) and its
# `internal/configuration/validator/webauthn.go`, then add it to this set with the rest.
WRITTEN_WEBAUTHN_KEYS = frozenset(
    {
        "disable",
        "enable_passkey_login",
        "display_name",
        "attestation_conveyance_preference",
        "timeout",
        "selection_criteria",
    }
)
WRITTEN_SELECTION_CRITERIA_KEYS = frozenset({"attachment", "user_verification"})


def webauthn_is_an_optional_second_factor(webauthn):
    """True when WebAuthn is available and is not wired as a first factor."""
    return webauthn.get("disable") is False and not webauthn.get("enable_passkey_login")


def only_the_keys_this_role_writes(webauthn):
    """True when the block carries no key beyond the checked set above.

    Every key in that set was read off the pinned version's own sources. A key from anywhere
    else renders, parses and lints, and then Authelia refuses to start on it — under
    `Recreate`, on the SSO gate, with the old pod already gone.
    """
    if not set(webauthn) <= WRITTEN_WEBAUTHN_KEYS:
        return False
    selection = webauthn.get("selection_criteria") or {}
    return set(selection) <= WRITTEN_SELECTION_CRITERIA_KEYS


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


def test_the_written_key_set_is_clean():
    assert only_the_keys_this_role_writes(GOOD_WEBAUTHN)


def test_a_key_copied_from_the_docs_site_is_flagged():
    """`webauthn.metadata` is real in the docs and unchecked here — the copy-paste case."""
    assert not only_the_keys_this_role_writes(
        {**GOOD_WEBAUTHN, "metadata": {"enabled": True}}
    )


def test_an_unchecked_selection_criterion_is_flagged():
    """The nested half of the same rule."""
    assert not only_the_keys_this_role_writes(
        {
            **GOOD_WEBAUTHN,
            "selection_criteria": {
                "user_verification": "preferred",
                "discoverability": "preferred",
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


def test_rendered_webauthn_writes_only_checked_keys(authelia_webauthn):
    assert only_the_keys_this_role_writes(authelia_webauthn), (
        f"webauthn carries a key nobody checked against the pinned Authelia version: "
        f"{sorted(set(authelia_webauthn) - WRITTEN_WEBAUTHN_KEYS)} "
        f"{sorted(set(authelia_webauthn.get('selection_criteria') or {}) - WRITTEN_SELECTION_CRITERIA_KEYS)}. "
        f"It renders, it parses, and the pod refuses to start on it"
    )
