"""One Gmail app password feeds three services, and one of them can break silently.

`smtp_notify_app_password` is Uptime Kuma's second alert channel, the credential
monitor-bridge's `email_backstop` re-authenticates on a throttle, and the credential
Authelia sends with. Authelia's failure mode renders clean:

- **Authelia.** `disable_startup_check` belongs to `notifier`, not to `notifier.smtp`.
  Indented one level deeper it is still valid YAML, the manifest validator still parses it,
  and Authelia refuses to boot — which under this role's `Recreate` strategy means SSO is
  down for the fleet with the old pod already gone.

Each rule is a `..._is_clean` / `..._is_flagged` pair over a predicate, applied afterwards to
the real rendered manifests behind a non-vacuity fixture. The render harness stubs SOPS
values, so these rules speak to the SHAPE of the wiring — which key points where, and how it
is nested — never to a credential's content.
"""

import pytest
from _helpers import K8S_ROLES
from _k8s_render import rendered_docs
from lib import yaml_fast

STUB_ADDRESS = "stub@example.com"


def _secret_data(docs, role, name):
    """The named Secret's `stringData`, as a mapping."""
    for doc_role, _tpl, doc in docs:
        if doc_role != role or doc.get("kind") != "Secret":
            continue
        if (doc.get("metadata") or {}).get("name") != name:
            continue
        return doc.get("stringData") or {}
    return {}


# --- authelia ---------------------------------------------------------------------


def startup_check_is_disabled_at_the_notifier_level(notifier):
    """True when `disable_startup_check` is a direct child of `notifier` and is off.

    Nested under `smtp` instead, Authelia ignores it, runs the probe, and refuses to boot
    when it fails. The nesting is the whole rule — a `True` in the wrong place reads exactly
    like a `True` in the right one.
    """
    return notifier.get("disable_startup_check") is True


def smtp_notifier_is_wired(notifier):
    """True when the SMTP notifier carries an implicit-TLS address, a login and a sender."""
    smtp = notifier.get("smtp") or {}
    return (
        str(smtp.get("address", "")).startswith("submissions://")
        and bool(smtp.get("username"))
        and bool(smtp.get("password"))
        and str(smtp.get("username")) in str(smtp.get("sender", ""))
    )


GOOD_SMTP: dict[str, str] = {
    "address": "submissions://smtp.gmail.com:465",
    "username": STUB_ADDRESS,
    "password": "STUB",
    "sender": f"Authelia <{STUB_ADDRESS}>",
}

GOOD_NOTIFIER: dict[str, object] = {
    "disable_startup_check": True,
    "smtp": GOOD_SMTP,
}


def test_notifier_level_disable_is_clean():
    assert startup_check_is_disabled_at_the_notifier_level(GOOD_NOTIFIER)


def test_disable_nested_under_smtp_is_flagged():
    """The exact misindentation: valid YAML, ignored by Authelia, fails to boot."""
    misnested = {"smtp": {**GOOD_SMTP, "disable_startup_check": True}}
    assert not startup_check_is_disabled_at_the_notifier_level(misnested)


def test_wired_notifier_is_clean():
    assert smtp_notifier_is_wired(GOOD_NOTIFIER)


def test_starttls_address_is_flagged():
    """465 is the transport Kuma already proves against Gmail; `smtp://` is a different one."""
    bad = {
        **GOOD_NOTIFIER,
        "smtp": {**GOOD_SMTP, "address": "smtp://smtp.gmail.com:587"},
    }
    assert not smtp_notifier_is_wired(bad)


def test_sender_from_another_account_is_flagged():
    bad = {
        **GOOD_NOTIFIER,
        "smtp": {**GOOD_SMTP, "sender": "Authelia <noreply@example.org>"},
    }
    assert not smtp_notifier_is_wired(bad)


def test_filesystem_notifier_is_flagged():
    """No SMTP block at all: codes written to a file in the pod."""
    assert not smtp_notifier_is_wired(
        {"filesystem": {"filename": "/config/notification.txt"}}
    )


# --- applied to the real rendered manifests ----------------------------------------


@pytest.fixture(scope="module")
def docs():
    return list(rendered_docs())


@pytest.fixture(scope="module")
def authelia_notifier(docs):
    """The `notifier` block of Authelia's rendered configuration.yml.

    It is a YAML document nested inside a Secret's `stringData`, so it loads twice — the
    outer manifest, then the string it carries.
    """
    raw = _secret_data(docs, "authelia", "authelia-config").get("configuration.yml")
    assert raw, "no configuration.yml in the rendered authelia-config Secret"
    notifier = (yaml_fast.safe_load(raw) or {}).get("notifier") or {}
    assert notifier, "the rendered Authelia config carries no notifier block"
    return notifier


def test_authelia_startup_check_is_off_at_the_right_level(authelia_notifier):
    assert startup_check_is_disabled_at_the_notifier_level(authelia_notifier), (
        "disable_startup_check must be a direct child of `notifier`; nested under `smtp` it "
        "is ignored and Authelia refuses to boot when the SMTP probe fails, taking SSO down "
        "for the fleet under this role's Recreate strategy"
    )


def test_authelia_smtp_notifier_is_wired_live(authelia_notifier):
    assert smtp_notifier_is_wired(authelia_notifier), (
        f"the Authelia SMTP notifier is not fully wired: {sorted(authelia_notifier.get('smtp') or {})}"
    )


def test_authelia_role_still_has_the_decided_marker():
    """The startup check is off deliberately; a reviewer who cannot see why will turn it on.

    Anchored on the marker text rather than a line number, per the repo-root convention.
    """
    src = (K8S_ROLES / "authelia" / "templates" / "config-secret.yaml.j2").read_text()
    assert "DECIDED: the SMTP startup check stays OFF" in src
