"""The `blackbox-ingress` scrape job probes every ingress tile, built from `containers_list`.

observability's `prometheus.yaml.j2` renders one target per tile the `kuma_ingress_monitors`
filter yields (docs/target-architecture/monitoring/m4-blackbox-ingress.md, *The probe job*).
These guards render the job from the real inventory, with an entry added or changed where the
question is what moves, so each states what Prometheus would receive.

The rejecting half is `test_an_entry_the_job_does_not_probe_is_flagged`: an entry whose
`hostname` the rendered job lacks must come back from `unprobed_hostnames`. The named members
are `homepage`, behind SSO, and `authelia`, the home-critical entry whose hostname differs from
its name.

Run: uv run pytest ansible/tests/services/test_blackbox_ingress_targets.py
"""

import copy

from _k8s_render import host_context, render_role_template
from _kuma_entities import _entities
from kuma_monitors import kuma_ingress_monitors
from lib import yaml_fast
from service_tier import in_service_tier

JOB = "blackbox-ingress"
REQUIRED_SERVICES = frozenset({"homepage", "authelia"})
_NEW = {"name": "newsvc", "platform": "k8s", "hostname": "newsvc", "port": 8080}


def job_targets(containers_list: list[dict] | None = None) -> dict[str, dict]:
    """The rendered job's targets, each mapped to its labels, in render order."""
    overrides = {"containers_list": containers_list} if containers_list else None
    text = render_role_template("observability", "prometheus.yaml.j2", overrides)
    configmap = next(
        doc
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc.get("kind") == "ConfigMap"
    )
    config = yaml_fast.safe_load(configmap["data"]["prometheus.yml"])
    [job] = [j for j in config["scrape_configs"] if j["job_name"] == JOB]
    return {
        target: item["labels"]
        for item in job["static_configs"]
        for target in item["targets"]
    }


def tile_url(hostname: str, domain: str) -> str:
    """The URL Kuma's ingress tile probes, built the way its template builds it."""
    return f"https://{hostname}.local.{domain}"


def unprobed_hostnames(
    targets: dict[str, dict], containers_list: list[dict]
) -> list[str]:
    """The hostnames of the tiles the filter yields that the job has no target for."""
    domain = host_context()["domain"]
    return [
        tile["hostname"]
        for tile in kuma_ingress_monitors(containers_list)
        if tile_url(tile["hostname"], domain) not in targets
    ]


def _entries() -> list[dict]:
    return copy.deepcopy(host_context()["containers_list"])


def test_the_targets_are_the_filters_tiles_string_for_string():
    ctx = host_context()
    targets = job_targets()
    expected = [
        tile_url(tile["hostname"], ctx["domain"])
        for tile in kuma_ingress_monitors(ctx["containers_list"])
    ]
    assert list(targets) == expected
    services = {labels["service"] for labels in targets.values()}
    assert REQUIRED_SERVICES <= services, sorted(REQUIRED_SERVICES - services)
    assert unprobed_hostnames(targets, ctx["containers_list"]) == []


def test_the_targets_equal_kumas_ingress_tile_urls():
    """The soak-join check: the soak queries join Kuma's `monitor_url` to the alert's
    `instance`, so the two strings must match. M4's PR 4 deletes Kuma's loop and this test."""
    cl = host_context()["containers_list"]
    entities = _entities()
    kuma_urls = [
        entities[tile["id"] + ".json"]["url"] for tile in kuma_ingress_monitors(cl)
    ]
    assert list(job_targets()) == kuma_urls


def test_each_targets_labels_follow_its_entry_sso_and_tier():
    ctx = host_context()
    cl = ctx["containers_list"]
    targets = job_targets()
    for tile in kuma_ingress_monitors(cl):
        critical = in_service_tier(cl, tile["entry"], "home-critical")
        assert targets[tile_url(tile["hostname"], ctx["domain"])] == {
            "service": tile["entry"],
            "module": "http_sso_302" if tile["behind_sso"] else "http_2xx",
            "page_tier": "critical" if critical else "warning",
        }, tile
    assert targets[tile_url("auth", ctx["domain"])]["page_tier"] == "critical"
    assert targets[tile_url("homepage", ctx["domain"])]["module"] == "http_sso_302"


def test_a_new_entry_gets_a_target_and_kuma_false_removes_one():
    domain = host_context()["domain"]
    sso = {**_NEW, "use_authelia": True, "auth_tier": "one_factor"}
    added = job_targets([*_entries(), sso])
    assert added[tile_url("newsvc", domain)] == {
        "service": "newsvc",
        "module": "http_sso_302",
        "page_tier": "warning",
    }
    entries = _entries()
    next(e for e in entries if e["name"] == "homepage")["kuma"] = False
    assert tile_url("homepage", domain) not in job_targets(entries)


def test_an_entry_the_job_does_not_probe_is_flagged():
    targets = job_targets()
    assert unprobed_hostnames(targets, [*_entries(), _NEW]) == ["newsvc"]


def test_every_target_host_is_pinned_in_the_exporters_host_aliases():
    ctx = host_context()
    deployment = yaml_fast.safe_load(
        render_role_template(
            "blackbox-exporter",
            "deployment.yaml.j2",
            {
                "hostvars": {
                    ctx["k8s_primary_node"]: {"containers_list": ctx["containers_list"]}
                }
            },
        )
    )
    aliased = {
        name
        for alias in deployment["spec"]["template"]["spec"]["hostAliases"]
        if alias["ip"] == ctx["k3s_metallb_ingress_vip"]
        for name in alias["hostnames"]
    }
    hosts = {target.removeprefix("https://") for target in job_targets()}
    assert len(hosts) >= 26
    assert hosts <= aliased, sorted(hosts - aliased)
