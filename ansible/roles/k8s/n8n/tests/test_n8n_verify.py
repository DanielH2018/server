"""n8n's verify Job: its script fails on any answer from n8n, and its render passes the census.

The script tests run `n8n_verify.main` against local ports: an HTTP server stands in for a port
that answers, and a port nothing listens on stands in for one the fence refuses. The render
tests render a copy of the n8n role, so one test can edit the policy and leave the tree alone.
The plan is docs/target-architecture/deploy/d5-bulk-move.md.

Run: uv run pytest ansible/roles/k8s/n8n/tests/test_n8n_verify.py
"""

import re
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from _k8s_render import rendered_docs

import n8n_verify
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "n8n"
# A path into a role's tree, as any lookup names it; group 1 is the role.
ROLE_PATH = re.compile(r"/roles/k8s/([^/'\"]+)/")


def _serve(status=200, location=None):
    """A local server that answers every GET with `status`, and `location` if given."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            if location:
                self.send_header("Location", location)
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
    port can be handed to the next stand-in server, in this test or in another xdist worker,
    and the leg then reads an answer instead of a refusal (#6480).
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/"


@pytest.fixture
def control():
    server = _serve()
    yield server
    server.shutdown()
    server.server_close()


def _env(control_url, web, broker) -> dict[str, str]:
    return {
        "CONTROL_URL": control_url,
        "N8N_WEB_URL": web,
        "N8N_BROKER_URL": broker,
        "N8N_RENDER_DIGEST8": "abcd1234",
    }


def test_a_fenced_n8n_passes(control, closed_url, capsys):
    assert n8n_verify.main([], _env(_url(control), closed_url, closed_url)) == 0
    out = capsys.readouterr().out
    assert f"control ok: {_url(control)} answered" in out
    assert out.count("refused, as intended") == 2


@pytest.mark.parametrize("status", [200, 404], ids=["ok", "error-status"])
@pytest.mark.parametrize("leg", ["N8N_WEB_URL", "N8N_BROKER_URL"])
def test_an_answer_from_either_n8n_port_fails_the_job(
    control, closed_url, leg, status, capsys
):
    """Any answer is a connection n8n accepted, an HTTP error status included."""
    server = _serve(status)
    try:
        env = {**_env(_url(control), closed_url, closed_url), leg: _url(server)}
        assert n8n_verify.main([], env) == 1
    finally:
        server.shutdown()
        server.server_close()
    err = capsys.readouterr().err
    assert _url(server) in err
    assert "n8n-broker is not taking effect" in err


def test_a_leg_that_fails_on_dns_fails_the_job(control, closed_url, capsys):
    """A lookup failure is no refusal, so it proves nothing about the policy."""
    env = _env(_url(control), "http://n8n.invalid:5678/", closed_url)
    assert n8n_verify.main([], env) == 1
    assert "no refusal from http://n8n.invalid:5678/" in capsys.readouterr().err


def test_a_refused_control_fails_the_job(closed_url):
    """Without the control, both refusals below it could be a pod with no network."""
    env = _env(closed_url, closed_url, closed_url)
    with pytest.raises(n8n_verify.Failed, match="CONTROL FAILED"):
        n8n_verify.run(env, control_retry_seconds=0)


def test_a_control_that_redirects_to_an_answer_passes(closed_url, capsys):
    """traefik's web entrypoint redirects to https; urllib follows, and the target answers."""
    target = _serve(404)
    redirect = _serve(301, location=_url(target))
    try:
        env = _env(_url(redirect), closed_url, closed_url)
        assert n8n_verify.main([], env) == 0
    finally:
        for server in (redirect, target):
            server.shutdown()
            server.server_close()
    assert "control ok" in capsys.readouterr().out


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _flip(roles, stubs, edit_policy=False) -> cluster.RenderedTag:
    """Render a copy of the role, its policy edited when `edit_policy` is set."""
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
    return _flip(tmp_path_factory.mktemp("roles"), stubs)


@pytest.fixture(scope="module")
def flipped(flipped_tag) -> list[dict]:
    return _docs(flipped_tag.verify)


def _job(docs: list[dict]) -> dict:
    (job,) = [doc for doc in docs if doc["kind"] == "Job"]
    return job


def _env_of(job: dict) -> dict[str, str]:
    (container,) = job["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e["value"] for e in container["env"]}


def test_the_tree_renders_n8n_verify(stubs, flipped_tag):
    """The flip moved the Job to `templates/verify/`, so the tree's own render emits it."""
    assert cluster.render_tag(TAG, stubs).verify == flipped_tag.verify != {}


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


def test_the_flipped_job_dials_the_two_ports_n8n_broker_fences(flipped, flipped_tag):
    """Each isolation leg dials a port `n8n-broker` has a rule for, or a refusal proves nothing."""
    (policy,) = [
        doc
        for doc in _docs(flipped_tag.files)
        if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "n8n-broker"
    ]
    fenced = {
        int(port["port"])
        for rule in policy["spec"]["ingress"]
        for port in rule["ports"]
    }
    env = _env_of(_job(flipped))
    dialled = {urlsplit(env[leg]).port for leg in ("N8N_WEB_URL", "N8N_BROKER_URL")}
    assert dialled == fenced == {5678, 5679}
    assert {
        urlsplit(env[leg]).hostname for leg in ("N8N_WEB_URL", "N8N_BROKER_URL")
    } == {"n8n.homelab.svc.cluster.local"}


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


def _admits(policy: dict, label: str) -> list[int]:
    """The ports on which `policy` may admit a pod labelled `app: <label>`, failing closed."""
    return [
        int(port["port"])
        for rule in policy["spec"]["ingress"]
        if "from" not in rule or any(_may_admit(peer, label) for peer in rule["from"])
        for port in rule["ports"]
    ]


def test_n8n_broker_admits_the_verify_pod_on_neither_port(flipped, flipped_tag):
    """The caller census: admitting `n8n-verify` would open the hole the Job proves closed."""
    label = _job(flipped)["spec"]["template"]["metadata"]["labels"]["app"]
    assert label == "n8n-verify"
    (policy,) = [
        doc
        for doc in _docs(flipped_tag.files)
        if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "n8n-broker"
    ]
    assert _admits(policy, label) == []


@pytest.mark.parametrize(
    "peer",
    [
        {"podSelector": {"matchLabels": {"app": "n8n-verify"}}},
        {"podSelector": {}},
        {"ipBlock": {"cidr": "10.42.0.0/16"}},
        {
            "podSelector": {
                "matchExpressions": [
                    {"key": "app", "operator": "In", "values": ["n8n-verify"]}
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
def test_a_policy_that_admits_the_verify_pod_is_flagged(peer):
    """The red cases: each peer shape that can admit `n8n-verify` shows up as its port."""
    policy = {
        "spec": {
            "ingress": [
                {"from": [peer], "ports": [{"port": 5678}]},
                {
                    "from": [{"podSelector": {"matchLabels": {"app": "n8n-runners"}}}],
                    "ports": [{"port": 5679}],
                },
            ]
        }
    }
    assert _admits(policy, "n8n-verify") == [5678]


def test_traefiks_fence_admits_the_control_from_any_pod():
    """The control dials traefik:80, which the Service maps to the pod's port 8000."""
    (policy,) = [
        doc
        for role, _tpl, doc in rendered_docs()
        if role == "netpol-baseline"
        and doc.get("kind") == "NetworkPolicy"
        and doc["metadata"]["name"] == "traefik"
    ]
    assert 8000 in _admits(policy, "n8n-verify")


def test_the_flipped_job_runs_the_flux_job_image_without_a_token(flipped):
    pod = _job(flipped)["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    flux = yaml_fast.safe_load((K8S_ROLES / "flux/defaults/main.yml").read_text())
    assert container["image"] == flux["flux_job_image"]
    assert _env_of(_job(flipped))["CONTROL_URL"] == (
        "http://traefik.homelab.svc.cluster.local:80/"
    )


def test_a_policy_only_change_renames_the_job(stubs, tmp_path):
    """The policy is what the Job tests, so editing it alone makes Flux run the Job again."""
    names = [
        _job(_docs(_flip(tmp_path / str(n), stubs, edit_policy=edit).verify))[
            "metadata"
        ]["name"]
        for n, edit in enumerate((False, True))
    ]
    assert names[0] != names[1]


def test_the_staged_job_reads_no_other_roles_file():
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
