"""observability's verify Job: its script fails an unpinned OTLP hostPort, and its render passes the census.

The script tests run `observability_verify.main` with a stand-in for the API read, and one test
serves an HTTP error from a local port to reach `read_daemonset` itself. The render tests copy
the role, move `templates/verify-inert/` to `templates/verify/` as the flip PR will, and render
the copy through `render_tag`, so the Job is checked as Flux will receive it in
`observability-verify` while the tree's own render still emits none. The plan is
docs/target-architecture/deploy/d5-bulk-move.md, *A verify conversion lands as inert code*.

Run: uv run pytest ansible/roles/k8s/observability/tests/test_observability_verify.py
"""

import copy
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import observability_verify
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "observability"
# A path into a role's tree, as any lookup names it; group 1 is the role.
ROLE_PATH = re.compile(r"/roles/k8s/([^/'\"]+)/")
ENV = {
    "OTLP_NAMESPACE": "observability",
    "OTLP_DAEMONSET": "otel-collector",
    "OTLP_HOST_IP": "127.0.0.1",
    "OBSERVABILITY_RENDER_DIGEST8": "abcd1234",
}


def _daemonset(*ports: dict) -> dict:
    return {
        "spec": {
            "template": {
                "spec": {"containers": [{"name": "collector", "ports": list(ports)}]}
            }
        }
    }


def _reader(daemonset: dict):
    def read(namespace: str, name: str) -> dict:
        assert (namespace, name) == ("observability", "otel-collector")
        return daemonset

    return read


def test_a_loopback_pinned_receiver_passes(capsys):
    ds = _daemonset(
        {"containerPort": 4317, "hostPort": 4317, "hostIP": "127.0.0.1"},
        {"containerPort": 13133},
    )
    assert observability_verify.main([], ENV, _reader(ds)) == 0
    assert "pinned: collector:4317 on 127.0.0.1" in capsys.readouterr().out


@pytest.mark.parametrize(
    "port",
    [
        {"containerPort": 4317, "hostPort": 4317},
        {"containerPort": 4317, "hostPort": 4317, "hostIP": "0.0.0.0"},
        {"containerPort": 4317, "hostPort": 4317, "hostIP": "10.0.0.5"},
    ],
    ids=["no-host-ip", "every-address", "lan-address"],
)
def test_an_unpinned_receiver_fails_the_job(port, capsys):
    assert observability_verify.main([], ENV, _reader(_daemonset(port))) == 1
    assert "NOT pinned to 127.0.0.1: collector:4317" in capsys.readouterr().err


def test_a_daemonset_with_no_host_port_fails_the_job(capsys):
    """No hostPort leaves the check with no subject, and the host with no receiver."""
    ds = _daemonset({"containerPort": 4317})
    assert observability_verify.main([], ENV, _reader(ds)) == 1
    assert "declares no hostPort" in capsys.readouterr().err


def test_an_api_error_fails_the_read():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(observability_verify.Failed, match="HTTP 403"):
            observability_verify.read_daemonset("observability", "x", "t", base=base)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _flipped_copy(roles: Path, stubs, edit_collector=False) -> cluster.RenderedTag:
    """Render a copy of the role with its Job moved to `templates/verify/`, as the flip PR will."""
    shutil.copytree(K8S_ROLES / TAG, roles / TAG)
    templates = roles / TAG / "templates"
    (templates / "verify-inert").rename(templates / "verify")
    if edit_collector:
        collector = templates / "collector.yaml.j2"
        collector.write_text(collector.read_text() + "# a collector-only edit\n")
    return cluster.render_tag(TAG, stubs, roles=roles)


def _docs(texts: dict[str, str]) -> list[dict]:
    return [
        doc
        for name, text in sorted(texts.items())
        if name != "kustomization.yaml"
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


@pytest.fixture(scope="module")
def flipped_tag(stubs, tmp_path_factory) -> cluster.RenderedTag:
    return _flipped_copy(tmp_path_factory.mktemp("roles"), stubs)


@pytest.fixture(scope="module")
def flipped(flipped_tag) -> list[dict]:
    return _docs(flipped_tag.verify)


def _job(docs: list[dict]) -> dict:
    (job,) = [doc for doc in docs if doc["kind"] == "Job"]
    return job


def _env_of(job: dict) -> dict[str, str]:
    (container,) = job["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e["value"] for e in container["env"]}


def _collector(rendered: cluster.RenderedTag) -> dict:
    (ds,) = [
        doc
        for doc in _docs(rendered.files)
        if doc["kind"] == "DaemonSet" and doc["metadata"]["name"] == "otel-collector"
    ]
    return ds


def test_the_tree_renders_no_verify_job_while_ansible_owns_observability(stubs):
    """Inert: the Ansible readback still owns the check until the flip moves the directory."""
    assert cluster.render_tag(TAG, stubs).verify == {}


def test_the_flipped_job_passes_every_job_census_row(flipped):
    assert sorted(doc["kind"] for doc in flipped) == [
        "ConfigMap",
        "Job",
        "Role",
        "RoleBinding",
        "ServiceAccount",
    ]
    assert problems({f"{TAG}-verify": flipped}) == []


def test_the_census_fails_the_flipped_job_once_its_spec_moves(flipped):
    """The red case for the name row: an edited spec under the rendered name is refused."""
    docs = [dict(doc) for doc in flipped]
    job = _job(docs)
    job["spec"] = {**job["spec"], "backoffLimit": 1}
    found = problems({f"{TAG}-verify": docs})
    assert any(line.startswith("name: ") for line in found)
    assert any(line.startswith("backoff: ") for line in found)


def test_the_flipped_job_passes_on_the_collector_the_tree_renders(flipped, flipped_tag):
    """The Job's inputs name the rendered DaemonSet, and its check passes on that render."""
    env = _env_of(_job(flipped))
    ds = _collector(flipped_tag)
    assert (env["OTLP_NAMESPACE"], env["OTLP_DAEMONSET"]) == (
        ds["metadata"]["namespace"],
        ds["metadata"]["name"],
    )
    assert observability_verify.check(ds, env["OTLP_HOST_IP"])


def test_the_flipped_job_fails_on_the_collector_with_its_pin_removed(
    flipped, flipped_tag
):
    """The red case on the real render: the same DaemonSet with no `hostIP` fails the check."""
    ds = copy.deepcopy(_collector(flipped_tag))
    for container in ds["spec"]["template"]["spec"]["containers"]:
        for port in container.get("ports") or []:
            port.pop("hostIP", None)
    with pytest.raises(observability_verify.Failed, match="NOT pinned"):
        observability_verify.check(ds, _env_of(_job(flipped))["OTLP_HOST_IP"])


def test_the_role_grants_get_on_the_collector_alone(flipped):
    (role,) = [doc for doc in flipped if doc["kind"] == "Role"]
    assert role["rules"] == [
        {
            "apiGroups": ["apps"],
            "resources": ["daemonsets"],
            "resourceNames": ["otel-collector"],
            "verbs": ["get"],
        }
    ]
    (binding,) = [doc for doc in flipped if doc["kind"] == "RoleBinding"]
    pod = _job(flipped)["spec"]["template"]["spec"]
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": pod["serviceAccountName"],
            "namespace": role["metadata"]["namespace"],
        }
    ]


def test_the_flipped_job_runs_the_flux_job_image(flipped):
    pod = _job(flipped)["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    flux = yaml_fast.safe_load((K8S_ROLES / "flux/defaults/main.yml").read_text())
    assert container["image"] == flux["flux_job_image"]


def test_a_collector_only_change_renames_the_job(stubs, tmp_path):
    """The DaemonSet is what the Job reads, so editing it alone makes Flux run the Job again."""
    names = [
        _job(
            _docs(_flipped_copy(tmp_path / str(n), stubs, edit_collector=edit).verify)
        )["metadata"]["name"]
        for n, edit in enumerate((False, True))
    ]
    assert names[0] != names[1]


def test_the_verify_job_reads_no_other_roles_file():
    """The helper and the image come from `render_tag`, never a lookup into another role.

    The deployer redeploys a role when a file it `lookup()`s in another role changes, and its
    scan reads `verify/` too. tdarr's test has the red case for this scan.
    """
    staged = sorted((K8S_ROLES / TAG / "templates" / "verify-inert").glob("*.j2"))
    assert [p.name for p in staged] == ["job.yaml.j2", "rbac.yaml.j2"]
    assert [
        (path.name, role)
        for path in staged
        for role in ROLE_PATH.findall(path.read_text())
        if role != TAG
    ] == []
