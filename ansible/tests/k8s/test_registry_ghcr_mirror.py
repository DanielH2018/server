"""The registry pod's ghcr.io pull-through mirror (D5 Open question 2, ruled 2026-10-16).

The mirror gives a node a LAN copy of a flux-owned GHCR image, so nut can cold-start during a
WAN outage. Five properties keep it from being decorative or from breaking the push store:

  * the mirror is its OWN container. A registry with `REGISTRY_PROXY_REMOTEURL` refuses every
    manifest Put and Delete, so the setting on the push store would break every image-builder
    push and registry-gc.sh's untag DELETEs;
  * its TTL is zero. The 168h default drops nut's image a week after its last rebuild;
  * it binds neither the push store's port nor the baked config's debug port :5001, which the
    two containers would otherwise both claim in the shared pod netns;
  * its store is its own claim. Every ghcr.io image in the cluster pulls through it and none
    expires, so on the push store's claim it would fill the volume and fail every build;
  * its weekly prune keeps untagged revisions. The push store's `--delete-untagged` would
    delete every digest-pinned image the mirror caches (#5541).

The `registries.yaml` half is `ansible/tests/setup/test_k3s_registries_mirror_ghcr.py`, and the
NetworkPolicy half is in `ansible/tests/k8s/test_registry_netpol_grants_follow_the_pinned_node.py`.
"""

from lib import yaml_fast
from _manifest_guards import K8S, _render, _role_context

ROLE = "registry"
CTX = _role_context(ROLE, domain="example.com")


def _docs(template: str) -> list[dict]:
    rendered = _render(K8S / ROLE / "templates" / template, **CTX)
    return [d for d in yaml_fast.safe_load_all(rendered) if d]


def _containers() -> dict[str, dict]:
    (deployment,) = [
        d for d in _docs("deployment.yaml.j2") if d["kind"] == "Deployment"
    ]
    return {c["name"]: c for c in deployment["spec"]["template"]["spec"]["containers"]}


def _env(container: dict) -> dict[str, str]:
    return {e["name"]: str(e["value"]) for e in container.get("env", [])}


def _mirror() -> dict:
    mirrors = [
        c for c in _containers().values() if "REGISTRY_PROXY_REMOTEURL" in _env(c)
    ]
    assert len(mirrors) == 1, (
        f"expected exactly one container proxying ghcr.io, found {[c['name'] for c in mirrors]}"
    )
    return mirrors[0]


def test_one_container_mirrors_ghcr_with_no_expiry():
    env = _env(_mirror())
    assert env["REGISTRY_PROXY_REMOTEURL"] == "https://ghcr.io", env
    # distribution's proxyregistry.go reads only a duration that is not > 0 as "never expire";
    # an unset TTL is the 168h default.
    assert env.get("REGISTRY_PROXY_TTL") in {"0", "0s"}, env


def test_the_push_store_is_not_a_proxy():
    """A proxy refuses Put and Delete, so the push store must carry no REGISTRY_PROXY_*."""
    push = _containers()["registry"]
    proxied = sorted(k for k in _env(push) if k.startswith("REGISTRY_PROXY_"))
    assert not proxied, f"the push store carries {proxied}; it would refuse every push"


def test_the_mirror_binds_its_own_ports():
    env = _env(_mirror())
    port = CTX["k8s_registry_ghcr_mirror_port"]
    assert port != CTX["k8s_registry_port"], port
    assert env["REGISTRY_HTTP_ADDR"].rsplit(":", 1)[1] == str(port), env
    # :5001 is the baked config's debug address, which the push store's container holds.
    debug = env.get("REGISTRY_HTTP_DEBUG_ADDR", ":5001").rsplit(":", 1)[1]
    assert debug not in {"5001", str(CTX["k8s_registry_port"]), str(port)}, env
    container_ports = {p["containerPort"] for p in _mirror()["ports"]}
    assert container_ports == {port}, container_ports


def test_the_mirror_stores_on_its_own_claim():
    """Every ghcr.io image pulls through the mirror and none expires, so it gets its own volume.

    On the push store's claim the cache would fill it and fail every image build. The push
    store's GC Job mounts only the push store's claim, so its `--delete-untagged` never walks
    the cache.
    """
    (deployment,) = [
        d for d in _docs("deployment.yaml.j2") if d["kind"] == "Deployment"
    ]
    volumes = {
        v["name"]: v["persistentVolumeClaim"]["claimName"]
        for v in deployment["spec"]["template"]["spec"]["volumes"]
    }

    def claims(container: dict) -> set[str]:
        return {volumes[m["name"]] for m in container.get("volumeMounts", [])}

    mirror = claims(_mirror())
    assert mirror, "the mirror mounts no claim"
    assert CTX["registry_k8s_claim"] not in mirror, mirror
    gc_claims = _job_claims(_gc_jobs()["registry-gc"])
    assert not mirror & gc_claims, (mirror, gc_claims)


def _gc_jobs() -> dict[str, dict]:
    return {
        d["metadata"]["name"]: d for d in _docs("gc-job.yaml.j2") if d["kind"] == "Job"
    }


def _job_claims(job: dict) -> set[str]:
    return {
        v["persistentVolumeClaim"]["claimName"]
        for v in job["spec"]["template"]["spec"]["volumes"]
        if "persistentVolumeClaim" in v
    }


def test_the_mirror_gc_job_collects_the_mirror_store_without_delete_untagged():
    """A digest-pinned pull is cached as a revision with no tag, so --delete-untagged on the
    mirror's store would delete nearly every cached image (#5541)."""
    job = _gc_jobs()["registry-gc-ghcr-mirror"]
    assert _job_claims(job) == {CTX["registry_k8s_ghcr_mirror_claim"]}, job
    (gc,) = job["spec"]["template"]["spec"]["containers"]
    assert gc["args"][0] == "garbage-collect", gc["args"]
    assert "--delete-untagged" not in gc["args"], gc["args"]
    (prune,) = job["spec"]["template"]["spec"]["initContainers"]
    assert prune["command"] == ["sh", "/prune/ghcr-mirror-prune.sh"], prune
    assert _env(prune)["KEEP_NEWEST"] == str(
        CTX["registry_k8s_ghcr_mirror_keep_revisions"]
    )


def test_the_mirror_prune_reads_an_optional_keep_set_and_the_script_verbatim():
    """registry-gc.sh deletes the keep set before each run, so a missing one must start the prune
    with no file (which prunes nothing) rather than block the pod or reuse an old set."""
    job = _gc_jobs()["registry-gc-ghcr-mirror"]
    volumes = {v["name"]: v for v in job["spec"]["template"]["spec"]["volumes"]}
    assert volumes["ghcr-mirror-keep"]["configMap"] == {
        "name": "registry-gc-ghcr-mirror-keep",
        "optional": True,
    }, volumes["ghcr-mirror-keep"]
    (script,) = [
        d
        for d in _docs("gc-job.yaml.j2")
        if d["kind"] == "ConfigMap"
        and d["metadata"]["name"]
        == volumes["ghcr-mirror-prune-script"]["configMap"]["name"]
    ]
    source = (K8S / ROLE / "files" / "ghcr-mirror-prune.sh").read_text()
    assert script["data"]["ghcr-mirror-prune.sh"] == source


def test_the_service_exposes_the_mirror_on_the_pinned_cluster_ip():
    (service,) = _docs("service.yaml.j2")
    assert service["spec"]["clusterIP"] == CTX["k8s_registry_cluster_ip"], service
    ports = {p["port"]: p["targetPort"] for p in service["spec"]["ports"]}
    mirror = CTX["k8s_registry_ghcr_mirror_port"]
    assert ports.get(mirror) == mirror, ports
