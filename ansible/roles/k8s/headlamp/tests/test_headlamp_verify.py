"""headlamp's verify Job: its script fails on any answer from headlamp, and its render passes the census.

The script tests run `headlamp_verify.main` against local ports: an HTTP server stands in for a
port that answers, and a port nothing listens on stands in for one the fence refuses. The render
tests render the tree through `render_tag`, so the Job is checked as Flux receives it in
`headlamp-verify`, and render a copy with the policy edited for the rename test. The plan is
docs/target-architecture/deploy/d5-bulk-move.md, *A verify conversion lands as inert code*.

Run: uv run pytest ansible/roles/k8s/headlamp/tests/test_headlamp_verify.py
"""

import re
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import headlamp_verify
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "headlamp"
# A path into a role's tree, as any lookup names it; group 1 is the role.
ROLE_PATH = re.compile(r"/roles/k8s/([^/'\"]+)/")


def _serve(status=200):
    """A local server that answers every GET with `status`."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/"


@pytest.fixture
def closed_url():
    """A URL on a local port nothing listens on, so a connect to it is refused.

    The socket stays bound, without listening, until the test ends. Linux refuses a connect to
    a bound port that is not listening, and no other socket can bind it meanwhile. A released
    port can be handed to a stand-in server, in this test or in another xdist worker, and the
    leg then reads an answer instead of a refusal (#6480).
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/"


@pytest.fixture
def control():
    # traefik answers a request no route matches with 404, which still proves it was reached.
    server = _serve(404)
    yield server
    server.shutdown()
    server.server_close()


def _env(control_url, headlamp) -> dict[str, str]:
    return {
        "CONTROL_URL": control_url,
        "HEADLAMP_URL": headlamp,
        "HEADLAMP_RENDER_DIGEST8": "abcd1234",
    }


def test_a_fenced_headlamp_passes(control, closed_url, capsys):
    assert headlamp_verify.main([], _env(_url(control), closed_url)) == 0
    out = capsys.readouterr().out
    assert f"control ok: {_url(control)} answered" in out
    assert f"isolated: {closed_url} refused, as intended" in out


@pytest.mark.parametrize("status", [200, 401], ids=["ok", "error-status"])
def test_an_answer_from_headlamp_fails_the_job(control, status, capsys):
    """Any answer is a connection headlamp accepted, an HTTP error status included."""
    server = _serve(status)
    try:
        assert headlamp_verify.main([], _env(_url(control), _url(server))) == 1
    finally:
        server.shutdown()
        server.server_close()
    err = capsys.readouterr().err
    assert _url(server) in err
    assert "a pod that is not traefik" in err


def test_a_leg_that_fails_on_dns_fails_the_job(control, capsys):
    """A lookup failure is no refusal, so it proves nothing about the policy."""
    env = _env(_url(control), "http://headlamp.invalid:4466/")
    assert headlamp_verify.main([], env) == 1
    assert "no refusal from http://headlamp.invalid:4466/" in capsys.readouterr().err


def test_a_refused_control_fails_the_job(closed_url):
    """Without the control, the refusal below it could be a pod with no network."""
    with pytest.raises(headlamp_verify.Failed, match="CONTROL FAILED"):
        headlamp_verify.run(_env(closed_url, closed_url), control_retry_seconds=0)


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _flipped_copy(roles: Path, stubs, edit_policy=False) -> cluster.RenderedTag:
    """Render a copy of the role, with the policy edited when asked."""
    shutil.copytree(K8S_ROLES / TAG, roles / TAG)
    templates = roles / TAG / "templates"
    if edit_policy:
        policy = templates / "networkpolicy.yaml.j2"
        policy.write_text(policy.read_text() + "# a policy-only edit\n")
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


def _policy(docs: list[dict], name: str) -> dict:
    (policy,) = [
        doc
        for doc in docs
        if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == name
    ]
    return policy


def test_the_tree_renders_headlamp_verify(stubs):
    """The renderer emits the Job that replaced the Ansible isolation probe."""
    assert cluster.render_tag(TAG, stubs).verify != {}


def test_the_flipped_job_passes_every_job_census_row(flipped):
    assert sorted(doc["kind"] for doc in flipped) == [
        "ConfigMap",
        "Job",
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


def test_the_flipped_job_dials_the_port_headlamps_fence_covers(flipped, flipped_tag):
    """The isolation leg dials the port the fence has a rule for, or a refusal proves nothing."""
    policy = _policy(_docs(flipped_tag.files), "headlamp")
    fenced = {
        int(port["port"])
        for rule in policy["spec"]["ingress"]
        for port in rule["ports"]
    }
    url = urlsplit(_env_of(_job(flipped))["HEADLAMP_URL"])
    assert url.hostname == "headlamp.homelab.svc.cluster.local"
    assert {url.port} == fenced == {4466}


def _may_admit(peer: dict, label: str) -> bool:
    """Whether `peer` may admit a pod labelled `app: <label>`, failing closed.

    The only peer that provably excludes the pod is a `podSelector` whose `matchLabels` name
    another `app`, with no `matchExpressions`. An empty selector, an `ipBlock` (the pod CIDR
    holds every pod) and a `matchExpressions` selector all count as admitting.
    """
    selector = peer.get("podSelector")
    if selector is None or selector.get("matchExpressions"):
        return True
    app = (selector.get("matchLabels") or {}).get("app")
    return app is None or app == label


def _may_admit_on(policy: dict, label: str) -> list[int]:
    """The ports on which `policy` may admit a pod labelled `app: <label>`, failing closed."""
    return [
        int(port["port"])
        for rule in policy["spec"]["ingress"]
        if "from" not in rule or any(_may_admit(peer, label) for peer in rule["from"])
        for port in rule.get("ports") or []
    ]


def test_headlamps_fence_does_not_admit_the_verify_pod(flipped, flipped_tag):
    """The caller census: admitting `headlamp-verify` would open the hole the Job proves closed."""
    label = _job(flipped)["spec"]["template"]["metadata"]["labels"]["app"]
    assert label == "headlamp-verify"
    assert _may_admit_on(_policy(_docs(flipped_tag.files), "headlamp"), label) == []


@pytest.mark.parametrize(
    "peer",
    [
        {"podSelector": {"matchLabels": {"app": "headlamp-verify"}}},
        {"podSelector": {}},
        {"ipBlock": {"cidr": "10.42.0.0/16"}},
        {
            "podSelector": {
                "matchExpressions": [
                    {"key": "app", "operator": "In", "values": ["headlamp-verify"]}
                ]
            }
        },
        {"namespaceSelector": {}},
    ],
    ids=[
        "match-labels",
        "empty-selector",
        "ip-block",
        "match-expressions",
        "namespace",
    ],
)
def test_a_headlamp_fence_that_admits_the_verify_pod_is_flagged(peer):
    """The red cases: each peer shape that can admit `headlamp-verify` shows up as its port."""
    policy = {"spec": {"ingress": [{"from": [peer], "ports": [{"port": 4466}]}]}}
    assert _may_admit_on(policy, "headlamp-verify") == [4466]


def test_traefiks_policy_opens_the_control_port_to_the_verify_pod(flipped, stubs):
    """The control dials traefik:80, which its Service maps to a port its policy opens to any pod.

    The policy is netpol-baseline's, which renders it for every pod labelled `app: traefik`. A
    rule there that admits the control only through a `from` list would refuse the Job's pod.
    """
    url = urlsplit(_env_of(_job(flipped))["CONTROL_URL"])
    assert (url.hostname, url.port) == ("traefik.homelab.svc.cluster.local", 80)
    traefik = _docs(cluster.render_tag("traefik", stubs).files)
    (service,) = [
        doc
        for doc in traefik
        if doc["kind"] == "Service" and doc["metadata"]["name"] == "traefik"
    ]
    (target,) = [p["targetPort"] for p in service["spec"]["ports"] if p["port"] == 80]
    # A named targetPort resolves through the traefik pod's container ports.
    named = {
        port["name"]: port["containerPort"]
        for doc in traefik
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "traefik"
        for container in doc["spec"]["template"]["spec"]["containers"]
        for port in container.get("ports") or []
        if "name" in port
    }
    target = named.get(target, target)
    policy = _policy(
        _docs(cluster.render_tag("netpol-baseline", stubs).files), "traefik"
    )
    open_to_all = [
        int(port["port"])
        for rule in policy["spec"]["ingress"]
        if "from" not in rule
        for port in rule.get("ports") or []
    ]
    assert target in open_to_all


def test_the_flipped_job_runs_the_flux_job_image_without_a_token(flipped):
    pod = _job(flipped)["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    flux = yaml_fast.safe_load((K8S_ROLES / "flux/defaults/main.yml").read_text())
    assert container["image"] == flux["flux_job_image"]


def test_a_headlamp_policy_only_change_renames_the_job(stubs, tmp_path):
    """The policy is what the Job tests, so editing it alone makes Flux run the Job again."""
    names = [
        _job(_docs(_flipped_copy(tmp_path / str(n), stubs, edit_policy=edit).verify))[
            "metadata"
        ]["name"]
        for n, edit in enumerate((False, True))
    ]
    assert names[0] != names[1]


def test_the_verify_job_reads_no_other_roles_file():
    """The helper and the image come from `render_tag`, never a lookup into another role.

    The deployer redeploys a role when a file it `lookup()`s in another role changes, and its
    scan reads `verify/` too. tdarr's test has the red case for this scan.
    """
    staged = sorted((K8S_ROLES / TAG / "templates" / "verify").glob("*.j2"))
    assert [p.name for p in staged] == ["job.yaml.j2", "rbac.yaml.j2"]
    assert [
        (path.name, role)
        for path in staged
        for role in ROLE_PATH.findall(path.read_text())
        if role != TAG
    ] == []
