"""homelab-mcp's file jail reads a checkout that a timer keeps current (#8409).

The pod mounts directories of its node's /home/<sys_user>/server checkout through hostPath.
Only a host whose package lists the `checkout-sync` component fast-forwards that checkout, so
the pod must be pinned to such a host: a preference would let it land on a stale or tree-less
checkout whenever the preferred node is drained.

Run: uv run pytest tests/services/test_homelab_mcp_source_mounts.py
"""

from pathlib import PurePosixPath

from _helpers import REPO
from _k8s_render import host_context, pod_spec, render_role_template
from lib import yaml_fast


def _synced_hosts() -> set[str]:
    hosts = {
        package.parent.name
        for package in (REPO / "hosts").glob("*/package.yml")
        if "checkout-sync" in yaml_fast.safe_load(package.read_text())["components"]
    }
    assert "daniel-box" in hosts, "the checkout-sync census found no daniel-box package"
    return hosts


def _pod() -> dict:
    return pod_spec(
        yaml_fast.safe_load(render_role_template("homelab-mcp", "deployment.yaml.j2"))
    )


def test_the_pod_is_pinned_to_a_host_whose_checkout_is_synced() -> None:
    affinity = _pod()["affinity"]["nodeAffinity"]
    assert "preferredDuringSchedulingIgnoredDuringExecution" not in affinity
    terms = affinity["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"
    ]
    nodes = {v for t in terms for e in t["matchExpressions"] for v in e["values"]}
    assert nodes and nodes <= _synced_hosts(), f"{nodes} has a host checkout-sync skips"


def test_the_jail_mounts_k8s_inventory_and_tests_read_only_and_no_ansible_tree() -> (
    None
):
    pod = _pod()
    [container] = pod["containers"]
    checkout = PurePosixPath(f"/home/{host_context()['sys_user']}/server")
    host_paths = {
        v["name"]: PurePosixPath(v["hostPath"]["path"])
        for v in pod["volumes"]
        if "hostPath" in v
    }
    assert sorted(p.relative_to(checkout).as_posix() for p in host_paths.values()) == [
        "inventory",
        "k8s",
        "tests",
    ]
    for path in host_paths.values():
        assert (REPO / path.relative_to(checkout)).is_dir(), (
            f"{path} is not in the repo"
        )

    root = PurePosixPath(
        next(e["value"] for e in container["env"] if e["name"] == "HOMELAB_FILE_ROOT")
    )
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    for name, path in host_paths.items():
        assert mounts[name]["readOnly"] is True
        assert PurePosixPath(mounts[name]["mountPath"]) == root / path.name
