"""qbittorrent's inert Flux Jobs: each check fails its broken case, the apply is safe to re-run.

The script tests run `qbittorrent_job.main` against a stateful local stand-in for the WebUI,
which also echoes a fixed address as the egress service does. The render tests copy the role,
move `templates/verify-inert/` to `templates/verify/` as the flip PR will, and render the copy,
so both Jobs are checked as Flux will receive them while the tree's own render still emits no
`qbittorrent-verify`. The plan is docs/target-architecture/deploy/d5-bulk-move.md, *A verify
conversion lands as inert code*.

Run: uv run pytest ansible/roles/k8s/qbittorrent/tests/test_qbittorrent_job.py
"""

import json
import re
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

import apply_prefs
import qbittorrent_job
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "qbittorrent"
COOKIE = "QBT_SID_8080=s3ss10n"
NODE_ADDRESS = "203.0.113.7"
TUNNEL_ADDRESS = "198.51.100.20"
# A path into a role's tree, as any lookup names it; group 1 is the role.
ROLE_PATH = re.compile(r"/roles/k8s/([^/'\"]+)/")


class FakeWebUI:
    """The state the stand-in serves: preferences, the external address, and what was asked."""

    def __init__(self, prefs: dict, external: str = TUNNEL_ADDRESS):
        self.prefs = dict(prefs)
        self.external = external
        self.url = ""
        self.logins = 0
        self.writes: list[dict] = []
        # Keys setPreferences accepts with a 200 and does not store.
        self.ignored: set[str] = set()


def _serve(state: FakeWebUI) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: str = "", cookie: str = "") -> None:
            data = body.encode()
            self.send_response(status)
            if cookie:
                self.send_header("Set-Cookie", f"{cookie}; HttpOnly; path=/")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            return self.headers.get("Cookie") == COOKIE

        def do_GET(self):
            if self.path == "/ip":
                self._send(200, NODE_ADDRESS + "\n")
            elif self.path == "/":
                self._send(200, "<html></html>")
            elif not self._authed():
                self._send(403, "Forbidden")
            elif self.path == "/api/v2/app/preferences":
                self._send(200, json.dumps(state.prefs))
            elif self.path == "/api/v2/transfer/info":
                self._send(
                    200, json.dumps({"last_external_address_v4": state.external})
                )
            else:
                self._send(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_qs(self.rfile.read(length).decode())
            if self.path == "/api/v2/auth/login":
                state.logins += 1
                if fields == {"username": ["admin"], "password": ["hunter2"]}:
                    self._send(204, cookie=COOKIE)
                else:
                    self._send(200, "Fails.")
            elif self.path == "/api/v2/app/setPreferences" and self._authed():
                values = json.loads(fields["json"][0])
                state.writes.append(values)
                state.prefs.update(
                    {k: v for k, v in values.items() if k not in state.ignored}
                )
                self._send(200)
            else:
                self._send(403, "Forbidden")

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    return server


@pytest.fixture
def webui():
    state = FakeWebUI(qbittorrent_job.DESIRED)
    server = _serve(state)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def cache(tmp_path):
    (tmp_path / "wireguard-mullvad.tar.xz").write_text("")
    return tmp_path


def _env(webui, job="verify", cache=None, credentials="admin\nhunter2") -> dict:
    return {
        "QBT_JOB": job,
        "QBT_URL": webui.url,
        "QBT_CREDENTIALS": credentials,
        "EGRESS_URL": f"{webui.url}/ip",
        "MODCACHE_DIR": str(cache),
    }


def test_a_tunnelled_pod_with_a_cached_mod_passes(webui, cache, capsys):
    assert qbittorrent_job.main([], _env(webui, cache=cache)) == 0
    out = capsys.readouterr().out
    assert "reach ok" in out
    assert "modcache ok: wireguard-mullvad.tar.xz" in out
    assert (
        f"qBittorrent presents {TUNNEL_ADDRESS}, this node presents {NODE_ADDRESS}"
        in out
    )


def test_traffic_leaving_in_the_clear_fails_the_job(webui, cache, capsys):
    webui.external = NODE_ADDRESS
    assert qbittorrent_job.main([], _env(webui, cache=cache)) == 1
    assert "LEAVING IN THE CLEAR" in capsys.readouterr().err


def test_no_external_address_within_the_wait_fails_the_tunnel_check(webui):
    webui.external = ""
    ui = qbittorrent_job.WebUI(webui.url)
    ui.login("admin", "hunter2")
    with pytest.raises(qbittorrent_job.Failed, match="no external address within 10s"):
        qbittorrent_job.tunnel(
            ui,
            f"{webui.url}/ip",
            wait_seconds=10,
            sleep=lambda _: None,
            clock=iter(range(99)).__next__,
        )


def test_a_refused_webui_fails_the_reach_check():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    ui = qbittorrent_job.WebUI(f"http://127.0.0.1:{port}")
    with pytest.raises(qbittorrent_job.Failed, match="kill-switch has eaten"):
        qbittorrent_job.reach(ui, retry_seconds=0)


def test_a_modcache_with_no_tarball_fails_the_job(webui, tmp_path, capsys):
    (tmp_path / "wireguard-mullvad.lock").write_text("")
    assert qbittorrent_job.main([], _env(webui, cache=tmp_path)) == 1
    assert "holds no mod tarball" in capsys.readouterr().err


def test_a_modcache_lock_beside_a_tarball_warns_and_passes(cache):
    (cache / "wireguard-mullvad.lock").write_text("")
    lines = qbittorrent_job.modcache(str(cache))
    assert lines[1].startswith(
        "WARNING: a modcache lock is present (wireguard-mullvad.lock)"
    )


def test_a_rejected_login_fails_the_job_after_one_attempt(webui, cache, capsys):
    env = _env(webui, cache=cache, credentials="admin\nwrong")
    assert qbittorrent_job.main([], env) == 1
    assert "login failed: Fails." in capsys.readouterr().err
    assert webui.logins == 1


def test_a_second_apply_writes_nothing(webui, capsys):
    """The `homelab/rerun-safe` test: a second run of the mutation Job writes no preference."""
    webui.prefs |= {"connection_speed": 30, "preallocate_all": False}
    env = _env(webui, job="apply-prefs")
    assert qbittorrent_job.main([], env) == 0
    assert webui.writes == [{"connection_speed": 100, "preallocate_all": True}]
    assert qbittorrent_job.main([], env) == 0
    assert len(webui.writes) == 1
    assert "9 preferences already match; nothing to do" in capsys.readouterr().out


def test_an_ignored_write_fails_the_apply(webui, capsys):
    """setPreferences answers 200 for a key it ignores, so only the read-back can fail."""
    webui.prefs |= {"hashing_threads": 1}
    webui.ignored = {"hashing_threads"}
    assert qbittorrent_job.main([], _env(webui, job="apply-prefs")) == 1
    assert "NOT APPLIED: hashing_threads" in capsys.readouterr().err


def test_the_job_applies_the_preferences_apply_prefs_holds():
    assert qbittorrent_job.DESIRED == apply_prefs.DESIRED


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


@pytest.fixture(scope="module")
def flipped_tag(stubs, tmp_path_factory) -> cluster.RenderedTag:
    """The render of a copy of the role whose Jobs have left `verify-inert/`."""
    roles = tmp_path_factory.mktemp("roles")
    shutil.copytree(K8S_ROLES / TAG, roles / TAG)
    templates = roles / TAG / "templates"
    (templates / "verify-inert").rename(templates / "verify")
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
def flipped(flipped_tag) -> list[dict]:
    return _docs(flipped_tag.verify)


def _jobs(docs: list[dict]) -> dict[str, dict]:
    return {
        doc["metadata"]["annotations"]["homelab/job-kind"]: doc
        for doc in docs
        if doc["kind"] == "Job"
    }


def test_the_tree_renders_no_qbittorrent_verify_while_ansible_owns_it(stubs):
    assert cluster.render_tag(TAG, stubs).verify == {}


def test_the_flipped_jobs_pass_every_job_census_row(flipped):
    kinds = sorted(doc["kind"] for doc in flipped)
    assert kinds == ["ConfigMap", "ConfigMap", "Job", "Job", "ServiceAccount"]
    assert sorted(_jobs(flipped)) == ["mutation", "verify"]
    assert problems({f"{TAG}-verify": flipped}) == []


def test_the_census_fails_a_flipped_job_once_its_spec_moves(flipped):
    """The red case for the name and backoff rows: an edited spec under the rendered name."""
    docs = [dict(doc) for doc in flipped]
    job = next(doc for doc in docs if doc["kind"] == "Job")
    job["spec"] = {**job["spec"], "backoffLimit": 1}
    found = problems({f"{TAG}-verify": docs})
    assert any(line.startswith("name: ") for line in found)


def test_qbittorrents_fence_admits_both_job_pods_on_the_webui_port(
    flipped, flipped_tag
):
    """The caller census: each Job dials the WebUI, which the role-owned fence guards."""
    (policy,) = [
        doc for doc in _docs(flipped_tag.files) if doc["kind"] == "NetworkPolicy"
    ]
    admitted = {
        (peer["podSelector"]["matchLabels"]["app"], int(port["port"]))
        for rule in policy["spec"]["ingress"]
        for peer in rule.get("from") or []
        if "matchLabels" in (peer.get("podSelector") or {})
        for port in rule["ports"]
    }
    for job in _jobs(flipped).values():
        app = job["spec"]["template"]["metadata"]["labels"]["app"]
        assert (app, 8080) in admitted, app


def test_the_verify_job_mounts_the_sidecars_modcache_from_the_config_claim(
    flipped, flipped_tag
):
    """The Job reads the cache where the sidecar writes it, so a tarball there is on the volume."""
    (deployment,) = [
        doc for doc in _docs(flipped_tag.files) if doc["kind"] == "Deployment"
    ]

    def modcache_source(pod: dict, container: str) -> tuple[str, str]:
        claims = {
            v["name"]: v["persistentVolumeClaim"]["claimName"]
            for v in pod["volumes"]
            if "persistentVolumeClaim" in v
        }
        (mount,) = [
            m
            for c in [*pod.get("initContainers", []), *pod["containers"]]
            if c["name"] == container
            for m in c["volumeMounts"]
            if m["mountPath"] == "/modcache"
        ]
        return claims[mount["name"]], mount["subPath"]

    job_pod = _jobs(flipped)["verify"]["spec"]["template"]["spec"]
    sidecar = modcache_source(deployment["spec"]["template"]["spec"], "wireguard")
    assert sidecar == ("qbittorrent-config", "modcache")
    assert modcache_source(job_pod, "verify") == sidecar


def test_the_jobs_read_no_other_roles_file():
    """The helper and the image come from `render_tag`, never a lookup into another role."""
    staged = sorted((K8S_ROLES / TAG / "templates" / "verify-inert").glob("*.j2"))
    assert [p.name for p in staged] == ["job.yaml.j2", "prefs.yaml.j2", "rbac.yaml.j2"]
    assert [
        (path.name, role)
        for path in staged
        for role in ROLE_PATH.findall(path.read_text())
        if role != TAG
    ] == []
