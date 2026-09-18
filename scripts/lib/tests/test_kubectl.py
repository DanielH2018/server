"""The shared kubectl runner: one argv shape, and every call names the cluster it must reach.

The wrong-cluster refusal used to guard one caller, `probe.py health` (#1663). Now it sits in
the invoker, so the tests here are the ones that prove it fires for everyone — plus the census
at the bottom, which is what keeps a new caller from building its own argv again (#2062).

Run: uv run pytest scripts/lib/tests/test_kubectl.py
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
from lib import kubectl as kubectl_lib
from lib.repo_paths import REPO

PROD_NODES = {
    "items": [{"metadata": {"name": n}} for n in ("daniel-box", "daniel-server")]
}
STAGE_NODES = {"items": [{"metadata": {"name": "daniel-stage"}}]}


@pytest.fixture(autouse=True)
def _fresh_identity_cache():
    """Each test starts with no cached cluster identity and ends leaving none behind."""
    kubectl_lib.forget_served_cluster()
    yield
    kubectl_lib.forget_served_cluster()


class _FakeCluster:
    """Stand in a cluster: discovery resolves, and `get nodes` answers with `nodes`.

    `tools` is the `Tools` to hand the runner; `argvs` is what it ran, so a test can assert
    what happened after the identity read. `stdout`/`returncode` shape every other answer.
    """

    def __init__(self, kubeconfig):
        self.nodes = PROD_NODES
        self.argvs = []
        self.stdout = "{}"
        self.returncode = 0
        self.tools = kubectl_lib.Tools(
            run=self._run,
            find_tool=lambda name: "/usr/local/bin/kubectl",
            find_kubeconfig=lambda: kubeconfig,
        )

    def _run(self, argv, timeout):
        self.argvs.append(argv)
        if argv[-4:] == ["get", "nodes", "-o", "json"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.nodes), "")
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "boom")

    def kubectl(self, cluster, *args, **kwargs):
        return kubectl_lib.kubectl(cluster, *args, tools=self.tools, **kwargs)

    def kubectl_json(self, cluster, *args, **kwargs):
        return kubectl_lib.kubectl_json(cluster, *args, tools=self.tools, **kwargs)


@pytest.fixture
def cluster(tmp_path):
    cfg = tmp_path / "kubeconfig"
    cfg.write_text("")
    return _FakeCluster(cfg)


# ── the pure pieces ─────────────────────────────────────────────────────────────────────────


def test_argv_is_the_bare_binary_and_the_arguments():
    assert kubectl_lib.kubectl_argv("get", "pods") == ["kubectl", "get", "pods"]


def test_argv_carries_a_discovered_binary_and_kubeconfig():
    assert kubectl_lib.kubectl_argv(
        "get", "pods", binary="/usr/local/bin/kubectl", kubeconfig="/tmp/kc"
    ) == ["/usr/local/bin/kubectl", "--kubeconfig", "/tmp/kc", "get", "pods"]


def test_privileged_argv_is_sudo_in_front():
    assert kubectl_lib.kubectl_argv("exec", "x", privileged=True)[:2] == [
        "sudo",
        "kubectl",
    ]


def test_cluster_of_reads_each_clusters_nodes():
    assert kubectl_lib.cluster_of(kubectl_lib.node_names(PROD_NODES)) == "prod"
    assert kubectl_lib.cluster_of(kubectl_lib.node_names(STAGE_NODES)) == "stage"


def test_cluster_of_is_unknown_for_a_name_from_neither_cluster():
    assert kubectl_lib.cluster_of(["some-other-node"]) is None
    assert kubectl_lib.cluster_of([]) is None


def test_a_new_node_in_a_cluster_does_not_make_it_unknown():
    """Membership, not equality — otherwise adding a node refuses every call at once."""
    assert kubectl_lib.cluster_of(["daniel-box", "daniel-server", "daniel-3"]) == "prod"


def test_cluster_for_host_names_the_cluster_a_node_stands_in():
    assert kubectl_lib.cluster_for_host("daniel-server") == "prod"
    assert kubectl_lib.cluster_for_host("daniel-stage") == "stage"
    assert kubectl_lib.cluster_for_host("daniel-pi") is None


def test_asking_for_the_cluster_this_kubectl_serves_is_clean():
    assert kubectl_lib.cluster_refusal("prod", "prod") is None


def test_asking_for_staging_against_a_prod_kubectl_is_flagged():
    """The failure #1663 is about: this returned a healthy prod verdict instead."""
    refusal = kubectl_lib.cluster_refusal("stage", "prod")
    assert refusal and "serves the prod cluster, not stage" in refusal


def test_an_unreadable_node_list_is_flagged():
    """Fails closed: a kubectl that cannot say who its nodes are supports no claim at all."""
    assert "cannot confirm" in kubectl_lib.cluster_refusal("prod", None)


# ── the runner ──────────────────────────────────────────────────────────────────────────────


def test_a_call_naming_the_served_cluster_runs(cluster):
    cluster.stdout = '{"items": []}'
    assert cluster.kubectl_json("prod", "get", "pods") == {"items": []}
    assert cluster.argvs[-1] == [
        "/usr/local/bin/kubectl",
        "--kubeconfig",
        cluster.argvs[-1][2],
        "get",
        "pods",
    ]


def test_a_call_naming_the_other_cluster_is_refused_before_it_runs(cluster):
    with pytest.raises(kubectl_lib.WrongCluster, match="not stage"):
        cluster.kubectl("stage", "get", "pods")
    # Only the identity read ran; the refused call never reached the runner.
    assert [argv[-4:] for argv in cluster.argvs] == [["get", "nodes", "-o", "json"]]


def test_an_unrecognised_node_list_is_refused_for_every_cluster(cluster):
    cluster.nodes = {"items": [{"metadata": {"name": "elsewhere"}}]}
    for name in kubectl_lib.CLUSTER_NODES:
        with pytest.raises(kubectl_lib.WrongCluster, match="cannot confirm"):
            cluster.kubectl(name, "get", "pods")


def test_a_cluster_name_the_table_lacks_is_a_caller_bug(cluster):
    with pytest.raises(ValueError, match="unknown cluster"):
        cluster.kubectl("production", "get", "pods")


def test_the_identity_read_happens_once_per_process(cluster):
    for _ in range(3):
        cluster.kubectl("prod", "get", "pods")
    node_reads = [a for a in cluster.argvs if a[-4:] == ["get", "nodes", "-o", "json"]]
    assert len(node_reads) == 1
    assert len(cluster.argvs) == 4


def test_a_missing_binary_or_kubeconfig_is_a_setup_error():
    no_binary = kubectl_lib.Tools(find_tool=lambda name: None)
    with pytest.raises(kubectl_lib.MissingKubectl, match="kubectl not found"):
        kubectl_lib.kubectl("prod", "get", "pods", tools=no_binary)
    no_kubeconfig = kubectl_lib.Tools(
        find_tool=lambda name: "/usr/local/bin/kubectl", find_kubeconfig=lambda: None
    )
    with pytest.raises(kubectl_lib.MissingKubectl, match="no readable kubeconfig"):
        kubectl_lib.kubectl("prod", "get", "pods", tools=no_kubeconfig)


def test_privileged_uses_the_k3s_kubeconfig_and_an_unprivileged_identity_read(cluster):
    """`sudo` reads root's kubeconfig; the discovered one is the read-only SA, which cannot
    exec. The identity read stays unprivileged — which cluster the API server is does not
    depend on who is asking, and an extra sudo prompt per process would.
    """
    cluster.kubectl("prod", "exec", "deploy/x", "--", "true", privileged=True)
    identity, call = cluster.argvs
    assert identity[0] != "sudo"
    assert call[:4] == [
        "sudo",
        "/usr/local/bin/kubectl",
        "--kubeconfig",
        str(kubectl_lib.K3S_KUBECONFIG),
    ]


def test_json_is_none_on_a_failed_or_unparseable_call(cluster):
    cluster.returncode = 1
    assert cluster.kubectl_json("prod", "get", "pods") is None
    cluster.returncode, cluster.stdout = 0, "not json"
    assert cluster.kubectl_json("prod", "get", "pods") is None


def test_check_raises_with_stderr_attached(cluster):
    cluster.returncode = 1
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        cluster.kubectl("prod", "get", "pods", check=True)
    assert excinfo.value.stderr == "boom"


# ── discovery ───────────────────────────────────────────────────────────────────────────────


def test_find_tool_looks_beyond_an_impoverished_path(monkeypatch):
    """cron's PATH omits /usr/local/bin, where kubectl lives on the cluster nodes."""
    binary = Path("/usr/local/bin/kubectl")
    if not binary.exists():
        pytest.skip("kubectl not installed at the path this guards")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert kubectl_lib.find_tool("kubectl") == str(binary)


def test_find_tool_returns_none_for_a_genuinely_absent_binary():
    assert kubectl_lib.find_tool("definitely-not-a-real-binary") is None


def test_find_kubeconfig_prefers_an_explicit_kubeconfig_env(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit"
    explicit.write_text("")
    monkeypatch.setenv("KUBECONFIG", str(explicit))
    assert kubectl_lib.find_kubeconfig() == explicit


def test_find_kubeconfig_reads_kubeconfig_as_a_path_list(monkeypatch, tmp_path):
    missing, second = tmp_path / "missing", tmp_path / "second"
    second.write_text("")
    monkeypatch.setenv("KUBECONFIG", f"{missing}:{second}")
    assert kubectl_lib.find_kubeconfig() == second


def test_find_kubeconfig_skips_an_unreadable_candidate(monkeypatch, tmp_path):
    """The actual cron failure: the k3s default exists but is root-only 0640."""
    readable = tmp_path / "readable"
    readable.write_text("")
    monkeypatch.delenv("KUBECONFIG", raising=False)
    unreadable = tmp_path / "root-only"
    unreadable.write_text("")
    unreadable.chmod(0o000)
    assert kubectl_lib.find_kubeconfig(user=unreadable, k3s=readable) == readable


# ── the census ──────────────────────────────────────────────────────────────────────────────

SCRIPTS = REPO / "scripts"

# Callers that reach the cluster and must appear in the census below. A census that returns
# nothing passes `all(...)` vacuously; naming known consumers is what makes it fail when the
# scan stops matching (the pattern `test_probe_boundaries.py` sets).
KNOWN_CALLERS = frozenset(
    {
        "diagnostics/probe_lib/health.py",
        "diagnostics/probe_lib/vip_placement.py",
        "infra_map/live.py",
        "dev/measure_rollout_gap.py",
        "grafana/export_grafana_dashboards.py",
    }
)


def _non_test_scripts() -> list[Path]:
    return [
        path
        for path in sorted(SCRIPTS.rglob("*.py"))
        if "tests" not in path.relative_to(SCRIPTS).parts
        and path.relative_to(SCRIPTS).as_posix() != "lib/kubectl.py"
    ]


def test_no_script_outside_the_invoker_builds_a_kubectl_argv():
    """The acceptance check from #2062, as a guard rather than a one-off grep.

    A module that spells `"kubectl"` as a string literal is building its own
    argv, which is the thing this module exists to make unnecessary — and any such call
    runs against an unnamed cluster. `"k3s"` is not matched: it is also a role name in a
    path (`roles/setup/k3s`), and `k3s kubectl` cannot be spelled without the second word.
    """
    offenders = [
        path.relative_to(SCRIPTS).as_posix()
        for path in _non_test_scripts()
        if '"kubectl"' in path.read_text()
    ]
    assert offenders == [], f"kubectl argv built outside lib/kubectl.py: {offenders}"


def test_every_known_caller_imports_the_invoker():
    """Non-vacuity for the guard above: the callers it replaced still go through here."""
    importers = {
        path.relative_to(SCRIPTS).as_posix()
        for path in _non_test_scripts()
        if re.search(r"^from lib(\.kubectl| import kubectl)", path.read_text(), re.M)
    }
    missing = KNOWN_CALLERS - importers
    assert not missing, (
        f"{sorted(missing)} no longer import lib.kubectl; census sees {sorted(importers)}"
    )


def test_every_named_node_is_a_host_in_the_inventory():
    """Non-vacuity against ground truth: a renamed host must fail here, not silently.

    `CLUSTER_NODES` is a constant because probe.py cannot parse Ansible at runtime. Nothing
    but this test notices it drifting from the inventory the names came from.
    """
    hosts = (REPO / "ansible" / "inventory" / "hosts.ini").read_text()
    declared = {
        line.split()[0]
        for line in hosts.splitlines()
        if re.match(r"^[a-z0-9][a-z0-9-]*\s", line)
    }
    named = set().union(*kubectl_lib.CLUSTER_NODES.values())
    assert named, "CLUSTER_NODES is empty — every cluster check would pass vacuously"
    assert named <= declared, (
        f"CLUSTER_NODES names {sorted(named - declared)}, which is in no inventory host line; "
        "the identity check would call the real cluster unknown and refuse every call"
    )
