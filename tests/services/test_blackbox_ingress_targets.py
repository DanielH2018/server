"""The `blackbox-ingress` scrape job probes every ingress tile, built from `containers_list`.

observability's `prometheus.yaml.j2` renders one target per tile the `ingress_probe_targets`
filter yields (ADR-0026).
These guards render the job from the real inventory, with an entry added or changed where the
question is what moves, so each states what Prometheus would receive.

The rejecting half is `test_an_entry_the_job_does_not_probe_is_flagged`: an entry whose
`hostname` the rendered job lacks must come back from `unprobed_hostnames`. The named members
are `homepage`, behind SSO, and `authelia`, the home-critical entry whose hostname differs from
its name.

Run: uv run pytest tests/services/test_blackbox_ingress_targets.py
"""

import copy

from _k8s_render import host_context, render_role_template
from renderer.template_filters.ingress_probes import ingress_probe_targets
from lib import yaml_fast
from renderer.template_filters.service_tier import in_service_tier

JOB = "blackbox-ingress"
REQUIRED_SERVICES = frozenset({"homepage", "authelia"})
OPTED_OUT = frozenset(
    {
        "crowdsec",
        "ical-proxy",
        "jellyfin",
        "livesync",
        "loki-homelab",
        "pihole",
        "scrutiny",
    }
)
_NEW = {"name": "newsvc", "hostname": "newsvc", "port": 8080}


def job_targets(services: list[dict] | None = None) -> dict[str, dict]:
    """The rendered job's targets, each mapped to its labels, in render order."""
    overrides = {"services": services} if services else None
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
    """The URL a target probes: the entry's ingress name on the LAN domain."""
    return f"https://{hostname}.local.{domain}"


def unprobed_hostnames(targets: dict[str, dict], services: list[dict]) -> list[str]:
    """The hostnames of the tiles the filter yields that the job has no target for."""
    domain = host_context()["domain"]
    return [
        tile["hostname"]
        for tile in ingress_probe_targets(services)
        if tile_url(tile["hostname"], domain) not in targets
    ]


def entries_with_the_retired_key(services: list[dict]) -> list[str]:
    """The entries still carrying `kuma:`, which the filter ignores, so its opt-out is lost."""
    return [entry["name"] for entry in services if "kuma" in entry]


def _entries() -> list[dict]:
    return copy.deepcopy(host_context()["services"])


def test_no_entry_carries_the_retired_kuma_key_is_clean():
    assert entries_with_the_retired_key(_entries()) == []


def test_an_entry_with_the_retired_kuma_key_is_flagged():
    assert entries_with_the_retired_key([{**_NEW, "kuma": False}]) == ["newsvc"]


def test_the_opted_out_entries_render_no_target():
    routed = {e["name"] for e in _entries() if e.get("hostname")}
    assert OPTED_OUT <= routed, sorted(OPTED_OUT - routed)
    services = {labels["service"] for labels in job_targets().values()}
    assert not OPTED_OUT & services, sorted(OPTED_OUT & services)


def test_the_targets_are_the_filters_tiles_string_for_string():
    ctx = host_context()
    targets = job_targets()
    expected = [
        tile_url(tile["hostname"], ctx["domain"])
        for tile in ingress_probe_targets(ctx["services"])
    ]
    assert list(targets) == expected
    services = {labels["service"] for labels in targets.values()}
    assert REQUIRED_SERVICES <= services, sorted(REQUIRED_SERVICES - services)
    assert unprobed_hostnames(targets, ctx["services"]) == []


def test_each_targets_labels_follow_its_entry_sso_and_tier():
    ctx = host_context()
    cl = ctx["services"]
    targets = job_targets()
    for tile in ingress_probe_targets(cl):
        critical = in_service_tier(cl, tile["entry"], "home-critical")
        assert targets[tile_url(tile["hostname"], ctx["domain"])] == {
            "service": tile["entry"],
            "module": "http_sso_302" if tile["behind_sso"] else "http_2xx",
            "page_tier": "critical" if critical else "warning",
        }, tile
    assert targets[tile_url("auth", ctx["domain"])]["page_tier"] == "critical"
    assert targets[tile_url("homepage", ctx["domain"])]["module"] == "http_sso_302"


def test_a_new_entry_gets_a_target_and_probe_false_removes_one():
    domain = host_context()["domain"]
    sso = {**_NEW, "use_authelia": True, "auth_tier": "one_factor"}
    added = job_targets([*_entries(), sso])
    assert added[tile_url("newsvc", domain)] == {
        "service": "newsvc",
        "module": "http_sso_302",
        "page_tier": "warning",
    }
    entries = _entries()
    next(e for e in entries if e["name"] == "homepage")["probe"] = False
    assert tile_url("homepage", domain) not in job_targets(entries)


def test_an_entry_the_job_does_not_probe_is_flagged():
    targets = job_targets()
    assert unprobed_hostnames(targets, [*_entries(), _NEW]) == ["newsvc"]


def test_every_target_host_is_pinned_in_the_exporters_host_aliases():
    ctx = host_context()
    deployment = yaml_fast.safe_load(
        render_role_template("blackbox-exporter", "deployment.yaml.j2")
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
