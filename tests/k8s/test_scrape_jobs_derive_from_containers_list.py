"""A plain exporter's scrape job renders from the `metrics` item on its containers_list entry (#3743).

observability's `prometheus.yaml.j2` used to hand-copy each exporter's Service DNS and port, and
the owning role defined the same port again. The port is now declared once, as a `metrics` item,
and both the scrape job (`scrape_jobs`) and the owning role's Service (`metrics_port`) read it.
A job that stays hand-written for its auth reads its entry's own `port` through `entry_port`
(#3816). `template_filters/scrape_jobs.py` documents the item's keys.

Run: uv run pytest tests/k8s/test_scrape_jobs_derive_from_containers_list.py
"""

import copy

import pytest
from lib.guard_jinja_env import FilterError, repo_filters

from _k8s_render import host_context, render_pod_env, render_role_template
from lib import yaml_fast
from renderer.template_filters.scrape_jobs import entry_port, metrics_port, scrape_jobs

# Every job derived from a `metrics` item: the seven from #3743, `nut` from #3816 and
# `monitor-bridge`, the check exporter's, and `navidrome`. Each must
# still render from an entry, so a `metrics` item dropped from the inventory fails here rather
# than reading as a smaller loop.
_DERIVED = {
    "crowdsec",
    "crowdsec-jobs",
    "autofix-bridge",
    "traefik-k8s",
    "speedtest",
    "pihole",
    "loki-homelab",
    "terraria-stats",
    "valheim-stats",
    "nut",
    "monitor-bridge",
    "navidrome",
}


def _scrape_targets(prometheus_text: str) -> dict[str, list[str]]:
    return {
        job["job_name"]: [
            t for sc in job.get("static_configs", []) for t in sc["targets"]
        ]
        for job in _scrape_configs(prometheus_text).values()
    }


def _scrape_configs(prometheus_text: str) -> dict[str, dict]:
    doc = next(
        d
        for d in yaml_fast.safe_load_all(prometheus_text)
        if d and d.get("kind") == "ConfigMap"
    )
    jobs = yaml_fast.safe_load(doc["data"]["prometheus.yml"])["scrape_configs"]
    return {job["job_name"]: job for job in jobs}


def _service_ports(text: str) -> list[int]:
    return [
        port["port"]
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc.get("kind") == "Service"
        for port in doc["spec"]["ports"]
    ]


def test_every_metrics_item_renders_as_a_scrape_job():
    ctx = host_context()
    derived = scrape_jobs(ctx["services"], ctx["k8s_namespace"])
    assert {job["job"] for job in derived} == _DERIVED
    # Terraria's replica gate is the one thing that can drop a derived job, so pin it open.
    rendered = _scrape_targets(
        render_role_template(
            "observability", "prometheus.yaml.j2", {"terraria_k8s_replicas": 1}
        )
    )
    for job in derived:
        assert rendered.get(job["job"]) == [job["target"]], job


@pytest.mark.parametrize(
    ("entry_name", "job"), [("pihole-exporter", "pihole"), ("nut-exporter", "nut")]
)
def test_the_scrape_target_and_the_service_read_one_port(entry_name, job):
    """Moving the port on the entry moves both halves; a copy left in either one fails here."""
    entries = copy.deepcopy(host_context()["services"])
    entry = next(e for e in entries if e["name"] == entry_name)
    (item,) = entry["metrics"]
    assert item["port"] != 9999
    item["port"] = 9999
    overrides = {"services": entries}
    targets = _scrape_targets(
        render_role_template("observability", "prometheus.yaml.j2", overrides)
    )
    service = render_role_template(entry_name, "service.yaml.j2", overrides)
    assert targets[job] == [f"{entry_name}.homelab.svc:9999"]
    assert _service_ports(service) == [9999]


def test_the_check_exporter_listener_service_and_scrape_job_read_one_port():
    """Moving monitor-bridge's `metrics` port moves the process env, the Service and the job."""
    entries = copy.deepcopy(host_context()["services"])
    entry = next(e for e in entries if e["name"] == "monitor-bridge")
    (item,) = entry["metrics"]
    assert entry["port"] != 9999
    item["port"] = 9999
    overrides = {"services": entries}
    targets = _scrape_targets(
        render_role_template("observability", "prometheus.yaml.j2", overrides)
    )
    service = render_role_template("monitor-bridge", "service.yaml.j2", overrides)
    assert targets["monitor-bridge"] == ["monitor-bridge.homelab.svc:9999"]
    assert 9999 in _service_ports(service)
    assert render_pod_env("monitor-bridge", overrides)["CHECK_EXPORTER_PORT"] == "9999"


def test_the_nut_job_renders_its_path_interval_and_params_from_the_item():
    job = _scrape_configs(render_role_template("observability", "prometheus.yaml.j2"))[
        "nut"
    ]
    assert job["metrics_path"] == "/ups_metrics"
    assert job["scrape_interval"] == "1m"
    # The item's value is a template; a params block left unresolved would read `{{`.
    assert job["params"] == {"ups": ["apc-ups"]}


def test_a_hand_written_job_reads_its_target_port_from_the_entry():
    """HA keeps a hand-written job for its auth; the port still lives on the entry."""
    entries = copy.deepcopy(host_context()["services"])
    entry = next(e for e in entries if e["name"] == "home-assistant")
    assert entry["port"] != 9999
    entry["port"] = 9999
    targets = _scrape_targets(
        render_role_template(
            "observability", "prometheus.yaml.j2", {"services": entries}
        )
    )
    assert targets["home-assistant"] == ["home-assistant.homelab.svc:9999"]


def test_the_alloy_pi_job_reads_its_port_from_the_pis_entry():
    """daniel-box's containers_list holds no Pi entry, so the job reads the Pi's through `hosts`."""
    ctx = host_context()
    hosts = copy.deepcopy(ctx["hosts"])
    # Since H5.8 the Pi's alloy entry sits in `compose_services`, which the job reads as well.
    entry = next(
        e for e in hosts["daniel-pi"]["compose_services"] if e["name"] == "alloy"
    )
    assert entry["port"] != 9999
    entry["port"] = 9999
    targets = _scrape_targets(
        render_role_template("observability", "prometheus.yaml.j2", {"hosts": hosts})
    )
    assert targets["alloy-pi"] == [f"{ctx['k8s_pi_client_ip']}:9999"]


def test_the_gpu_jobs_pod_filter_and_the_exporters_listener_read_one_port():
    """The gpu job discovers pods, so its port lives in a relabel regex, not in a target (#3868)."""
    entries = copy.deepcopy(host_context()["services"])
    entry = next(e for e in entries if e["name"] == "gpu-exporter")
    assert entry["port"] != 9999
    entry["port"] = 9999
    overrides = {"services": entries}
    prometheus = render_role_template("observability", "prometheus.yaml.j2", overrides)
    (port_filter,) = [
        rule
        for rule in _scrape_configs(prometheus)["gpu"]["relabel_configs"]
        if rule["source_labels"] == ["__meta_kubernetes_pod_container_port_number"]
    ]
    assert port_filter["regex"] == "9999"
    assert "9101" not in prometheus
    daemonset = render_role_template("gpu-exporter", "daemonset.yaml.j2", overrides)
    (exporter,) = next(
        doc
        for doc in yaml_fast.safe_load_all(daemonset)
        if doc and doc.get("kind") == "DaemonSet"
    )["spec"]["template"]["spec"]["containers"]
    assert exporter["command"][2:4] == ["--port", "9999"]
    assert [p["containerPort"] for p in exporter["ports"]] == [9999]
    assert exporter["readinessProbe"]["tcpSocket"]["port"] == 9999


def test_a_metrics_item_defaults_to_the_entry_name_and_port():
    entries = [{"name": "speedtest", "port": 80, "metrics": [{"job": "speedtest"}]}]
    assert scrape_jobs(entries, "ns") == [
        {"job": "speedtest", "target": "speedtest.ns.svc:80"}
    ]
    assert metrics_port(entries, "speedtest") == 80


def test_scrape_jobs_is_flagged_on_a_key_a_plain_job_cannot_carry():
    entries = [
        {
            "name": "uptime-kuma",
            "metrics": [{"job": "uptime-kuma", "port": 3001, "basic_auth": {}}],
        }
    ]
    with pytest.raises(ValueError, match="basic_auth"):
        scrape_jobs(entries, "ns")


@pytest.mark.parametrize("params", [{"ups": "apc-ups"}, {"ups": [1]}, ["ups"]])
def test_scrape_jobs_is_flagged_on_params_prometheus_cannot_read(params):
    entries = [
        {
            "name": "nut-exporter",
            "metrics": [{"job": "nut", "port": 9199, "params": params}],
        }
    ]
    with pytest.raises(ValueError, match="list of strings"):
        scrape_jobs(entries, "ns")


def test_scrape_jobs_is_flagged_on_a_job_declared_twice():
    entries = [
        {"name": "a", "metrics": [{"job": "x", "port": 1}]},
        {"name": "b", "metrics": [{"job": "x", "port": 2}]},
    ]
    with pytest.raises(ValueError, match="declared by both"):
        scrape_jobs(entries, "ns")


def test_metrics_port_is_flagged_on_an_undeclared_job():
    with pytest.raises(ValueError, match="no containers_list entry"):
        metrics_port([{"name": "a", "port": 1}], "a")


def test_entry_port_is_flagged_on_an_unknown_entry_or_one_with_no_port():
    entries = [{"name": "a", "port": 1}, {"name": "b"}]
    assert entry_port(entries, "a") == 1
    with pytest.raises(ValueError, match="no `port`"):
        entry_port(entries, "b")
    with pytest.raises(ValueError, match="no containers_list entry"):
        entry_port(entries, "c")


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("entry_port", ([{"name": "a", "port": 1}], "b")),
        ("metrics_port", ([{"name": "a", "port": 1}], "no-such-job")),
        ("scrape_jobs", ([{"name": "a", "metrics": "bogus"}], "ns")),
    ],
    ids=["entry-missing", "job-missing", "metrics-malformed"],
)
def test_a_refusal_is_a_filter_error_named_for_its_filter(name, args):
    """A render failure names the filter that refused, through the filter Ansible calls (#4480)."""
    with pytest.raises(FilterError) as caught:
        repo_filters()[name](*args)
    assert str(caught.value).startswith(f"{name}: "), str(caught.value)
