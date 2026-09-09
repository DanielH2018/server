"""One Gmail app password now feeds three services, and two of them can break silently.

`smtp_notify_app_password` was already Uptime Kuma's second alert channel and the credential
monitor-bridge's `email_backstop` re-authenticates on a throttle. Healthchecks and Authelia
joined it, and each brought a failure mode that renders clean:

- **Healthchecks.** Gmail authenticates the account its app password was minted for, so
  `EMAIL_HOST_USER` in the Secret must be the same address the Deployment's
  `DEFAULT_FROM_EMAIL` sends as. They are two keys in two files, and Django reports a
  mismatch as a generic auth failure at send time — long after the deploy is green. This is
  the state the role sat in from 2026-08-30: host, port and user all configured, no password,
  every send failing auth.
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


def _deployment_env(docs, role, container=None):
    """The first matching Deployment's env, as a name -> value mapping.

    Only literal `value` entries land here; a `valueFrom` carries no value to compare and is
    skipped rather than stored as None.
    """
    for doc_role, _name, doc in docs:
        if doc_role != role or doc.get("kind") != "Deployment":
            continue
        containers = (
            ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
        ).get("containers") or []
        for c in containers:
            if container and c.get("name") != container:
                continue
            return {e["name"]: e["value"] for e in (c.get("env") or []) if "value" in e}
    return {}


def _secret_data(docs, role, name):
    """The named Secret's `stringData`, as a mapping."""
    for doc_role, _tpl, doc in docs:
        if doc_role != role or doc.get("kind") != "Secret":
            continue
        if (doc.get("metadata") or {}).get("name") != name:
            continue
        return doc.get("stringData") or {}
    return {}


# --- healthchecks -----------------------------------------------------------------


def smtp_identity_is_consistent(secret, env):
    """True when the SMTP login address is the address mail is sent as.

    Gmail rejects a `From` that does not belong to the authenticated account, so these two
    disagreeing is a send-time auth failure that no deploy gate can see.
    """
    user = secret.get("EMAIL_HOST_USER")
    sender = env.get("DEFAULT_FROM_EMAIL")
    return bool(user) and user == sender


def smtp_password_is_present(secret):
    """True when a password renders at all.

    The 2026-08-30 state is the thing this rejects: EMAIL_HOST/PORT/USE_TLS/USER all set and
    the password key simply absent, so Django attempts SMTP and fails auth on every send
    instead of skipping it.
    """
    return bool(secret.get("EMAIL_HOST_PASSWORD"))


def test_matching_identity_is_clean():
    assert smtp_identity_is_consistent(
        {"EMAIL_HOST_USER": STUB_ADDRESS}, {"DEFAULT_FROM_EMAIL": STUB_ADDRESS}
    )


def test_mismatched_identity_is_flagged():
    assert not smtp_identity_is_consistent(
        {"EMAIL_HOST_USER": "someone-else@gmail.com"},
        {"DEFAULT_FROM_EMAIL": STUB_ADDRESS},
    )


def test_absent_identity_is_flagged():
    """An unset user is not "matches whatever the sender is"."""
    assert not smtp_identity_is_consistent({}, {})


def test_present_password_is_clean():
    assert smtp_password_is_present({"EMAIL_HOST_PASSWORD": "STUB"})


def test_absent_password_is_flagged():
    assert not smtp_password_is_present({"EMAIL_HOST_USER": STUB_ADDRESS})


def test_empty_password_is_flagged():
    assert not smtp_password_is_present({"EMAIL_HOST_PASSWORD": ""})


# Django's two TLS switches are mutually exclusive, and healthchecks reads each through
# `envbool`, which accepts only "", "True" and "False" and raises on anything else. So a
# transport is described by the triple, and three of its combinations are broken in different
# ways: both true raises at startup, both false sends the AUTH in the clear, and the right
# switch against the wrong port never connects.
TRANSPORTS = {
    "implicit-tls-465": {
        "EMAIL_PORT": "465",
        "EMAIL_USE_TLS": "False",
        "EMAIL_USE_SSL": "True",
    },
    "starttls-587": {
        "EMAIL_PORT": "587",
        "EMAIL_USE_TLS": "True",
        "EMAIL_USE_SSL": "False",
    },
}


def uses_the_proven_gmail_transport(env):
    """True when the transport is implicit TLS on 465, and coherently so.

    Gmail answered STARTTLS on 587 with `535 Username and Password not accepted` on
    2026-09-09, using the account and app password it accepts on 465. Every other consumer
    here — Authelia, Uptime Kuma, monitor-bridge's `email_backstop` — was already on 465, so
    this pins healthchecks to the one transport with live evidence behind it.
    """
    return (
        env.get("EMAIL_PORT") == "465"
        and env.get("EMAIL_USE_SSL") == "True"
        and env.get("EMAIL_USE_TLS") == "False"
    )


def tls_switches_are_coherent(env):
    """True when exactly one of Django's two TLS switches is on.

    Both on is the one that bites hardest: Django raises `EMAIL_USE_TLS/EMAIL_USE_SSL are
    mutually exclusive` at startup, so the pod crashloops rather than sending a bad message.
    """
    return (env.get("EMAIL_USE_TLS") == "True") != (env.get("EMAIL_USE_SSL") == "True")


def test_implicit_tls_is_clean():
    assert uses_the_proven_gmail_transport(TRANSPORTS["implicit-tls-465"])


def test_starttls_is_flagged():
    """The configuration Gmail rejected. It is coherent, which is why only the port rule catches it."""
    assert not uses_the_proven_gmail_transport(TRANSPORTS["starttls-587"])
    assert tls_switches_are_coherent(TRANSPORTS["starttls-587"])


def test_right_switch_wrong_port_is_flagged():
    assert not uses_the_proven_gmail_transport(
        {"EMAIL_PORT": "587", "EMAIL_USE_TLS": "False", "EMAIL_USE_SSL": "True"}
    )


def test_coherent_switches_are_clean():
    assert tls_switches_are_coherent(TRANSPORTS["implicit-tls-465"])


def test_both_tls_switches_on_is_flagged():
    """Django raises at startup on this pair, so the pod never reaches Ready."""
    assert not tls_switches_are_coherent(
        {"EMAIL_PORT": "465", "EMAIL_USE_TLS": "True", "EMAIL_USE_SSL": "True"}
    )


def test_both_tls_switches_off_is_flagged():
    """Neither switch on sends the AUTH over an unencrypted connection."""
    assert not tls_switches_are_coherent(
        {"EMAIL_PORT": "465", "EMAIL_USE_TLS": "False", "EMAIL_USE_SSL": "False"}
    )


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
    """The pre-change state — codes written to a file in the pod, no SMTP block at all."""
    assert not smtp_notifier_is_wired(
        {"filesystem": {"filename": "/config/notification.txt"}}
    )


# --- applied to the real rendered manifests ----------------------------------------


@pytest.fixture(scope="module")
def docs():
    return list(rendered_docs())


@pytest.fixture(scope="module")
def healthchecks_smtp(docs):
    """The healthchecks Secret and Deployment env, with the non-vacuity check.

    Without it, a renamed Secret or a template that stopped rendering turns every rule below
    into an assertion over two empty mappings — which passes.
    """
    secret = _secret_data(docs, "healthchecks", "healthchecks")
    env = _deployment_env(docs, "healthchecks")
    assert "EMAIL_HOST_USER" in secret, (
        f"no EMAIL_HOST_USER in the rendered healthchecks Secret; found {sorted(secret)}"
    )
    assert "DEFAULT_FROM_EMAIL" in env, (
        f"no DEFAULT_FROM_EMAIL in the rendered healthchecks Deployment; found {sorted(env)}"
    )
    assert "EMAIL_PORT" in env, (
        f"no EMAIL_PORT in the rendered healthchecks Deployment, so the transport rules below "
        f"would assert over an absent key and pass; found {sorted(env)}"
    )
    return secret, env


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


def test_healthchecks_smtp_identity_matches_live(healthchecks_smtp):
    secret, env = healthchecks_smtp
    assert smtp_identity_is_consistent(secret, env), (
        f"EMAIL_HOST_USER {secret.get('EMAIL_HOST_USER')!r} is not the address "
        f"DEFAULT_FROM_EMAIL {env.get('DEFAULT_FROM_EMAIL')!r} sends as; Gmail rejects that "
        f"pairing at send time, not at deploy time"
    )


def test_healthchecks_smtp_password_renders_live(healthchecks_smtp):
    secret, _env = healthchecks_smtp
    assert smtp_password_is_present(secret), (
        "healthchecks has EMAIL_HOST/PORT/USE_TLS but no EMAIL_HOST_PASSWORD — Django "
        "attempts SMTP and fails auth rather than skipping the send"
    )


def test_healthchecks_uses_the_proven_transport_live(healthchecks_smtp):
    _secret, env = healthchecks_smtp
    assert uses_the_proven_gmail_transport(env), (
        f"healthchecks is not on implicit TLS/465: EMAIL_PORT={env.get('EMAIL_PORT')!r} "
        f"EMAIL_USE_SSL={env.get('EMAIL_USE_SSL')!r} EMAIL_USE_TLS={env.get('EMAIL_USE_TLS')!r}. "
        f"Gmail answered STARTTLS/587 with 535 on this account's app password"
    )


def test_healthchecks_tls_switches_are_coherent_live(healthchecks_smtp):
    _secret, env = healthchecks_smtp
    assert tls_switches_are_coherent(env), (
        "EMAIL_USE_TLS and EMAIL_USE_SSL must not both be set the same way — Django raises "
        "at startup when both are true, and sends AUTH in the clear when neither is"
    )


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
