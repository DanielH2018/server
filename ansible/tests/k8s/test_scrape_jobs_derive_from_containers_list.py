"""A plain exporter's scrape job renders from the `metrics` item on its containers_list entry (#3743).

observability's `prometheus.yaml.j2` used to hand-copy each exporter's Service DNS and port, and
the owning role defined the same port again. The port is now declared once, as a `metrics` item,
and both the scrape job (`scrape_jobs`) and the owning role's Service (`metrics_port`) read it.
A job that stays hand-written for its auth reads its entry's own `port` through `entry_port`
(#3816). `filter_plugins/scrape_jobs.py` documents the item's keys.

Run: uv run pytest ansible/tests/k8s/test_scrape_jobs_derive_from_containers_list.py
"""

import copy

import pytest

from _k8s_render import host_context, render_role_template
from lib import yaml_fast
from scrape_jobs import entry_port, metrics_port, scrape_jobs

# Every job derived from a `metrics` item: the seven from #3743, and `nut` from #3816. Each must
# still render from an entry, so a `metrics` item dropped from the inventory fails here rather
# than reading as a smaller loop.
_DERIVED = {
    "crowdsec",
    "traefik-k8s",
    "speedtest",
    "pihole",
    "loki-homelab",
    "terraria-stats",
    "valheim-stats",
    "nut",
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
    derived = scrape_jobs(ctx["containers_list"], ctx["k8s_namespace"])
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
    entries = copy.deepcopy(host_context()["containers_list"])
    entry = next(e for e in entries if e["name"] == entry_name)
    (item,) = entry["metrics"]
    assert item["port"] != 9999
    item["port"] = 9999
    overrides = {"containers_list": entries}
    targets = _scrape_targets(
        render_role_template("observability", "prometheus.yaml.j2", overrides)
    )
    service = render_role_template(entry_name, "service.yaml.j2", overrides)
    assert targets[job] == [f"{entry_name}.homelab.svc:9999"]
    assert _service_ports(service) == [9999]


def test_the_nut_job_renders_its_path_interval_and_params_from_the_item():
    job = _scrape_configs(render_role_template("observability", "prometheus.yaml.j2"))[
        "nut"
    ]
    assert job["metrics_path"] == "/ups_metrics"
    assert job["scrape_interval"] == "1m"
    # The item's value is a template; a params block left unresolved would read `{{`.
    assert job["params"] == {"ups": ["apc-ups"]}


@pytest.mark.parametrize("entry_name", ["uptime-kuma", "home-assistant"])
def test_a_hand_written_job_reads_its_target_port_from_the_entry(entry_name):
    """Kuma and HA keep hand-written jobs for their auth; the port still lives on the entry."""
    entries = copy.deepcopy(host_context()["containers_list"])
    entry = next(e for e in entries if e["name"] == entry_name)
    assert entry["port"] != 9999
    entry["port"] = 9999
    targets = _scrape_targets(
        render_role_template(
            "observability", "prometheus.yaml.j2", {"containers_list": entries}
        )
    )
    assert targets[entry_name] == [f"{entry_name}.homelab.svc:9999"]


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
