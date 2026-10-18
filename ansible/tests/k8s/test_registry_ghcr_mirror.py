"""The registry pod's ghcr.io pull-through mirror (D5 Open question 2, ruled 2026-10-16).

The mirror gives a node a LAN copy of a flux-owned GHCR image, so nut can cold-start during a
WAN outage. Four properties keep it from being decorative or from breaking the push store:

  * the mirror is its OWN container. A registry with `REGISTRY_PROXY_REMOTEURL` refuses every
    manifest Put and Delete, so the setting on the push store would break every image-builder
    push and registry-gc.sh's untag DELETEs;
  * its TTL is zero. The 168h default drops nut's image a week after its last rebuild;
  * it binds neither the push store's port nor the baked config's debug port :5001, which the
    two containers would otherwise both claim in the shared pod netns;
  * its store sits outside the push store's `docker/registry/v2` tree, which the GC Job's
    garbage-collect walks.

The `registries.yaml` half is `ansible/tests/setup/test_k3s_registries_mirror_ghcr.py`, and the
NetworkPolicy half is in `ansible/tests/k8s/test_registry_netpol_grants_follow_the_pinned_node.py`.
"""

import posixpath

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


def test_the_mirror_store_is_outside_the_collected_tree():
    push_root = _env(_containers()["registry"])[
        "REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY"
    ]
    mirror_root = _env(_mirror())["REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY"]
    collected = posixpath.join(push_root, "docker")
    assert mirror_root != push_root, mirror_root
    assert not (mirror_root + "/").startswith(collected + "/"), (
        f"the mirror store {mirror_root} sits inside {collected}, which garbage-collect walks"
    )


def test_the_service_exposes_the_mirror_on_the_pinned_cluster_ip():
    (service,) = _docs("service.yaml.j2")
    assert service["spec"]["clusterIP"] == CTX["k8s_registry_cluster_ip"], service
    ports = {p["port"]: p["targetPort"] for p in service["spec"]["ports"]}
    mirror = CTX["k8s_registry_ghcr_mirror_port"]
    assert ports.get(mirror) == mirror, ports
