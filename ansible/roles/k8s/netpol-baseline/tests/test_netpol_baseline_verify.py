"""netpol-baseline's inert verify Job: its script fails each way the probe did, and its render passes the census.

The script tests run `netpol_baseline_verify` against local ports: an HTTP server stands in for
a port that answers, a raw TCP server for a non-HTTP port that accepts, and a port nothing
listens on for one the fence refuses. The render tests copy the role, move
`templates/verify-inert/` to `templates/verify/` as the flip PR will, and render the copy, so
the Job is checked as Flux will receive it while the tree's own render emits no
`netpol-baseline-verify`. The plan is docs/target-architecture/deploy/d5-bulk-move.md, *A
verify conversion lands as inert code*.

Run: uv run pytest ansible/roles/k8s/netpol-baseline/tests/test_netpol_baseline_verify.py
"""

import json
import re
import socket
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from _k8s_render import rendered_docs
from _netpol_baseline_flipped import STAGED, TAG, write_stubs
from _netpol_baseline_flipped import docs as _docs
from _netpol_baseline_flipped import env_of as _env_of
from _netpol_baseline_flipped import flipped_render as _flipped_render
from _netpol_baseline_flipped import job as _job

import netpol_baseline_verify as verify
from lib.k8s_roles import K8S_ROLES
from renderer.renderer_lib import cluster
from renderer.tests._job_census import problems

# A path into a role's tree, as any lookup names it; group 1 is the role.
ROLE_PATH = re.compile(r"/roles/k8s/([^/'\"]+)/")


def _serve(status=200, location=None):
    """A local server that answers every GET and OPTIONS with `status`, and `location` if given."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_OPTIONS = do_GET

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    return server


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/"


@pytest.fixture
def servers():
    started = []

    def start(status=200, location=None):
        started.append(_serve(status, location))
        return _url(started[-1])

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def _closed_port() -> int:
    """A local port nothing listens on, so a connect to it is refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def closed_url() -> str:
    return f"http://127.0.0.1:{_closed_port()}/"


@pytest.fixture
def mqtt_url():
    """A port that accepts and answers with bytes that are no HTTP, as mosquitto or nut would."""
    listener = socket.create_server(("127.0.0.1", 0))

    def accept():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                conn.recv(1024)
                conn.sendall(b"\x20\x02\x00\x05")

    threading.Thread(target=accept, daemon=True).start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}/"
    listener.close()


def _row(url, expect="refused"):
    return {
        "url": url,
        "expect": expect,
        "policy": "networkpolicy-x.yaml.j2",
        "hint": "HINT",
    }


def _env(servers, closed_url, rows=None, **over) -> dict[str, str]:
    env = {
        "NETPOL_VERIFY_STAGE": "legs",
        "CONTROL_URL": servers(),
        "SENTINEL_URL": servers(),
        "LIVENESS_URL": servers(),
        "TARGETS": json.dumps(rows if rows is not None else [_row(closed_url)]),
        "DNS_NAMES": "localhost",
    }
    return {**env, **over}


def _legs(env):
    """The legs with no retries, and the DNS leg dialling a closed port rather than this host's 53."""
    return verify.legs(
        env, control_retry_seconds=0, sentinel_retry_seconds=0, dns_port=_closed_port()
    )


def test_a_fenced_cluster_passes(servers, closed_url):
    rows = [_row(closed_url), _row(servers(), expect="open")]
    out = "\n".join(_legs(_env(servers, closed_url, rows)))
    for line in (
        "control ok",
        "sentinel control ok",
        "liveness ok",
        "isolated:",
        "open port ok",
        "dns ok: pihole answered localhost",
    ):
        assert line in out


@pytest.mark.parametrize("status", [200, 404], ids=["ok", "error-status"])
def test_an_http_answer_on_a_refused_row_fails(servers, closed_url, status):
    """Any answer is a connection the target accepted, an HTTP error status included."""
    env = _env(servers, closed_url, [_row(servers(status))])
    with pytest.raises(verify.Failed, match=r"POLICY NOT ENFORCED.*HINT"):
        _legs(env)


@pytest.mark.parametrize("status", [301, 302, 307])
def test_a_redirect_to_a_refusing_address_fails_a_refused_row(
    servers, closed_url, status
):
    """The target accepted the connection; where its redirect points proves nothing."""
    env = _env(servers, closed_url, [_row(servers(status, location=closed_url))])
    with pytest.raises(verify.Failed, match="POLICY NOT ENFORCED"):
        _legs(env)


def test_a_non_http_accept_on_a_refused_row_fails(servers, closed_url, mqtt_url):
    """mosquitto and nut answer an HTTP request with a protocol error, which still is an accept."""
    with pytest.raises(verify.Failed, match="POLICY NOT ENFORCED"):
        _legs(_env(servers, closed_url, [_row(mqtt_url)]))


def test_a_refused_row_that_fails_on_dns_fails(servers, closed_url):
    """A lookup failure is no refusal, so it proves nothing about the policy."""
    env = _env(servers, closed_url, [_row("http://sonarr.invalid:8989/")])
    with pytest.raises(verify.Failed, match=r"no refusal from http://sonarr\.invalid"):
        _legs(env)


def test_a_refused_open_row_fails(servers, closed_url):
    env = _env(servers, closed_url, [_row(closed_url, expect="open")])
    with pytest.raises(verify.Failed, match="OPEN PORT FAILED"):
        _legs(env)


@pytest.mark.parametrize(
    ("leg", "message"),
    [("CONTROL_URL", "CONTROL FAILED"), ("SENTINEL_URL", "SENTINEL CONTROL FAILED")],
)
def test_a_refused_control_fails(servers, closed_url, leg, message):
    """Without the controls, every refusal below them could be a pod with no network."""
    with pytest.raises(verify.Failed, match=message):
        _legs(_env(servers, closed_url, **{leg: closed_url}))


def test_a_control_that_fails_on_dns_fails(servers, closed_url):
    env = _env(servers, closed_url, CONTROL_URL="http://traefik.invalid:80/")
    with pytest.raises(verify.Failed, match="CONTROL FAILED"):
        _legs(env)


def test_a_liveness_error_status_fails(servers, closed_url):
    """littlelink must serve the page; an answer from traefik alone is not enough."""
    with pytest.raises(verify.Failed, match="LIVENESS FAILED"):
        _legs(_env(servers, closed_url, LIVENESS_URL=servers(502)))


def test_an_unresolved_dns_name_fails(servers, closed_url):
    env = _env(servers, closed_url, DNS_NAMES="daniel-pi.invalid.,daniel-pi.invalid")
    with pytest.raises(verify.Failed, match="DNS FAILED"):
        _legs(env)


def test_the_lever_off_legs_are_skipped(servers, closed_url):
    """With the homelab lever off the Job leaves both variables empty, and neither leg runs."""
    env = _env(servers, closed_url, LIVENESS_URL="", DNS_NAMES="")
    lines = _legs(env)
    assert len(lines) == 3
    assert not any(line.startswith(("liveness", "dns")) for line in lines)


def test_only_a_resolver_error_counts_as_unresolved():
    assert verify.unresolved(urllib.error.URLError(socket.gaierror(-2, "Name unknown")))
    assert not verify.unresolved(
        urllib.error.URLError(ConnectionRefusedError(111, "x"))
    )
    assert not verify.unresolved(TimeoutError())


def _slices(*endpoints):
    return {"items": [{"endpoints": list(endpoints) or None}]}


GATE_ROWS = [
    {"service": "sonarr", "namespace": "homelab", "plane": "homelab"},
    {"service": "loki", "namespace": "observability", "plane": "observability"},
]


def test_the_gate_passes_when_every_target_is_ready():
    paths = []

    def get(path):
        paths.append(path)
        return _slices({"conditions": {"ready": True}})

    assert verify.gate(GATE_ROWS, get) == [
        "gate ok: 2 probe targets each have a ready endpoint"
    ]
    assert paths[1] == (
        "/apis/discovery.k8s.io/v1/namespaces/observability/endpointslices"
        "?labelSelector=kubernetes.io%2Fservice-name%3Dloki"
    )


@pytest.mark.parametrize(
    "slices",
    [
        _slices(),
        _slices({"conditions": {"ready": False}}),
        _slices({"conditions": {}}),
        {"items": []},
    ],
    ids=["null-endpoints", "not-ready", "no-condition", "no-slice"],
)
def test_the_gate_fails_a_target_with_no_ready_endpoint(slices):
    def get(path):
        if "service-name%3Dloki" in path:
            return slices
        return _slices({"conditions": {"ready": True}})

    with pytest.raises(
        verify.Failed, match=r"no ready endpoint for loki \(observability\)"
    ):
        verify.gate(GATE_ROWS, get)


def test_the_gate_reads_the_api_with_the_pod_token(tmp_path):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth"] = self.headers["Authorization"]
            body = json.dumps(_slices({"conditions": {"ready": True}})).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    try:
        get = verify.api_reader("tok", base=_url(server).rstrip("/"))
        assert verify.gate(GATE_ROWS[:1], get)
    finally:
        server.shutdown()
        server.server_close()
    assert seen["auth"] == "Bearer tok"


def test_an_unknown_stage_fails(capsys):
    assert verify.main([], {"NETPOL_VERIFY_STAGE": "both"}) == 1
    assert "must be 'gate' or 'legs'" in capsys.readouterr().err


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    return write_stubs(tmp_path_factory)


@pytest.fixture(scope="module")
def flipped(stubs, tmp_path_factory) -> list[dict]:
    """The `netpol-baseline-verify` documents a copy of the role renders once its Job leaves `verify-inert/`."""
    return _docs(_flipped_render(stubs, tmp_path_factory.mktemp("roles")))


def test_the_tree_renders_no_netpol_baseline_verify_while_ansible_owns_it(stubs):
    assert cluster.render_tag(TAG, stubs).verify == {}


def test_the_flipped_job_passes_every_job_census_row(flipped):
    kinds = sorted(doc["kind"] for doc in flipped)
    assert kinds == [
        "ConfigMap",
        "Job",
        *["Role"] * 3,
        *["RoleBinding"] * 3,
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


def test_the_flipped_job_carries_the_whole_target_table(flipped):
    """The gate reads every active row; the legs dial each row with an `expect`."""
    pod = _job(flipped)["spec"]["template"]["spec"]
    (gate,), (legs,) = pod["initContainers"], pod["containers"]
    gate_env, legs_env = _env_of(gate), _env_of(legs)
    gated = {row["service"] for row in json.loads(gate_env["GATE_TARGETS"])}
    assert {
        "littlelink",
        "prometheus",
        "grafana",
        "source-controller",
        "speedtest",
    } <= gated
    dialled = {row["url"]: row["expect"] for row in json.loads(legs_env["TARGETS"])}
    assert dialled["http://littlelink.homelab.svc.cluster.local:3000/"] == "refused"
    assert dialled["http://loki.observability.svc.cluster.local:3100/"] == "refused"
    # The flux-system rows dial the Service port, not the pod port the fence names.
    assert (
        dialled["http://source-controller.flux-system.svc.cluster.local:80/"]
        == "refused"
    )
    assert not any("grafana" in url or "speedtest" in url for url in dialled)
    assert gate_env["SSL_CERT_FILE"].endswith("serviceaccount/ca.crt")
    assert "SSL_CERT_FILE" not in legs_env
    assert legs_env["LIVENESS_URL"].startswith("https://www.local.")
    assert legs_env["DNS_NAMES"] == "daniel-pi.lan.,daniel-pi.lan"
    assert pod["serviceAccountName"] == "netpol-baseline-verify"
    labels = _job(flipped)["spec"]["template"]["metadata"]["labels"]
    assert labels == {"app": "netpol-baseline-verify", "netpol-probe": "control"}


def test_the_homelab_lever_off_drops_its_rows_and_legs(stubs, tmp_path):
    docs = _docs(_flipped_render(stubs, tmp_path, {"netpol_baseline_enforced": False}))
    legs_env = _env_of(_job(docs)["spec"]["template"]["spec"]["containers"][0])
    assert legs_env["LIVENESS_URL"] == "" and legs_env["DNS_NAMES"] == ""
    assert not any(".homelab." in row["url"] for row in json.loads(legs_env["TARGETS"]))


def test_the_roles_grant_only_lists(flipped):
    """EndpointSlices everywhere the table dials; pods only where a baseline exempts."""
    slices = {
        "apiGroups": ["discovery.k8s.io"],
        "resources": ["endpointslices"],
        "verbs": ["list"],
    }
    pods = {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}
    roles = {
        r["metadata"]["namespace"]: r["rules"] for r in flipped if r["kind"] == "Role"
    }
    assert roles == {
        "flux-system": [slices],
        "homelab": [slices, pods],
        "observability": [slices, pods],
    }


def test_every_policy_template_renames_the_job():
    """A policy template the digest leaves out would change without Flux re-running the Job."""
    listed = set(
        re.findall(
            r"lookup\('template', '(networkpolicy[\w-]*\.yaml\.j2)'\)",
            (STAGED / "job.yaml.j2").read_text(),
        )
    )
    present = {
        p.name for p in (K8S_ROLES / TAG / "templates").glob("networkpolicy*.yaml.j2")
    }
    assert "networkpolicy-netpol-sentinel.yaml.j2" in present
    assert listed == present


def test_a_policy_change_renames_the_job(stubs, tmp_path):
    names = []
    for n, cidrs in enumerate((["10.42.0.1/32"], ["10.42.0.2/32"])):
        docs = _docs(
            _flipped_render(
                stubs, tmp_path / str(n), {"netpol_baseline_node_cidrs": cidrs}
            )
        )
        names.append(_job(docs)["metadata"]["name"])
    assert names[0] != names[1]


def test_no_rendered_policy_admits_the_verify_pod():
    """Every `refused` row must refuse the Job, so no caller list may name its label."""
    admitting = [
        (role, doc["metadata"]["name"])
        for role, _tpl, doc in rendered_docs()
        if doc.get("kind") == "NetworkPolicy"
        and "netpol-baseline-verify" in json.dumps(doc)
    ]
    assert admitting == []


def test_the_staged_job_reads_no_other_roles_file():
    """The deployer redeploys a role when a file it `lookup()`s in another role changes."""
    staged = sorted(STAGED.glob("*.j2"))
    assert [p.name for p in staged] == ["job.yaml.j2", "rbac.yaml.j2"]
    assert [
        (path.name, role)
        for path in staged
        for role in ROLE_PATH.findall(path.read_text())
        if role != TAG
    ] == []
