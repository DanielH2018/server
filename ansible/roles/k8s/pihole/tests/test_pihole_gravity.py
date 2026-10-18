"""pihole's Flux forms: the gravity reconcile Job, and instance 2's apply after instance 1.

The script tests run `pihole_gravity.main` against two stateful local stand-ins for FTL's v6
REST API, one per instance. The render tests copy the role, move `templates/verify-inert/` to
`templates/verify/` as the flip PR will, and render the copy, so the Job is checked as Flux will
receive it while the tree's own render still emits no `pihole-verify`. The plan is
docs/target-architecture/deploy/d5-bulk-move.md, *A verify conversion lands as inert code*.

Run: uv run pytest ansible/roles/k8s/pihole/tests/test_pihole_gravity.py
"""

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import pytest

import pihole_gravity
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from lib.repo_paths import ANSIBLE
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.bundle import bundle_files
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "pihole"
PASSWORD = "hunter2"
OISD = "https://big.oisd.nl"
STEVENBLACK = "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"
REGEX = [r"(\.|^)logs\.roku\.com$", r"(\.|^)ads\.samsung\.com$"]
DECLARED = {
    "adlists": [
        {"address": OISD, "enabled": 1},
        {"address": STEVENBLACK, "enabled": 0},
    ],
    "regex_deny": REGEX,
}
GRAVITY_OK = "  [i] Swapping databases...\n  [✓] Swapping databases\n  [✓] Done.\n"
DOTENV = "pihole_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"


class FakeFTL:
    """One instance's API state: its lists, its sessions, and what was asked of it."""

    def __init__(self, lists: dict[str, bool], regex: set[str]):
        self.lists = {
            address: {
                "address": address,
                "type": "block",
                "comment": "seeded",
                "groups": [0, 3],
                "enabled": enabled,
            }
            for address, enabled in lists.items()
        }
        self.regex = set(regex)
        self.url = ""
        self.logins = 0
        self.sessions: set[str] = set()
        self.writes: list[tuple[str, str, dict]] = []
        self.gravity_runs = 0
        self.gravity_output = GRAVITY_OK
        # Addresses a PUT answers 200 for and does not store.
        self.ignored: set[str] = set()


def _serve(state: FakeFTL) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: object = None) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")

        def _authed(self) -> bool:
            return self.headers.get("X-FTL-SID") in state.sessions

        def do_GET(self):
            url = urlsplit(self.path)
            if not self._authed():
                self._send(401, {"error": {"key": "unauthorized"}})
            elif url.path == "/api/lists" and url.query == "type=block":
                self._send(200, {"lists": list(state.lists.values())})
            elif url.path == "/api/domains/deny/regex":
                domains = [
                    {"domain": d, "type": "deny", "kind": "regex"} for d in state.regex
                ]
                self._send(200, {"domains": domains})
            else:
                self._send(404)

        def do_POST(self):
            url = urlsplit(self.path)
            body = self._body()
            if url.path == "/api/auth":
                state.logins += 1
                if body.get("password") != PASSWORD:
                    self._send(401, {"session": {"valid": False}})
                    return
                sid = f"sid-{state.logins}"
                state.sessions.add(sid)
                self._send(200, {"session": {"valid": True, "sid": sid}})
            elif not self._authed():
                self._send(401, {"error": {"key": "unauthorized"}})
            elif url.path == "/api/lists" and url.query == "type=block":
                state.writes.append(("POST", url.path, body))
                state.lists[body["address"]] = {**body, "type": "block"}
                self._send(
                    201,
                    {
                        "processed": {
                            "success": [{"item": body["address"]}],
                            "errors": [],
                        }
                    },
                )
            elif url.path == "/api/domains/deny/regex":
                state.writes.append(("POST", url.path, body))
                state.regex.add(body["domain"])
                self._send(
                    201,
                    {
                        "processed": {
                            "success": [{"item": body["domain"]}],
                            "errors": [],
                        }
                    },
                )
            elif url.path == "/api/action/gravity":
                state.gravity_runs += 1
                data = state.gravity_output.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404)

        def do_PUT(self):
            url = urlsplit(self.path)
            body = self._body()
            # FTL routes on the raw path: an address whose `/` went unencoded is a longer path
            # that names no list.
            prefix = "/api/lists/"
            address = unquote(url.path[len(prefix) :])
            if not self._authed():
                self._send(401, {"error": {"key": "unauthorized"}})
            elif (
                not url.path.startswith(prefix)
                or "/" in url.path[len(prefix) :]
                or url.query != "type=block"
                or address not in state.lists
            ):
                self._send(404, {"error": {"key": "not_found"}})
            else:
                state.writes.append(("PUT", address, body))
                if address not in state.ignored:
                    state.lists[address] = {**body, "address": address}
                self._send(
                    200, {"processed": {"success": [{"item": address}], "errors": []}}
                )

        def do_DELETE(self):
            sid = self.headers.get("X-FTL-SID")
            if urlsplit(self.path).path == "/api/auth" and sid in state.sessions:
                state.sessions.discard(sid)
                self._send(204)
            else:
                self._send(401, {"error": {"key": "unauthorized"}})

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    return server


@pytest.fixture
def instances():
    """Instance 1 drifted (oisd and one regex absent, StevenBlack enabled), instance 2 matching."""
    states = [
        FakeFTL({STEVENBLACK: True}, {REGEX[1]}),
        FakeFTL({OISD: True, STEVENBLACK: False}, set(REGEX)),
    ]
    servers = [_serve(state) for state in states]
    for state, server in zip(states, servers, strict=True):
        state.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield states
    for server in servers:
        server.shutdown()
        server.server_close()


def _env(instances, tmp_path, password=PASSWORD) -> dict:
    declared = tmp_path / "declared.json"
    declared.write_text(json.dumps(DECLARED))
    return {
        "PIHOLE_TARGETS": " ".join(
            f"{name}={state.url}"
            for name, state in zip(["pihole", "pihole-2"], instances, strict=True)
        ),
        "PIHOLE_PASSWORD": password,
        "DECLARED_PATH": str(declared),
    }


def test_a_drifted_instance_is_reconciled_and_both_rebuild(instances, tmp_path, capsys):
    first, second = instances
    assert pihole_gravity.main([], _env(instances, tmp_path)) == 0
    assert first.writes == [
        (
            "POST",
            "/api/lists",
            {
                "address": OISD,
                "comment": "ansible-declared",
                "groups": [0],
                "enabled": True,
            },
        ),
        # The PUT keeps the comment and groups the row already had.
        (
            "PUT",
            STEVENBLACK,
            {"comment": "seeded", "type": "block", "groups": [0, 3], "enabled": False},
        ),
        (
            "POST",
            "/api/domains/deny/regex",
            {
                "domain": REGEX[0],
                "comment": "ansible-declared",
                "groups": [0],
                "enabled": True,
            },
        ),
    ]
    assert second.writes == []
    # Either instance changing rebuilds both, so the two stay equivalent.
    assert [first.gravity_runs, second.gravity_runs] == [1, 1]
    assert [first.sessions, second.sessions] == [set(), set()]
    assert "pihole-2: gravity rebuilt" in capsys.readouterr().out


def test_a_second_run_writes_nothing_and_rebuilds_nothing(instances, tmp_path, capsys):
    """The `homelab/rerun-safe` test: a second run of the mutation Job changes nothing."""
    env = _env(instances, tmp_path)
    assert pihole_gravity.main([], env) == 0
    writes = [len(state.writes) for state in instances]
    assert pihole_gravity.main([], env) == 0
    assert [len(state.writes) for state in instances] == writes
    assert [state.gravity_runs for state in instances] == [1, 1]
    assert [state.sessions for state in instances] == [set(), set()]
    assert "2 instances match the declaration; nothing to do" in capsys.readouterr().out


def test_a_rejected_login_fails_after_one_attempt(instances, tmp_path, capsys):
    assert pihole_gravity.main([], _env(instances, tmp_path, password="wrong")) == 1
    assert "pihole: login refused with HTTP 401" in capsys.readouterr().err
    assert [state.logins for state in instances] == [1, 0]
    assert [state.writes for state in instances] == [[], []]


def test_a_write_the_read_back_does_not_find_fails(instances, tmp_path, capsys):
    instances[0].ignored = {STEVENBLACK}
    assert pihole_gravity.main([], _env(instances, tmp_path)) == 1
    assert (
        f"NOT APPLIED: adlist {STEVENBLACK} has enabled=True" in capsys.readouterr().err
    )
    assert [state.gravity_runs for state in instances] == [0, 0]
    # The session is deleted on the failing path too.
    assert instances[0].sessions == set()


def test_a_gravity_run_that_never_swaps_its_database_fails(instances, tmp_path, capsys):
    instances[0].gravity_output = "  [✗] Unable to build gravity tree\n"
    assert pihole_gravity.main([], _env(instances, tmp_path)) == 1
    assert (
        "pihole: gravity never swapped in its new database" in capsys.readouterr().err
    )


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _render_tree_copy(tmp_path, stubs, edit=None) -> cluster.RenderedTag:
    """The flipped render of a copy of the role and of `cluster-vars.source`.

    `edit(role)` changes the copied role first. `playbook_dir` rides in the stubs, which outrank
    every other layer, so the Job's lookups read the copy rather than the tree.
    """
    tree = tmp_path / "ansible"
    role = tree / "roles" / "k8s" / TAG
    shutil.copytree(K8S_ROLES / TAG, role)
    (tree / "vars").mkdir()
    shutil.copy(ANSIBLE / "vars" / "cluster-vars.source", tree / "vars")
    (role / "templates" / "verify-inert").rename(role / "templates" / "verify")
    if edit:
        edit(role)
    copied = {**stubs, "playbook_dir": str(tree)}
    return cluster.render_tag(TAG, copied, roles=tree / "roles" / "k8s")


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
    return _render_tree_copy(tmp_path_factory.mktemp("flipped"), stubs)


def _job(docs: list[dict]) -> dict:
    (job,) = [doc for doc in docs if doc["kind"] == "Job"]
    return job


def test_the_tree_renders_no_pihole_verify_while_ansible_owns_it(stubs):
    assert cluster.render_tag(TAG, stubs).verify == {}


def test_the_flipped_job_passes_every_job_census_row(flipped_tag):
    docs = _docs(flipped_tag.verify)
    assert sorted(doc["kind"] for doc in docs) == ["ConfigMap", "Job", "ServiceAccount"]
    assert problems({f"{TAG}-verify": docs}) == []


def test_the_job_applies_the_declaration_the_role_defaults_hold(flipped_tag):
    (configmap,) = [
        doc for doc in _docs(flipped_tag.verify) if doc["kind"] == "ConfigMap"
    ]
    defaults = yaml_fast.safe_load(
        (K8S_ROLES / TAG / "defaults" / "main.yml").read_text()
    )
    assert json.loads(configmap["data"]["declared.json"]) == {
        "adlists": defaults["pihole_k8s_adlists"],
        "regex_deny": defaults["pihole_k8s_regex_deny"],
    }


def _edit_dnsmasq(role) -> None:
    config = role / "templates" / "config" / "pihole-dnsmasq.conf.j2"
    config.write_text(config.read_text() + "# a dnsmasq-only edit\n")


def _edit_declaration(role) -> None:
    defaults = role / "defaults" / "main.yml"
    defaults.write_text(
        defaults.read_text() + "pihole_k8s_regex_deny: ['(\\.|^)x\\.example$']\n"
    )


@pytest.mark.parametrize(
    "edit", [_edit_dnsmasq, _edit_declaration], ids=["config", "declaration"]
)
def test_a_change_that_rolls_a_pihole_or_moves_the_declaration_renames_the_job(
    edit, stubs, tmp_path
):
    """A config edit rolls both instances through `checksum/restart` and leaves the Job text alone."""
    before = _job(_docs(_render_tree_copy(tmp_path / "a", stubs).verify))
    after = _job(_docs(_render_tree_copy(tmp_path / "b", stubs, edit).verify))
    assert before["metadata"]["name"] != after["metadata"]["name"]


def test_the_pihole_fence_admits_the_gravity_job_on_the_web_port(flipped_tag, stubs):
    """The caller census: the Job dials pihole:80 and pihole-2-web:80, through one fence."""
    policies = [
        doc
        for doc in _docs(cluster.render_tag("netpol-baseline", stubs).files)
        if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "pihole"
    ]
    (policy,) = policies
    admitted = {
        (peer["podSelector"]["matchLabels"]["app"], int(port["port"]))
        for rule in policy["spec"]["ingress"]
        for peer in rule.get("from") or []
        for port in rule["ports"]
    }
    job = _job(_docs(flipped_tag.verify))
    app = job["spec"]["template"]["metadata"]["labels"]["app"]
    assert (app, 80) in admitted
    targets = next(
        env["value"]
        for env in job["spec"]["template"]["spec"]["containers"][0]["env"]
        if env["name"] == "PIHOLE_TARGETS"
    )
    assert [url.rsplit(":", 1)[1] for url in targets.split()] == ["80", "80"]


def test_instance_2_applies_from_pihole_deferred_after_instance_1_is_ready(flipped_tag):
    """`roll_one.yml`'s Flux form: `pihole-deferred` waits on `pihole`, then `pihole-verify` on it."""
    deployments = {
        directory: [
            doc["metadata"]["name"]
            for doc in _docs(files)
            if doc["kind"] == "Deployment"
        ]
        for directory, files in flipped_tag.directories().items()
    }
    assert deployments["pihole"] == ["pihole"]
    assert deployments["pihole-deferred"] == ["pihole-2"]
    files = bundle_files(flipped_tag.directories(), {}, [TAG], "600s", DOTENV)
    kustomizations = {
        doc["metadata"]["name"]: doc["spec"]
        for doc in yaml_fast.safe_load_all(files[f"flux/{TAG}.yaml"])
    }
    for name in ("pihole", "pihole-deferred", "pihole-verify"):
        assert kustomizations[name]["wait"] is True, name
    depends = {
        name: [edge["name"] for edge in spec.get("dependsOn", [])]
        for name, spec in kustomizations.items()
    }
    assert "pihole" in depends["pihole-deferred"]
    assert "pihole-deferred" in depends["pihole-verify"]
