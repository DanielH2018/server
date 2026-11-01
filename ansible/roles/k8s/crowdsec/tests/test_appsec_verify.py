"""The AppSec verify's verdict: up only when cscli shows an enabled config and an enabled rule.

`files/appsec_verify.py` replaced `crowdsec-appsec-verify.sh.j2` at H2.4b. These tests drive its
`main` with a stubbed cscli, so each branch of the bash script it ports has a case.

Run: uv run pytest ansible/roles/k8s/crowdsec/tests/test_appsec_verify.py
"""

import json

import yaml

import appsec_verify as av
from lib.repo_paths import ANSIBLE


def _cscli(configs=("enabled",), rules=("enabled",), lapi=0, fail=()):
    """A runner answering the three cscli calls, and the list of argvs it records."""
    calls = []

    def runner(argv):
        calls.append(argv)
        assert argv[0] == av.CSCLI
        kind = argv[1]
        if kind == "lapi":
            return lapi, ""
        if kind in fail:
            return 1, ""
        statuses = configs if kind == "appsec-configs" else rules
        return 0, json.dumps(
            {kind: [{"name": f"x{i}", "status": s} for i, s in enumerate(statuses)]}
        )

    return runner, calls


def test_an_enabled_config_and_rule_is_up_with_the_counts(capsys):
    runner, calls = _cscli(configs=("enabled", "disabled"), rules=("enabled",) * 3)
    assert av.main(runner) == 0
    assert (
        capsys.readouterr().out == "1 appsec-configs enabled, 3 enabled rules loaded\n"
    )
    assert [argv[1:] for argv in calls] == [
        ["lapi", "status"],
        ["appsec-configs", "list", "-o", "json"],
        ["appsec-rules", "list", "-o", "json"],
    ]


def test_an_unreachable_lapi_defers_with_exit_0_and_queries_nothing_else():
    runner, calls = _cscli(lapi=1)
    assert av.verdict(runner) == (
        0,
        "crowdsec unreachable — deferring to the scrape job + workload health",
    )
    assert len(calls) == 1


def test_no_enabled_config_is_down():
    assert av.verdict(_cscli(configs=("disabled",))[0]) == (
        1,
        "no enabled appsec-configs — inline WAF not loaded, edge is ban-list-only",
    )


def test_no_enabled_rule_is_down():
    assert av.verdict(_cscli(rules=())[0]) == (
        1,
        "0 enabled appsec-rules loaded — WAF config present but not enforcing",
    )


def test_a_failed_or_unparseable_query_is_a_query_error_not_zero_items():
    assert av.verdict(_cscli(fail=("appsec-rules",))[0]) == (
        1,
        "cscli appsec-rules list failed though LAPI is up (appsec query error)",
    )
    assert av.enabled_count("not json", "appsec-rules") is None
    assert av.enabled_count('{"other": []}', "appsec-rules") is None
    assert av.enabled_count('{"appsec-rules": []}', "appsec-rules") == 0


K8S_WORKLOAD_RULES = ANSIBLE / "roles/k8s/observability/files/rules/k8s-workloads.yaml"
OUTAGE_OWNERS = frozenset({"K8sDeploymentZeroAvailable", "K8sContainerCrashLooping"})


def _doc_text():
    """The module docstring with its line wraps folded, so a phrase reads whole wherever it wraps."""
    return " ".join((av.__doc__ or "").split())


def test_the_docstring_no_longer_names_the_retired_workload_health_tile():
    assert "Workload Health" not in _doc_text()


def test_the_docstring_names_the_k8s_workloads_rules_that_own_an_outage():
    doc = yaml.safe_load(K8S_WORKLOAD_RULES.read_text())
    alerts = {r.get("alert") for g in doc["groups"] for r in g["rules"]}
    assert OUTAGE_OWNERS <= alerts
    assert {name for name in OUTAGE_OWNERS if name not in _doc_text()} == set()
    assert "cluster scrape job" in _doc_text()
