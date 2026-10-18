"""registry's inert verify Jobs: the push proves a round trip, the isolation leg fails on any answer.

The script tests run `registry_verify.main` against local ports: a small in-memory registry
stands in for the push store, an HTTP server stands in for a port that answers, and a port
nothing listens on stands in for one the fence refuses. The render tests copy the registry
role, move `templates/verify-inert/` to `templates/verify/` as the flip PR will, and render
the copy, so the Jobs are checked as Flux will receive them while the tree's own render still
emits no `registry-verify`. The plan is docs/target-architecture/deploy/d5-bulk-move.md,
*A verify conversion lands as inert code*.

Run: uv run pytest ansible/roles/k8s/registry/tests/test_registry_verify.py
"""

import hashlib
import re
import shutil
import socket
import threading
from typing import cast
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

import registry_verify
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "registry"
# A path into a role's tree, as any lookup names it; group 1 is the role.
ROLE_PATH = re.compile(r"/roles/k8s/([^/'\"]+)/")


class FakeRegistry(ThreadingHTTPServer):
    """An in-memory registry speaking the part of the distribution API the push uses.

    Attributes:
        blobs: The stored blobs, by digest.
        manifests: The stored manifests, by `<repo>:<tag>`.
        uploads: How many blob uploads completed.
        corrupt: When set, a manifest GET serves these bytes instead of the stored ones.
    """

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, bytes] = {}
        self.uploads = 0
        self.corrupt: bytes | None = None
        super().__init__(("127.0.0.1", 0), _RegistryHandler)


class _RegistryHandler(BaseHTTPRequestHandler):
    @property
    def registry(self) -> FakeRegistry:
        return cast(FakeRegistry, self.server)

    def _send(self, status, headers=None, body=b""):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_HEAD(self):
        digest = self.path.rsplit("/", 1)[-1]
        self._send(200 if digest in self.registry.blobs else 404)

    def do_POST(self):
        self._body()
        repo = self.path.split("/")[2]
        self._send(202, {"Location": f"/v2/{repo}/blobs/uploads/abc?state=x"})

    def do_PUT(self):
        url = urlsplit(self.path)
        body = self._body()
        if "/blobs/uploads/" in url.path:
            digest = parse_qs(url.query)["digest"][0]
            if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
                self._send(400)
                return
            self.registry.blobs[digest] = body
            self.registry.uploads += 1
            self._send(201)
            return
        _, _, repo, _, tag = url.path.split("/")
        self.registry.manifests[f"{repo}:{tag}"] = body
        self._send(201)

    def do_GET(self):
        _, _, repo, _, tag = self.path.split("/")
        body = self.registry.manifests.get(f"{repo}:{tag}")
        if body is None:
            self._send(404)
            return
        served = self.registry.corrupt or body
        digest = "sha256:" + hashlib.sha256(served).hexdigest()
        self._send(200, {"Docker-Content-Digest": digest}, served)

    def log_message(self, format, *args):
        pass


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def registry():
    server = FakeRegistry()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def closed_url() -> str:
    """A URL on a local port nothing listens on, so a connect to it is refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v2/"


def _push_env(url) -> dict[str, str]:
    return {
        "CHECK": "push",
        "REGISTRY_URL": url,
        "REPO": "registry-selftest",
        "TAG": "verify",
        "REGISTRY_RENDER_DIGEST8": "abcd1234",
    }


def test_a_push_round_trip_passes(registry, capsys):
    assert registry_verify.main([], _push_env(_url(registry))) == 0
    _, manifest = registry_verify.image()
    assert registry.manifests == {"registry-selftest:verify": manifest}
    assert len(registry.blobs) == 2
    assert "pushed and read back registry-selftest:verify" in capsys.readouterr().out


def test_a_second_push_uploads_nothing(registry, capsys):
    """Fixed bytes mean the store gains nothing however often the Job runs."""
    assert registry_verify.main([], _push_env(_url(registry))) == 0
    assert registry_verify.main([], _push_env(_url(registry))) == 0
    assert registry.uploads == 2
    assert capsys.readouterr().out.count("already present") == 2


def test_a_tag_that_reads_back_as_another_digest_fails_the_job(registry, capsys):
    registry.corrupt = b'{"schemaVersion":2}'
    assert registry_verify.main([], _push_env(_url(registry))) == 1
    assert "did not store what was pushed" in capsys.readouterr().err


def test_an_unreachable_registry_fails_the_push(closed_url):
    url = closed_url.removesuffix("/v2/")
    with pytest.raises(
        registry_verify.Failed, match="still admits app=registry-selftest-push"
    ):
        registry_verify.run(_push_env(url), push_retry_seconds=0)


def _isolation_env(*urls) -> dict[str, str]:
    return {"CHECK": "isolation", "ISOLATED_URLS": " ".join(urls)}


def test_refused_ports_pass_the_isolation_check(closed_url, capsys):
    assert registry_verify.main([], _isolation_env(closed_url, closed_url)) == 0
    assert capsys.readouterr().out.count("refused, as intended") == 2


@pytest.mark.parametrize("answering", [0, 1], ids=["push-port", "mirror-port"])
def test_an_answer_on_either_port_fails_the_isolation_check(
    registry, closed_url, answering, capsys
):
    """The fake registry answers `/v2/` with 404, an error status, which still is an answer."""
    urls = [closed_url, closed_url]
    urls[answering] = f"{_url(registry)}/v2/"
    assert registry_verify.main([], _isolation_env(*urls)) == 1
    assert "registry accepted the connection" in capsys.readouterr().err


def test_a_leg_that_fails_on_dns_fails_the_isolation_check(capsys):
    """A lookup failure is no refusal; it stands in for the old probe's `getent` control."""
    env = _isolation_env("http://registry.invalid:5000/v2/")
    assert registry_verify.main([], env) == 1
    assert "no refusal from http://registry.invalid:5000/v2/" in capsys.readouterr().err


@pytest.mark.parametrize(
    "env",
    [{"CHECK": "isolation", "ISOLATED_URLS": " "}, {"CHECK": "pull"}],
    ids=["no-urls", "unknown-check"],
)
def test_a_job_that_would_check_nothing_fails(env):
    with pytest.raises(registry_verify.Failed):
        registry_verify.run(env)


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _flipped_render(stubs, roles, edit_policy=False) -> cluster.RenderedTag:
    shutil.copytree(K8S_ROLES / TAG, roles / TAG)
    templates = roles / TAG / "templates"
    (templates / "verify-inert").rename(templates / "verify")
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
    return _flipped_render(stubs, tmp_path_factory.mktemp("roles"))


@pytest.fixture(scope="module")
def flipped(flipped_tag) -> list[dict]:
    return _docs(flipped_tag.verify)


def _job(docs: list[dict], check: str) -> dict:
    (job,) = [
        doc
        for doc in docs
        if doc["kind"] == "Job"
        and doc["metadata"]["name"].startswith(f"registry-verify-{check}-")
    ]
    return job


def _env_of(job: dict) -> dict[str, str]:
    (container,) = job["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e["value"] for e in container["env"]}


def _label(job: dict) -> str:
    return job["spec"]["template"]["metadata"]["labels"]["app"]


@pytest.fixture(scope="module")
def policy(flipped_tag) -> dict:
    (policy,) = [
        doc
        for doc in _docs(flipped_tag.files)
        if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "registry"
    ]
    return policy


def test_the_tree_renders_no_registry_verify_while_ansible_owns_it(stubs):
    assert cluster.render_tag(TAG, stubs).verify == {}


def test_the_flipped_jobs_pass_every_job_census_row(flipped):
    assert sorted(doc["kind"] for doc in flipped) == [
        "ConfigMap",
        "Job",
        "Job",
        "ServiceAccount",
    ]
    assert problems({f"{TAG}-verify": flipped}) == []


def test_the_census_fails_a_flipped_job_once_its_spec_moves(flipped):
    """The red case for the name row: an edited spec under the rendered name is refused."""
    docs = [dict(doc) for doc in flipped]
    job = _job(docs, "push")
    job["spec"] = {**job["spec"], "backoffLimit": 1}
    found = problems({f"{TAG}-verify": docs})
    assert any(line.startswith("name: ") for line in found)
    assert any(line.startswith("backoff: ") for line in found)


def _may_admit(peer: dict, label: str) -> bool:
    """Whether `peer` may admit a pod whose only label is `app: <label>`, failing closed.

    The Jobs' pods carry that one label. An `ipBlock` wider than one address (the pod CIDR
    holds every pod), an empty selector and a `namespaceSelector` count as admitting. A `/32`
    is one of the fence's node grants, the gateway address containerd's pulls arrive from,
    which no pod holds. A `podSelector` admits when the pod's labels meet every `matchLabels`
    pair and every `In` or `NotIn` expression; any other operator counts as admitting.
    """
    if "ipBlock" in peer:
        return not peer["ipBlock"]["cidr"].endswith("/32")
    selector = peer.get("podSelector")
    if selector is None:
        return True
    labels = {"app": label}
    if not (selector.get("matchLabels") or {}).items() <= labels.items():
        return False
    for expression in selector.get("matchExpressions") or []:
        value = labels.get(expression["key"])
        if expression["operator"] == "In" and value not in expression["values"]:
            return False
        if expression["operator"] == "NotIn" and value in expression["values"]:
            return False
    return True


def _may_admit_on(policy: dict, label: str) -> list[int]:
    """The ports on which `policy` may admit a pod labelled `app: <label>`, failing closed."""
    return sorted(
        int(port["port"])
        for rule in policy["spec"]["ingress"]
        if "from" not in rule or any(_may_admit(peer, label) for peer in rule["from"])
        for port in rule["ports"]
    )


def test_the_fence_admits_the_push_job_on_the_push_port(flipped, policy):
    """The caller census: the push Job keeps the selftest's label, and with it its admission."""
    job = _job(flipped, "push")
    assert _label(job) == "registry-selftest-push"
    url = urlsplit(_env_of(job)["REGISTRY_URL"])
    assert url.hostname == "registry.homelab.svc.cluster.local"
    assert url.port == 5000
    assert _may_admit_on(policy, _label(job)) == [5000]
    # The admission is by label: a pod with any other `app` gets nothing.
    assert _may_admit_on(policy, "registry-not-admitted") == []


@pytest.fixture(scope="module")
def mirror_policy(flipped_tag) -> dict:
    (policy,) = [
        doc
        for doc in _docs(flipped_tag.files)
        if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "ghcr-mirror"
    ]
    return policy


def test_the_isolation_job_dials_every_port_the_fence_covers(
    flipped, policy, mirror_policy
):
    """A port the Job skipped would be a fence hole nothing checks.

    The Service fronts both pods, so the fence is the push store's policy and the mirror's.
    """
    fenced = {
        int(port["port"])
        for fence in (policy, mirror_policy)
        for rule in fence["spec"]["ingress"]
        for port in rule["ports"]
    }
    urls = [
        urlsplit(u)
        for u in _env_of(_job(flipped, "isolation"))["ISOLATED_URLS"].split()
    ]
    assert {u.hostname for u in urls} == {"registry.homelab.svc.cluster.local"}
    assert {u.port for u in urls} == fenced == {5000, 5002}


def test_the_fence_does_not_admit_the_isolation_job(flipped, policy, mirror_policy):
    """Admitting `registry-verify` would open the hole the Job proves closed."""
    label = _label(_job(flipped, "isolation"))
    assert label == "registry-verify"
    assert _may_admit_on(policy, label) == []
    assert _may_admit_on(mirror_policy, label) == []


@pytest.mark.parametrize(
    "peer",
    [
        {"podSelector": {"matchLabels": {"app": "registry-verify"}}},
        {"podSelector": {}},
        {"ipBlock": {"cidr": "10.42.0.0/16"}},
        {
            "podSelector": {
                "matchExpressions": [
                    {"key": "app", "operator": "In", "values": ["registry-verify"]}
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
def test_a_fence_that_admits_the_isolation_job_is_flagged(peer):
    """The red cases: each peer shape that can admit `registry-verify` shows up as its port."""
    policy = {"spec": {"ingress": [{"from": [peer], "ports": [{"port": 5000}]}]}}
    assert _may_admit_on(policy, "registry-verify") == [5000]


def test_the_flipped_jobs_run_the_flux_job_image_without_a_token(flipped):
    flux = yaml_fast.safe_load((K8S_ROLES / "flux/defaults/main.yml").read_text())
    for check in ("push", "isolation"):
        pod = _job(flipped, check)["spec"]["template"]["spec"]
        (container,) = pod["containers"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert container["image"] == flux["flux_job_image"]


def test_a_policy_only_change_renames_both_jobs(stubs, tmp_path):
    """The fence is what the isolation Job tests, so editing it alone makes Flux run it again."""
    renders = [
        _docs(_flipped_render(stubs, tmp_path / str(n), edit_policy=edit).verify)
        for n, edit in enumerate((False, True))
    ]
    for check in ("push", "isolation"):
        names = [_job(docs, check)["metadata"]["name"] for docs in renders]
        assert names[0] != names[1]


def test_the_staged_jobs_read_no_other_roles_file():
    """The deployer redeploys a role when a file it `lookup()`s in another role changes."""
    staged = sorted((K8S_ROLES / TAG / "templates" / "verify-inert").glob("*.j2"))
    assert [p.name for p in staged] == ["job.yaml.j2", "rbac.yaml.j2"]
    assert [
        (path.name, role)
        for path in staged
        for role in ROLE_PATH.findall(path.read_text())
        if role != TAG
    ] == []
