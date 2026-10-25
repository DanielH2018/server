"""The crowdsec-jobs sidecar exports each Kuma-pushed job's verdict, and the rules read it.

`files/jobs_metrics.py` serves `homelab_check_*` gauges for the three jobs that push to Kuma
(M6 batch B6b). The first tests drive the exposition and `run_job`'s export. The rest hold the
three places that must agree on the port and on the job tags: the scrape job, the pod and the
NetworkPolicy, and the shadow rules' `check` literals.

Run: uv run pytest ansible/roles/k8s/crowdsec/tests/test_jobs_metrics.py
"""

import re
import subprocess
import urllib.error
import urllib.request

import crowdsec_jobs as cj
import jobs_metrics
import pytest
from _helpers import K8S_ROLES
from _k8s_render import render_role_template
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "pod-producers.yaml"
PUSHING = {job.tag: job for job in cj.JOBS if job.push_env}


def _samples(body: bytes) -> dict[str, float]:
    """`name{labels}` to value for each sample line of an exposition body."""
    return {
        line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
        for line in body.decode().splitlines()
        if line and not line.startswith("#")
    }


def test_the_three_pushing_jobs_are_exported_and_the_bouncer_prune_is_not():
    assert set(PUSHING) == {
        "crowdsec-remote-allowlist",
        "crowdsec-appsec-verify",
        "crowdsec-home-allowlist",
    }
    assert set(cj.EXPOSITION._deadlines) == set(PUSHING)


def test_a_deadline_is_three_periods_of_its_job():
    samples = _samples(cj.EXPOSITION.body())
    assert (
        samples['homelab_check_deadline_seconds{check="crowdsec-appsec-verify"}']
        == 2700
    )
    assert (
        samples['homelab_check_deadline_seconds{check="crowdsec-remote-allowlist"}']
        == 900
    )


def test_before_a_first_run_the_timestamp_is_the_start_and_there_is_no_verdict():
    exposition = jobs_metrics.Exposition({"job-a": 900}, started=1000.0)
    assert _samples(exposition.body()) == {
        'homelab_check_last_run_timestamp_seconds{check="job-a"}': 1000.0,
        'homelab_check_deadline_seconds{check="job-a"}': 900.0,
    }


def test_a_run_replaces_its_jobs_row_in_place():
    exposition = jobs_metrics.Exposition({"job-a": 900, "job-b": 900}, started=1000.0)
    exposition.record("job-a", False, 1500.0)
    exposition.record("job-a", True, 1800.0)
    exposition.record("job-b", False, 1700.0)
    exposition.record("not-covered", True, 1900.0)
    samples = _samples(exposition.body())
    assert samples['homelab_check_success{check="job-a"}'] == 1.0
    assert samples['homelab_check_last_run_timestamp_seconds{check="job-a"}'] == 1800.0
    assert samples['homelab_check_success{check="job-b"}'] == 0.0
    assert not [k for k in samples if "not-covered" in k]


def _run(job_tag: str, returncode: int) -> list[tuple]:

    recorded = []
    cj.run_job(
        cj.JOBS[[j.tag for j in cj.JOBS].index(job_tag)],
        runner=lambda argv, **kw: subprocess.CompletedProcess(argv, returncode, "x\n"),
        push=lambda *a, **kw: True,
        env={},
        out=lambda line: None,
        export=lambda tag, ok, now: recorded.append((tag, ok)),
    )
    return recorded


def test_a_failed_run_exports_zero_and_a_clean_run_exports_one():
    assert _run("crowdsec-appsec-verify", 1) == [("crowdsec-appsec-verify", False)]
    assert _run("crowdsec-remote-allowlist", 0) == [("crowdsec-remote-allowlist", True)]


def test_a_job_with_no_kuma_monitor_exports_nothing():
    assert _run("crowdsec-bouncer-prune", 1) == []


def test_the_server_answers_get_metrics_and_nothing_else():
    exposition = jobs_metrics.Exposition({"job-a": 900}, started=1000.0)
    server = exposition.serve(0)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/metrics") as reply:
            assert b"homelab_check_deadline_seconds" in reply.read()
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/other")
        assert err.value.code == 404
        err.value.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def pod():
    [deployment] = yaml_fast.safe_load_all(
        render_role_template("crowdsec", "deployment.yaml.j2")
    )
    return deployment["spec"]["template"]["spec"]


def _port() -> int:
    """The port of the rendered `crowdsec-jobs` scrape job, which the inventory's item sets."""
    text = render_role_template("observability", "prometheus.yaml.j2")
    configmap = next(
        doc
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc.get("kind") == "ConfigMap"
    )
    config = yaml_fast.safe_load(configmap["data"]["prometheus.yml"])
    [job] = [j for j in config["scrape_configs"] if j["job_name"] == "crowdsec-jobs"]
    [static] = job["static_configs"]
    [target] = static["targets"]
    host, _, port = target.rpartition(":")
    assert host == "crowdsec.homelab.svc"
    return int(port)


def test_the_sidecar_listens_where_the_scrape_job_and_the_service_point(pod):
    [sidecar] = [c for c in pod["containers"] if c["name"] == "crowdsec-jobs"]
    env = {e["name"]: e.get("value") for e in sidecar["env"]}
    assert int(env["JOBS_METRICS_PORT"]) == _port()
    [declared] = sidecar["ports"]
    assert declared == {"name": "jobs-metrics", "containerPort": _port()}
    [service] = yaml_fast.safe_load_all(
        render_role_template("crowdsec", "service.yaml.j2")
    )
    [exposed] = [p for p in service["spec"]["ports"] if p["name"] == "jobs-metrics"]
    assert (exposed["port"], exposed["targetPort"]) == (_port(), "jobs-metrics")


def test_prometheus_may_reach_the_sidecar_port():
    docs = list(
        yaml_fast.safe_load_all(
            render_role_template("netpol-baseline", "networkpolicy-crowdsec.yaml.j2")
        )
    )
    ports = {
        p["port"]
        for doc in docs
        if doc
        for rule in doc["spec"]["ingress"]
        for source in rule.get("from", [])
        if source.get("podSelector", {}).get("matchLabels") == {"app": "prometheus"}
        for p in rule["ports"]
    }
    assert _port() in ports


def _rule_checks(text: str) -> set[str]:
    """Every `check="..."` literal in the sidecar's failed rules."""
    return set(re.findall(r'check="([a-z-]+)"', text))


def test_the_rules_name_exactly_the_jobs_the_sidecar_exports():
    assert _rule_checks(RULES.read_text()) == set(PUSHING)


def test_a_misspelt_check_literal_is_flagged():
    text = RULES.read_text().replace(
        'check="crowdsec-appsec-verify"', 'check="crowdsec-appsec-verfy"'
    )
    assert _rule_checks(text) != set(PUSHING)
