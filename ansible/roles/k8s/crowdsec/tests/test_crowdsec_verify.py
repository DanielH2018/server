"""crowdsec's inert verify Job, against a fixture LAPI and Prometheus, and as Flux would render it.

`World` answers the script's LAPI and Prometheus calls from a small state machine: the ban
lands when the alert is posted, and each counter read returns the next scripted sample. The
red fixtures are D5's (*crowdsec's verify from outside the cluster*): an edge that answers 200
while banned fails naming the edge, and a blocked evidence channel fails naming the channel.
Every path after the ban must lift it.

The render tests copy the role, move `templates/verify-inert/` to `templates/verify/` as the
flip PR will, and render the copy, while the tree's own render still emits no
`crowdsec-verify`. The machine the flipped Job logs in as must be one the LAPI pod's
`lapi-register` initContainer registers.

Run: uv run pytest ansible/roles/k8s/crowdsec/tests/test_crowdsec_verify.py
"""

import re
import shutil
import urllib.parse

import pytest

import crowdsec_verify as cv
from _k8s_render import render_role_template
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from lib.repo_paths import ANSIBLE
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.renderer_lib.restart import pod_references
from renderer.tests._job_census import problems

TAG = "crowdsec"
PI = "10.0.0.139"
ENV = {
    "LAPI_URL": "http://crowdsec:8080",
    "LAPI_MACHINE": "crowdsec-verify",
    "LAPI_PASSWORD": "pw",
    "BAN_IP": PI,
    "BAN_DURATION": "15m",
    "PROMETHEUS_URL": "http://prometheus:9090",
    "WINDOW_SECONDS": "300",
}


def _series(status: str, value: float) -> dict:
    return {"metric": {"status": status}, "value": [0, str(value)]}


class World(cv.Http):
    """A fixture LAPI and Prometheus. `samples` is the counter's value per read, in order."""

    def __init__(self, samples, up=1, ban_reads_back=True, lift_status=200):
        super().__init__()
        self.samples = list(samples)
        self.up = up
        self.ban_reads_back = ban_reads_back
        self.lift_status = lift_status
        self.banned = False
        self.calls = []

    def call(self, method, url, body=None, headers=None):
        parsed = urllib.parse.urlsplit(url)
        self.calls.append((method, parsed.path))
        if parsed.path == "/v1/watchers/login":
            return 200, {"token": "jwt"}
        if parsed.path == "/v1/alerts" and method == "POST":
            assert headers == {"Authorization": "Bearer jwt"}
            assert isinstance(body, list)
            [alert] = body
            [decision] = alert["decisions"]
            assert (decision["type"], decision["value"]) == ("ban", PI)
            self.banned = True
            return 201, ["1"]
        if parsed.path == "/v1/alerts":
            return 200, [{"id": 1}] if self.banned and self.ban_reads_back else []
        if parsed.path == "/v1/decisions" and method == "DELETE":
            self.banned = False
            return self.lift_status, {"nbDeleted": "1"}
        query = urllib.parse.parse_qs(parsed.query)["query"][0]
        if query == cv.CHANNEL_UP:
            result = [_series("", self.up)] if self.up is not None else []
        else:
            sample = self.samples.pop(0) if len(self.samples) > 1 else self.samples[0]
            result = [_series(s, v) for s, v in sample.items()]
        return 200, {"status": "success", "data": {"result": result}}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _run(world, capsys):
    clock = Clock()
    try:
        line = cv.run(ENV, world, clock=clock, sleep=clock.sleep)
    except cv.Failed as err:
        return "failed", str(err)
    return "passed", line


BEFORE = {"302": 100.0}


def test_an_edge_that_answers_403_while_banned_passes_and_lifts(capsys):
    world = World([BEFORE, BEFORE, {"302": 100.0, "403": 2.0}])
    assert _run(world, capsys) == (
        "passed",
        f"the edge answered 403 to {PI} 2 time(s) while banned",
    )
    assert world.calls[-1] == ("DELETE", "/v1/decisions")
    assert not world.banned


def test_an_edge_that_answers_200_while_banned_fails_naming_the_edge(capsys):
    world = World([BEFORE, {"302": 100.0, "200": 5.0}])
    result, message = _run(world, capsys)
    assert result == "failed"
    assert message.startswith(
        f"the edge: LAPI holds the ban on {PI}, but the edge answered 200 x5"
    )
    assert not world.banned


def test_a_dead_alloy_pi_scrape_fails_naming_the_channel_before_any_ban(capsys):
    world = World([BEFORE], up=0)
    result, message = _run(world, capsys)
    assert (result, message.split(":")[0]) == ("failed", "the evidence channel")
    # Nothing was banned, so nothing needed lifting.
    assert ("POST", "/v1/alerts") not in world.calls


def test_a_counter_with_no_sample_in_the_window_fails_naming_the_channel(capsys):
    world = World([BEFORE])
    result, message = _run(world, capsys)
    assert result == "failed"
    assert message.startswith("the evidence channel: no probe answer")
    assert not world.banned


def test_a_ban_that_does_not_read_back_fails_naming_lapi_and_lifts(capsys):
    world = World([BEFORE], ban_reads_back=False)
    assert _run(world, capsys) == (
        "failed",
        f"LAPI holds no active decision for {PI} after the ban",
    )
    assert world.calls[-1] == ("DELETE", "/v1/decisions")


def test_a_refused_lift_is_reported_and_does_not_hide_the_verdict(capsys):
    world = World([BEFORE, {"302": 100.0, "403": 1.0}], lift_status=403)
    assert _run(world, capsys)[0] == "passed"
    assert "did not lift the ban on 10.0.0.139: HTTP 403" in capsys.readouterr().err


class Unreachable(World):
    """LAPI stops answering at the lift: every DELETE raises as a dead connect does."""

    def call(self, method, url, body=None, headers=None):
        if method == "DELETE":
            raise OSError(111, "Connection refused")
        return super().call(method, url, body, headers)


def test_an_unreachable_lapi_at_the_lift_keeps_the_verdict(capsys):
    world = Unreachable([BEFORE, {"302": 100.0, "403": 1.0}])
    assert _run(world, capsys)[0] == "passed"
    assert "no answer (ConnectionRefusedError(111" in capsys.readouterr().err


def test_a_transport_failure_on_a_hop_is_a_verdict_not_a_traceback(capsys):
    class Dead(cv.Http):
        def call(self, method, url, body=None, headers=None):
            raise OSError(113, "No route to host")

    assert cv.main([], ENV, Dead()) == 1
    assert "a call got no answer: OSError(113" in capsys.readouterr().err


def test_a_counter_reset_counts_its_whole_value_as_the_rise():
    assert cv.increase({"403": 7.0, "302": 50.0}, {"403": 2.0, "302": 51.0}) == {
        "403": 2.0,
        "302": 1.0,
    }


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _docs(texts: dict[str, str]) -> list[dict]:
    return [
        doc
        for name, text in sorted(texts.items())
        if name != "kustomization.yaml"
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


def _render_tree_copy(tmp_path, stubs, edit=None) -> cluster.RenderedTag:
    """The flipped render of a copy of the role and of `cluster-vars.source`.

    `edit(role)` changes the copied role first. `playbook_dir` rides in the stubs, which outrank
    every other layer, so the Job's lookups read the copy rather than the tree.
    """
    tree = tmp_path / "ansible"
    role = tree / "roles" / "k8s" / TAG
    shutil.copytree(K8S_ROLES / TAG, role)
    # The jobs ConfigMap ships setup/common's kuma_push.py and k8s/manifests' helper.
    (tree / "roles" / "setup").symlink_to(ANSIBLE / "roles" / "setup")
    (tree / "roles" / "k8s" / "manifests").symlink_to(K8S_ROLES / "manifests")
    (tree / "vars").mkdir()
    shutil.copy(ANSIBLE / "vars" / "cluster-vars.source", tree / "vars")
    (role / "templates" / "verify-inert").rename(role / "templates" / "verify")
    if edit:
        edit(role)
    copied = {**stubs, "playbook_dir": str(tree)}
    return cluster.render_tag(TAG, copied, roles=tree / "roles" / "k8s")


@pytest.fixture(scope="module")
def flipped_tag(stubs, tmp_path_factory) -> cluster.RenderedTag:
    return _render_tree_copy(tmp_path_factory.mktemp("flipped"), stubs)


@pytest.fixture(scope="module")
def flipped(flipped_tag) -> list[dict]:
    return _docs(flipped_tag.verify)


def _job(docs: list[dict]) -> dict:
    (job,) = [doc for doc in docs if doc["kind"] == "Job"]
    return job


def _env_of(job: dict) -> dict[str, str]:
    (container,) = job["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value") for e in container["env"]}


def test_the_tree_renders_no_crowdsec_verify_while_ansible_owns_it(stubs):
    assert cluster.render_tag(TAG, stubs).verify == {}


def test_the_flipped_job_passes_every_job_census_row(flipped):
    assert sorted(doc["kind"] for doc in flipped) == [
        "ConfigMap",
        "Job",
        "Secret",
        "ServiceAccount",
    ]
    assert problems({f"{TAG}-verify": flipped}) == []


def test_the_flipped_job_bans_the_pi_and_reads_the_alloy_pi_counter(flipped):
    (job,) = [doc for doc in flipped if doc["kind"] == "Job"]
    (container,) = job["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["BAN_IP"] == PI
    assert env["PROMETHEUS_URL"].startswith("http://prometheus.observability.svc")


def test_the_lapi_pod_registers_the_machine_the_flipped_job_logs_in_as(flipped):
    (job,) = [doc for doc in flipped if doc["kind"] == "Job"]
    (container,) = job["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value") for e in container["env"]}
    [deployment] = yaml_fast.safe_load_all(
        render_role_template(TAG, "deployment.yaml.j2")
    )
    [script] = [
        c["command"][-1]
        for c in deployment["spec"]["template"]["spec"]["initContainers"]
        if c["name"] == "lapi-register"
    ]
    [machines] = re.findall(r"^for machine in (.+); do$", script, re.MULTILINE)
    assert env["LAPI_MACHINE"] in machines.split()


def _admitted(policy: dict, namespace: str | None = None) -> set[tuple[str, int]]:
    """(app, port) for each `from` peer of `policy`, its namespace pinned when one is named.

    A peer with a namespaceSelector counts only under `namespace`; one without counts only when
    `namespace` is None, the policy's own namespace.
    """
    admitted = set()
    for rule in policy["spec"]["ingress"]:
        for peer in rule.get("from") or []:
            selector = peer.get("namespaceSelector")
            pinned = (
                (selector or {})
                .get("matchLabels", {})
                .get("kubernetes.io/metadata.name")
            )
            if pinned != namespace:
                continue
            pods = peer.get("podSelector") or {}
            apps = [pods["matchLabels"]["app"]] if "matchLabels" in pods else []
            for expr in pods.get("matchExpressions", []):
                if expr["key"] == "app" and expr["operator"] == "In":
                    apps.extend(expr["values"])
            admitted |= {(app, int(p["port"])) for app in apps for p in rule["ports"]}
    return admitted


def _policy(stubs, name: str) -> dict:
    rendered = cluster.render_tag("netpol-baseline", stubs)
    (policy,) = [
        doc
        for doc in _docs({**rendered.files, **rendered.deferred})
        if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == name
    ]
    return policy


def test_the_lapi_fence_admits_the_job_on_the_lapi_port_alone(flipped, stubs):
    """The Job logs in to the LAPI; AppSec is traefik's alone, so the Job never gets it."""
    job_app = _job(flipped)["spec"]["template"]["metadata"]["labels"]["app"]
    assert job_app == "crowdsec-verify"
    lapi = int(_env_of(_job(flipped))["LAPI_URL"].rsplit(":", 1)[1])
    admitted = _admitted(_policy(stubs, "crowdsec"))
    assert (job_app, lapi) in admitted
    assert {port for app, port in admitted if app == job_app} == {lapi}
    # traefik's rule also opens AppSec, which is why the Job's peer sits in another rule.
    assert {port for app, port in admitted if app == "traefik"} > {lapi}


def test_prometheus_admits_the_job_from_the_homelab_namespace_only(flipped, stubs):
    """The namespace and pod selectors are one peer, so the label alone admits nothing."""
    job_app = _job(flipped)["spec"]["template"]["metadata"]["labels"]["app"]
    assert _env_of(_job(flipped))["PROMETHEUS_URL"].endswith(":9090")
    policy = _policy(stubs, "prometheus-callers")
    assert (job_app, 9090) in _admitted(policy, _job(flipped)["metadata"]["namespace"])
    assert (job_app, 9090) not in _admitted(policy)


def _edit_a_whitelist(role) -> None:
    path = role / "files" / "crowdsec-whitelist.yaml"
    path.write_text(path.read_text() + "# a whitelist-only edit\n")


def _edit_a_job(role) -> None:
    path = role / "files" / "bouncer_prune.py"
    path.write_text(path.read_text() + "# a job-only edit\n")


def _rotate_the_bouncer_key(role) -> None:
    source = role.parents[2] / "vars" / "cluster-vars.source"
    lines = source.read_text().splitlines()
    (row,) = [
        n
        for n, line in enumerate(lines)
        if line.startswith("crowdsec_k8s_bouncer_api_key ")
    ]
    key, _digest, *rest = lines[row].split()
    lines[row] = " ".join([key, "0" * 64, *rest])
    source.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize(
    "edit",
    [_edit_a_whitelist, _edit_a_job, _rotate_the_bouncer_key],
    ids=["whitelist", "job", "rotation"],
)
def test_a_change_that_rolls_the_pod_renames_the_job(edit, flipped, stubs, tmp_path):
    """Each rolls crowdsec through `checksum/restart` and leaves the Deployment's text alone."""
    after = _docs(_render_tree_copy(tmp_path, stubs, edit).verify)
    before_env, after_env = _env_of(_job(flipped)), _env_of(_job(after))
    assert before_env["CROWDSEC_ROLL_DIGEST8"] != after_env["CROWDSEC_ROLL_DIGEST8"]
    assert _job(flipped)["metadata"]["name"] != _job(after)["metadata"]["name"]


def test_every_input_the_pod_names_is_one_the_roll_digest_reads(flipped_tag):
    """A ConfigMap or Secret the pod gains moves `checksum/restart`, so the digest must read it."""
    sources = {
        (doc["kind"], doc["metadata"]["name"]): f"{name}.j2"
        for name, text in flipped_tag.files.items()
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc.get("kind") in ("ConfigMap", "Secret")
    }
    (deployment,) = [
        doc for doc in _docs(flipped_tag.files) if doc["kind"] == "Deployment"
    ]
    # kube-root-ca.crt is the cluster's own ConfigMap, which no render emits.
    named = {
        sources[ref]
        for ref in pod_references(deployment["spec"]["template"])
        if ref in sources
    }
    assert named == {
        "config-secret.yaml.j2",
        "jobs-configmap.yaml.j2",
        "jobs-secret.yaml.j2",
    }
    text = (K8S_ROLES / TAG / "templates" / "verify-inert" / "job.yaml.j2").read_text()
    assert {name for name in named if f"{name}'" not in text} == set()
