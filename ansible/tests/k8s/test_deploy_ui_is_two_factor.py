"""deploy.local starts deploys and clears holds, so it must sit in Authelia's two_factor list,
ahead of the *.local one_factor wildcard. A one_factor fall-through is silent: the page still
loads, behind a weaker gate."""

from lib import yaml_fast
from _k8s_render import rendered_docs


def access_rules(docs):
    for role, _name, doc in docs:
        if role != "authelia" or not isinstance(doc, dict):
            continue
        raw = (doc.get("stringData") or {}).get("configuration.yml")
        if raw:
            return (yaml_fast.safe_load(raw) or {})["access_control"]["rules"]
    return []


def policy_for(rules, host):
    for r in rules:
        domains = r["domain"] if isinstance(r["domain"], list) else [r["domain"]]
        if host in domains:
            return r["policy"]
        if any(d.startswith("*.") and host.endswith(d[1:]) for d in domains):
            return r["policy"]
    return None


def test_deploy_local_is_two_factor_is_clean():
    rules = access_rules(rendered_docs())
    assert rules, "authelia configuration.yml not found in the render"
    domain = next(
        d
        for r in rules
        for d in ([r["domain"]] if isinstance(r["domain"], str) else r["domain"])
        if d.startswith("code-server.")
    )
    assert policy_for(rules, domain.replace("code-server.", "deploy.")) == "two_factor"


def test_a_wildcard_only_host_reads_one_factor_is_flagged():
    rules = access_rules(rendered_docs())
    domain = next(
        d
        for r in rules
        for d in ([r["domain"]] if isinstance(r["domain"], str) else r["domain"])
        if d.startswith("code-server.")
    )
    assert (
        policy_for(rules, domain.replace("code-server.", "nonexistent."))
        == "one_factor"
    )
