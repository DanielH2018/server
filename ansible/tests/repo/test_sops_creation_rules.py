"""The cluster's SOPS creation rules resolve as ADR-0022 decides.

WHY THIS IS A TEST. SOPS takes the FIRST creation rule whose `path_regex` matches, and the
generic rule in `ansible/.sops.yaml` also matches every cluster manifest. A cluster rule moved
below it stops applying without an error, and the next manifest is encrypted without the
cluster key. A cluster key added to the generic rule hands the cluster `secrets.yml`. Neither
mistake shows until the cluster fails to decrypt, or never fails at all.

The test resolves paths the way SOPS does (`parseCreationRuleForFile` in sops
`config/config.go`: first `re.search` match wins) and compares recipient sets. It names no
key, so rotating the cluster key or a host key needs no change here.

WHAT IS NOT CHECKED. A real file's recipients against its rule: no cluster file exists yet,
and `cluster_vars.py check` (slice D3.2a) owns that comparison.

Run: uv run pytest ansible/tests/repo/test_sops_creation_rules.py
"""

import re

import pytest
from lib import yaml_fast
from _helpers import REPO

SOPS_YAML = REPO / "ansible" / ".sops.yaml"

SECRETS = "vars/secrets.yml"
CLUSTER_FILE = "vars/cluster-vars.sops.env"
CLUSTER_MANIFEST = "vars/cluster-manifests/pi-peer-backup.sops.yaml"


def _rule_for(rules: list[dict], path: str) -> dict:
    for rule in rules:
        if re.search(rule["path_regex"], path):
            return rule
    pytest.fail(f"no creation rule matches {path}")


def _recipients(rules: list[dict], path: str) -> set[str]:
    return {key.strip() for key in _rule_for(rules, path)["age"].split(",")}


def _problems(rules: list[dict]) -> list[str]:
    secrets = _recipients(rules, SECRETS)
    cluster_file = _recipients(rules, CLUSTER_FILE)
    out = []
    # Exactly one extra key, not merely a superset: a host key removed from the generic rule
    # but left on the cluster rules would still form a superset, and that host would keep
    # decrypting every cluster file.
    if not secrets <= cluster_file or len(cluster_file - secrets) != 1:
        out.append(
            f"{CLUSTER_FILE} must go to every secrets.yml recipient plus exactly one "
            f"cluster key; its rule lists {len(cluster_file)} key(s), secrets.yml's lists "
            f"{len(secrets)}, and {len(cluster_file - secrets)} are on the cluster rule only"
        )
    if _recipients(rules, CLUSTER_MANIFEST) != cluster_file:
        out.append(
            f"{CLUSTER_MANIFEST} resolves to a rule whose recipients differ from "
            f"{CLUSTER_FILE}'s; is a cluster rule below the generic rule, or do the two "
            "cluster rules list different keys?"
        )
    if (
        _rule_for(rules, CLUSTER_MANIFEST).get("encrypted_regex")
        != "^(data|stringData)$"
    ):
        out.append(f"{CLUSTER_MANIFEST}'s rule must encrypt only data and stringData")
    return out


def _tree_rules() -> list[dict]:
    return yaml_fast.safe_load(SOPS_YAML.read_text())["creation_rules"]


@pytest.mark.parametrize("prefix", ["", "ansible/", f"{REPO}/ansible/"])
def test_cluster_rules_resolve_from_any_path_form(prefix):
    """SOPS matches the path relative to ansible/, but `write` passes a repo-root or
    absolute --filename-override; every form must pick the same rule."""
    rules = _tree_rules()
    for path in (SECRETS, CLUSTER_FILE, CLUSTER_MANIFEST):
        assert _rule_for(rules, prefix + path) is _rule_for(rules, path)


def test_tree_creation_rules_is_clean():
    assert _problems(_tree_rules()) == []


def test_cluster_rule_below_the_generic_rule_is_flagged():
    rules = _tree_rules()
    generic = _rule_for(rules, SECRETS)
    reordered = [generic] + [rule for rule in rules if rule is not generic]
    assert any("below the generic rule" in p for p in _problems(reordered))


def test_cluster_key_on_the_generic_rule_is_flagged():
    rules = [dict(rule) for rule in _tree_rules()]
    generic = _rule_for(rules, SECRETS)
    generic["age"] = _rule_for(rules, CLUSTER_FILE)["age"]
    assert any(p.startswith(CLUSTER_FILE) for p in _problems(rules))


def test_host_key_left_on_the_cluster_rules_is_flagged():
    """A host removed from the generic rule but not from the cluster rules."""
    rules = [dict(rule) for rule in _tree_rules()]
    generic = _rule_for(rules, SECRETS)
    generic["age"] = ",".join(sorted(_recipients(rules, SECRETS))[1:])
    assert any(p.startswith(CLUSTER_FILE) for p in _problems(rules))
