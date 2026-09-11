"""daniel-stage renders Authelia's SMTP notifier, and boots without ever reaching Gmail.

`authelia_k8s_smtp_notifier` was false on staging until #1464, so the branch prod runs had no
boot rehearsal anywhere. It fails in the shape this repo has paid for twice:
`disable_startup_check` belongs to `notifier`, not `notifier.smtp`, and one level deeper is
valid YAML that renders clean, parses in `validate/k8s_manifests.py`, and then makes Authelia
refuse to start — under `Recreate`, on the SSO gate in front of most public routes, with the
old pod already gone.

Staging can rehearse it because the startup check is off. Authelia opens no SMTP connection at
boot, so a credential that authenticates nothing is enough to prove the half that bites: the
config parses and the pod comes up on it. The two facts that keeps true are what this file
holds — the branch is taken here, and the credential it renders is the plaintext stand-in from
`host_vars/daniel-stage.yml` rather than the production key, which staging cannot decrypt and
must not hold.

`test_smtp_wiring.py` holds the same nesting for the rendered PROD config. This is the staging
half: same rule, different host's variables, and it is the one that says a real deploy would
exercise the branch at all.
"""

import sys

import pytest
from _helpers import REPO

_REPO = REPO
sys.path.insert(0, str(_REPO / "scripts"))

from lib import yaml_fast  # noqa: E402

from validate.k8s_manifests import (  # noqa: E402 — needs the path insert above
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_ROLES,
    SHARED_TPL,
    load_yaml,
    make_env,
    make_lookup,
    register_ansible_filters,
    render_or_error,
    resolve_vars,
    role_defaults,
)

_HOST = "daniel-stage"
_ROLE = "authelia"
_TEMPLATE = "config-secret.yaml.j2"
_HOST_VARS = ANSIBLE / "inventory" / "host_vars" / f"{_HOST}.yml"
_CREDENTIAL = "smtp_notify_app_password"


def boots_without_reaching_smtp(notifier: dict) -> bool:
    """True when the SMTP branch is taken AND the boot-time probe is off at the right level.

    Both halves, because either alone is a different failure: no SMTP branch is the gap #1464
    reported, and an SMTP branch whose startup check runs is a staging pod that never reaches
    Ready on a credential that was never meant to work.
    """
    return notifier.get("disable_startup_check") is True and bool(
        (notifier.get("smtp") or {}).get("address")
    )


def test_a_rehearsable_notifier_is_clean() -> None:
    assert boots_without_reaching_smtp(
        {
            "disable_startup_check": True,
            "smtp": {"address": "submissions://smtp.gmail.com:465"},
        }
    )


def test_the_filesystem_branch_is_flagged() -> None:
    """The pre-#1464 staging state: no SMTP block, so nothing rehearses the branch."""
    assert not boots_without_reaching_smtp(
        {"filesystem": {"filename": "/config/notification.txt"}}
    )


def test_a_running_startup_check_is_flagged() -> None:
    """A fake credential and a live probe means the staging pod never reaches Ready."""
    assert not boots_without_reaching_smtp(
        {"smtp": {"address": "submissions://smtp.gmail.com:465"}}
    )


def test_a_check_disabled_one_level_deeper_is_flagged() -> None:
    assert not boots_without_reaching_smtp(
        {
            "smtp": {
                "address": "submissions://smtp.gmail.com:465",
                "disable_startup_check": True,
            }
        }
    )


# --- applied to the manifest a staging deploy would really render -------------------


@pytest.fixture(scope="module")
def staging_notifier() -> dict:
    base = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **load_yaml(_HOST_VARS),
        "playbook_dir": str(ANSIBLE),
    }
    base = resolve_vars(base, base)
    entry = next(c for c in base["containers_list"] if c["name"] == _ROLE)
    # Role defaults FIRST: Ansible ranks host_vars above them, and the override under test is
    # a host_vars one.
    ctx = {**role_defaults(_ROLE, base), **base, "container_item": entry}
    env = make_env([K8S_ROLES / _ROLE / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, _TEMPLATE, ctx)
    assert rendered is not None, (
        f"{_ROLE}/{_TEMPLATE} failed to render for {_HOST}: {err}"
    )
    doc = yaml_fast.safe_load(rendered)
    config = yaml_fast.safe_load((doc.get("stringData") or {})["configuration.yml"])
    notifier = (config or {}).get("notifier") or {}
    assert notifier, "the staging Authelia config carries no notifier block"
    return notifier


def test_staging_takes_the_smtp_branch_and_boots_on_it(staging_notifier) -> None:
    assert boots_without_reaching_smtp(staging_notifier), (
        f"daniel-stage must render the SMTP notifier with the startup check off at the "
        f"`notifier` level — that pair is what gives the branch a boot rehearsal on a "
        f"credential that authenticates nothing. Got keys {sorted(staging_notifier)}"
    )


def test_staging_renders_its_own_stand_in_credential(staging_notifier) -> None:
    """The value must come from the plaintext inventory, not from a decrypted SOPS key.

    Staging cannot decrypt the production `smtp_notify_app_password`, and a portal that could
    send as the operator is the thing this override exists to prevent.
    """
    declared = load_yaml(_HOST_VARS).get(_CREDENTIAL)
    assert declared, (
        f"{_HOST_VARS.name} declares no {_CREDENTIAL}, so the SMTP branch renders an "
        f"undefined name and Authelia starts with an empty password"
    )
    assert staging_notifier["smtp"]["password"] == declared
