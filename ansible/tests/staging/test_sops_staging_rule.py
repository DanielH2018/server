"""The staging secrets file must be encrypted to daniel-server's key and nothing else.

SOPS applies the FIRST matching creation rule. The production rule's path regex
(`.*(vars|secrets)/.*\\.ya?ml$`) matches `ansible/vars/secrets-staging.yml` perfectly well, so
the only thing keeping staging off the four production recipients is that its own, narrower
rule sits above it. Swap the two and `sops` encrypts the staging file to every production key
without printing a warning — the failure is silent, and its blast radius is a copy of every
production credential on a host whose stated purpose is being broken
(docs/staging-cluster.md, Decision 5).

So the ordering is the mechanism, and these tests pin it from both ends: the staging rule comes
first, AND the production rule genuinely would have matched. That second assertion is the one
that keeps this honest — if someone later narrows the production regex so it no longer matches,
ordering has stopped being what protects the file and this test says so instead of passing on.

The last test reads the encrypted file itself, because a correct config still lets a hand-run
`sops` write plaintext into the repo if the encrypt step is skipped.
"""

import re

import pytest
import yaml

from _helpers import ANSIBLE

SOPS_CONFIG = ANSIBLE / ".sops.yaml"
STAGING_SECRETS = ANSIBLE / "vars" / "secrets-staging.yml"

# Path SOPS matches its creation rules against, relative to the config's directory.
STAGING_PATH = "vars/secrets-staging.yml"
PROD_PATH = "vars/secrets.yml"

# daniel-server. The controller for every staging play, because it is the only host that can
# route to the guest — `load_secrets.yml` decrypts with `delegate_to: localhost`, so the
# controller's key is the one that has to work, not the guest's.
DANIEL_SERVER_RECIPIENT = (
    "age1shgt54wq7y9pk4nksxcfr5s539rh2g5uqkh9t2jlx6wj4a8aq34q4cyg7k"
)


def _rules():
    rules = yaml.safe_load(SOPS_CONFIG.read_text())["creation_rules"]
    assert rules, (
        f"{SOPS_CONFIG} has no creation_rules — check the loader, not the config."
    )
    return rules


def _recipients(rule):
    """The rule's age recipients. It is ONE comma-separated scalar, not a YAML list."""
    return [k.strip() for k in rule["age"].split(",") if k.strip()]


def _matches(rule, path):
    return re.search(rule["path_regex"], path) is not None


def test_the_staging_rule_comes_first():
    first = _rules()[0]
    assert _matches(first, STAGING_PATH), (
        f"the first creation rule in {SOPS_CONFIG} does not match {STAGING_PATH}. SOPS takes "
        f"the first matching rule, so a staging rule below the production one never applies "
        f"and the staging secrets get encrypted to every production recipient."
    )
    assert not _matches(first, PROD_PATH), (
        f"the first creation rule in {SOPS_CONFIG} also matches {PROD_PATH}. It is meant to be "
        f"the narrow staging rule; matching the production file would encrypt production "
        f"secrets to daniel-server alone and lock every other host out."
    )


def test_the_production_rule_would_otherwise_have_claimed_the_staging_file():
    """Ordering is only load-bearing while this is true. If it stops being true, say so."""
    later = [r for r in _rules()[1:] if _matches(r, PROD_PATH)]
    assert later, f"no rule after the first in {SOPS_CONFIG} matches {PROD_PATH}."
    assert any(_matches(r, STAGING_PATH) for r in later), (
        f"no production rule in {SOPS_CONFIG} matches {STAGING_PATH} any more. That is not a "
        f"failure by itself — it means the production regex was narrowed and rule ORDER is no "
        f"longer what keeps staging off the production keys. Re-read this file's docstring and "
        f"decide what the guard should be now, rather than deleting this assertion."
    )


def test_the_staging_rule_names_only_the_controllers_key():
    keys = _recipients(_rules()[0])
    assert keys == [DANIEL_SERVER_RECIPIENT], (
        f"the staging rule in {SOPS_CONFIG} lists {keys}, expected exactly "
        f"[{DANIEL_SERVER_RECIPIENT!r}]. Staging decrypts on daniel-server and holds no real "
        f"credential, so every extra recipient widens who can read it and buys nothing."
    )


def test_the_staging_key_is_a_real_recipient_of_the_production_file():
    """Catches a typo'd key, which would otherwise fail only at decrypt time on the host."""
    later = [r for r in _rules()[1:] if _matches(r, PROD_PATH)]
    assert later, (
        f"no rule after the first in {SOPS_CONFIG} matches {PROD_PATH}, so there is nothing to "
        f"check the staging key against. Fix the rule order first."
    )
    assert DANIEL_SERVER_RECIPIENT in _recipients(later[0]), (
        f"{DANIEL_SERVER_RECIPIENT} is not among the production recipients in {SOPS_CONFIG}, so it "
        f"is probably mistyped — daniel-server decrypts both files."
    )


def test_the_staging_secrets_file_is_actually_encrypted():
    doc = yaml.safe_load(STAGING_SECRETS.read_text())
    sops = doc.get("sops")
    assert sops, (
        f"{STAGING_SECRETS} has no `sops` metadata — it is PLAINTEXT in the repo. Encrypt it "
        f"with `sops --config {SOPS_CONFIG} encrypt --filename-override {STAGING_PATH} <plain>`."
    )
    recipients = [entry["recipient"] for entry in sops["age"]]
    assert recipients == [DANIEL_SERVER_RECIPIENT], (
        f"{STAGING_SECRETS} is encrypted to {recipients}, expected only the daniel-server key. "
        f"It was probably created while the rule order in {SOPS_CONFIG} was wrong; re-encrypt it."
    )


@pytest.mark.parametrize("key", ["domain"])
def test_every_value_in_the_staging_file_is_encrypted(key):
    """A partially-encrypted file reads as encrypted at a glance. Check the value, not the file."""
    value = yaml.safe_load(STAGING_SECRETS.read_text())[key]
    assert str(value).startswith("ENC["), (
        f"{key} in {STAGING_SECRETS} is not encrypted — its value is in the clear."
    )
