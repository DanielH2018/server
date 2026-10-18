"""The registry role's ghcr.io pull-through mirror (D5 Open question 2, ruled 2026-10-16).

The mirror gives a node a LAN copy of a flux-owned GHCR image, so nut can cold-start during a
WAN outage. These properties keep it from being decorative or from breaking the push store:

  * the mirror is its OWN Deployment. A registry with `REGISTRY_PROXY_REMOTEURL` refuses every
    manifest Put and Delete, so the setting on the push store would break every image-builder
    push and registry-gc.sh's untag DELETEs. And a mirror inside the push store's pod could be
    collected only by scaling that pod, which the operator ruled out on #5680;
  * it keeps the address every consumer names: the loopback hostPort on the registry node, and
    the pinned ClusterIP through a Service that routes each port to one Deployment by name;
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

import re

from lib import yaml_fast
from _manifest_guards import K8S, _render, _role_context
from _shell_render import rendered_shell_text

ROLE = "registry"
CTX = _role_context(ROLE, domain="example.com")


def _docs(template: str) -> list[dict]:
    rendered = _render(K8S / ROLE / "templates" / template, **CTX)
    return [d for d in yaml_fast.safe_load_all(rendered) if d]


def _deployments() -> dict[str, dict]:
    return {
        d["metadata"]["name"]: d
        for template in ("deployment.yaml.j2", "ghcr-mirror.yaml.j2")
        for d in _docs(template)
        if d["kind"] == "Deployment"
    }


def _pod(deployment: dict) -> dict:
    return deployment["spec"]["template"]["spec"]


def _env(container: dict) -> dict[str, str]:
    return {e["name"]: str(e["value"]) for e in container.get("env", [])}


def _mirror() -> dict:
    mirrors = [
        c
        for d in _deployments().values()
        for c in _pod(d)["containers"]
        if "REGISTRY_PROXY_REMOTEURL" in _env(c)
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


def test_the_mirror_is_its_own_deployment():
    """The operator's ruling on #5680: the mirror's GC must never scale the push store.

    A mirror container inside deploy/registry's pod can only be taken offline by scaling that
    Deployment, so the push store's pod must carry no proxy container at all.
    """
    deployments = _deployments()
    (push,) = _pod(deployments["registry"])["containers"]
    proxied = sorted(k for k in _env(push) if k.startswith("REGISTRY_PROXY_"))
    assert not proxied, f"the push store carries {proxied}; it would refuse every push"
    (mirror,) = _pod(deployments["ghcr-mirror"])["containers"]
    assert mirror == _mirror(), mirror["name"]


def test_the_mirror_renders_no_replica_count():
    """registry-gc.sh scales deploy/ghcr-mirror to zero for the prune. A rendered `replicas`
    would give the apply, and later Flux, a claim on the field that restores the pod mid-GC."""
    assert "replicas" not in _deployments()["ghcr-mirror"]["spec"]


def test_the_mirror_answers_on_the_address_consumers_name():
    """daniel-box's registries.yaml names `localhost:<port>`, so the mirror keeps the loopback
    hostPort on the registry node. Moving either is a setup-plane change on every node."""
    pod = _pod(_deployments()["ghcr-mirror"])
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": CTX["k8s_registry_node"]}
    (port,) = _mirror()["ports"]
    assert (
        port["hostPort"]
        == port["containerPort"]
        == CTX["k8s_registry_ghcr_mirror_port"]
    )
    assert port["hostIP"] == CTX["k8s_registry_host_ip"], port


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
    pod = _pod(_deployments()["ghcr-mirror"])
    volumes = {
        v["name"]: v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"]
    }
    mirror = {volumes[m["name"]] for m in _mirror().get("volumeMounts", [])}
    assert mirror == {CTX["registry_k8s_ghcr_mirror_claim"]}, mirror
    assert CTX["registry_k8s_claim"] not in mirror, mirror
    gc_claims = _job_claims(_gc_jobs()["registry-gc"])
    assert not mirror & gc_claims, (mirror, gc_claims)


def _gc_jobs() -> dict[str, dict]:
    return {
        d["metadata"]["name"]: d
        for template in ("gc-job.yaml.j2", "ghcr-mirror-gc-job.yaml.j2")
        for d in _docs(template)
        if d["kind"] == "Job"
    }


def test_each_gc_job_file_holds_one_store():
    """registry-gc.sh applies each file with only that store's Deployment scaled down."""
    push = {
        d["metadata"]["name"] for d in _docs("gc-job.yaml.j2") if d["kind"] == "Job"
    }
    mirror = {
        d["metadata"]["name"]
        for d in _docs("ghcr-mirror-gc-job.yaml.j2")
        if d["kind"] == "Job"
    }
    assert (push, mirror) == ({"registry-gc"}, {"registry-gc-ghcr-mirror"})


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
        for d in _docs("ghcr-mirror-gc-job.yaml.j2")
        if d["kind"] == "ConfigMap"
        and d["metadata"]["name"]
        == volumes["ghcr-mirror-prune-script"]["configMap"]["name"]
    ]
    source = (K8S / ROLE / "files" / "ghcr-mirror-prune.sh").read_text()
    assert script["data"]["ghcr-mirror-prune.sh"] == source


def test_the_service_routes_each_port_to_one_deployment_by_name():
    """One Service fronts both Deployments, so agents keep the pinned ClusterIP.

    The EndpointSlice controller leaves a pod out of a port whose NAMED targetPort it does not
    declare. A numeric targetPort, or a name both pods declare, would send part of the push
    traffic to the mirror pod or the reverse.
    """
    (service,) = _docs("service.yaml.j2")
    assert service["spec"]["clusterIP"] == CTX["k8s_registry_cluster_ip"], service
    selector = service["spec"]["selector"]
    deployments = _deployments()
    for name, deployment in deployments.items():
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert selector.items() <= labels.items(), (name, selector, labels)
    routed = {}
    for port in service["spec"]["ports"]:
        target = port["targetPort"]
        assert isinstance(target, str), f"targetPort {target!r} is not a port name"
        declaring = {
            name: p["containerPort"]
            for name, d in deployments.items()
            for c in _pod(d)["containers"]
            for p in c.get("ports", [])
            if p.get("name") == target
        }
        assert len(declaring) == 1, (target, declaring)
        ((owner, container_port),) = declaring.items()
        assert container_port == port["port"], (target, container_port, port)
        routed[port["port"]] = owner
    assert routed == {
        CTX["k8s_registry_port"]: "registry",
        CTX["k8s_registry_ghcr_mirror_port"]: "ghcr-mirror",
    }, routed


def test_registry_gc_applies_the_mirror_manifest_the_deploy_stages():
    """The mirror's Job is its own file, which the cron reads at GC time (#1669)."""
    (read,) = re.findall(
        r"^MIRROR_JOB_MANIFEST=(\S+)$",
        rendered_shell_text("k8s", "registry", "registry-gc.sh.j2"),
        re.MULTILINE,
    )
    tasks = (K8S / ROLE / "tasks" / "main.yml").read_text()
    assert f"dest: {read}\n" in tasks, (
        f"no task stages {read}, the file the cron applies"
    )
