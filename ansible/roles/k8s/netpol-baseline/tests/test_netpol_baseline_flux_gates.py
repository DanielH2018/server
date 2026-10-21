"""The Flux forms of netpol-baseline's lever asserts and live exempt-set gates (#5685).

The role's `tasks/main.yml` ran both before its apply, and neither was part of the probe.
Under Flux no task runs.

- The lever asserts become a check over the render context: the variables
  `renderer_lib.cluster.tag_context` resolves for the tag, the context Flux's render reads.
  Flux takes no `-e`, so the repo's defaults and inventory are every value a lever can hold,
  and CI runs this before a typo can merge. It evaluates the predicates of the Ansible assert it replaces, and every lever default must
  be named there.
- The exempt-set gates become the `exempt` check of the verify Job's `gate` initContainer,
  which runs after the apply. The script tests run it against fake API reads; the render
  tests read the flipped Job.

Run: uv run pytest ansible/roles/k8s/netpol-baseline/tests/test_netpol_baseline_flux_gates.py
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jinja2
import pytest
from _netpol_baseline_render import TAG, docs, env_of, job, verify_render, write_stubs

import netpol_baseline_verify as verify
from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env
from lib.k8s_roles import K8S_ROLES
from renderer.renderer_lib import cluster

DEFAULTS = K8S_ROLES / TAG / "defaults" / "main.yml"
# A lever is a role default that ends in `enforced` or `scope`: each plane's on/off switch
# and its label/namespace selector. A third plane's pair matches without an edit here.
LEVER = re.compile(r"^netpol_baseline_(?:\w+_)?(?:enforced|scope)$")


# The `that:` list of the "Check the netpol-baseline levers" assert that `tasks/main.yml` ran
# before its flip, verbatim (#5685). The levers are read where they coerce differently:
#   - `netpol_baseline_enforced` and `netpol_baseline_obs_enforced` go through `| bool` in the
#     templates, so a typo such as `fasle` is False, the allow-all body renders and nothing is
#     fenced behind a green deploy.
#   - `netpol_baseline_scope` and `netpol_baseline_obs_scope` are compared with `== 'namespace'`
#     and fall through to the label selector on any other value, so `Namespace` or `all` skips
#     the whole-namespace default-deny.
LEVER_ASSERTS = [
    "netpol_baseline_enforced | string | lower in ['true', 'false']",
    "netpol_baseline_scope in ['label', 'namespace']",
    "netpol_baseline_obs_enforced | string | lower in ['true', 'false']",
    "netpol_baseline_obs_scope in ['label', 'namespace']",
]


def lever_problems(ctx: dict) -> list[str]:
    """The levers whose `that:` condition is false in `ctx`, as the assert would refuse them.

    Each condition is evaluated with Ansible's filters, under a strict undefined so a lever
    missing from the context fails rather than reading as false.
    """
    env = make_ansible_env(undefined_cls=jinja2.StrictUndefined)
    return [
        expr.split()[0]
        for expr in LEVER_ASSERTS
        if not env.compile_expression(expr)(**ctx)
    ]


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    return write_stubs(tmp_path_factory)


def _context(stubs, overrides=None) -> dict:
    return cluster.tag_context(K8S_ROLES / TAG, TAG, {**stubs, **(overrides or {})})


def test_every_lever_default_is_asserted():
    """A lever the assert does not name would reach a Flux render unchecked."""
    levers = {k for k in yaml_fast.safe_load(DEFAULTS.read_text()) if LEVER.match(k)}
    assert "netpol_baseline_obs_scope" in levers
    assert levers == {expr.split()[0] for expr in LEVER_ASSERTS}


def test_the_rendered_levers_are_clean(stubs):
    assert lever_problems(_context(stubs)) == []


@pytest.mark.parametrize(
    ("lever", "value"),
    [
        ("netpol_baseline_enforced", "fasle"),
        ("netpol_baseline_obs_enforced", "yes"),
        ("netpol_baseline_scope", "Namespace"),
        ("netpol_baseline_obs_scope", "all"),
    ],
)
def test_a_mistyped_lever_is_flagged(stubs, lever, value):
    assert lever_problems(_context(stubs, {lever: value})) == [lever]


@pytest.mark.parametrize(
    "overrides",
    [
        {"netpol_baseline_enforced": False},
        {"netpol_baseline_obs_enforced": "false"},
        {"netpol_baseline_scope": "label", "netpol_baseline_obs_scope": "label"},
    ],
    ids=["enforced-off", "obs-off-as-string", "label-scope"],
)
def test_a_rollback_lever_value_is_clean(stubs, overrides):
    """The documented rollbacks are valid values, so the check must pass them."""
    assert lever_problems(_context(stubs, overrides)) == []


HOMELAB = {
    "namespace": "homelab",
    "variable": "netpol_baseline_exempt_workloads",
    "apps": ["headlamp", "registry"],
}
OBSERVABILITY = {
    "namespace": "observability",
    "variable": "netpol_baseline_obs_exempt_workloads",
    "apps": [],
}


def _pod(name, app=None):
    labels = {"netpol-baseline-exempt": "true"} | ({"app": app} if app else {})
    return {"metadata": {"name": name, "labels": labels}}


def _reader(pods_by_namespace):
    paths = []

    def get(path):
        paths.append(path)
        namespace = path.split("/")[4]
        return {"items": pods_by_namespace.get(namespace, [])}

    return get, paths


def test_the_exact_exempt_set_is_clean():
    """Two pods of one app, as a rollout runs, still count as one exempt app."""
    get, paths = _reader(
        {
            "homelab": [
                _pod("registry-a", "registry"),
                _pod("registry-b", "registry"),
                _pod("headlamp-a", "headlamp"),
            ]
        }
    )
    assert verify.exempt([HOMELAB, OBSERVABILITY], get) == [
        "exempt ok: homelab exempts exactly headlamp, registry",
        "exempt ok: observability exempts exactly nothing",
    ]
    assert paths[0] == (
        "/api/v1/namespaces/homelab/pods?labelSelector=netpol-baseline-exempt"
    )


@pytest.mark.parametrize(
    ("pods", "message"),
    [
        (
            {"homelab": [_pod("registry-a", "registry")]},
            r"homelab: headlamp carries no exemption",
        ),
        (
            {
                "homelab": [_pod("h", "headlamp"), _pod("r", "registry")],
                "observability": [_pod("loki-0", "loki")],
            },
            r"observability: loki is exempt but not in "
            r"netpol_baseline_obs_exempt_workloads",
        ),
        (
            {
                "homelab": [
                    _pod("h", "headlamp"),
                    _pod("r", "registry"),
                    _pod("stray-0"),
                ]
            },
            r"pod stray-0 \(no app label\) is exempt",
        ),
    ],
    ids=["missing", "unexpected", "no-app-label"],
)
def test_exempt_set_drift_is_flagged(pods, message):
    get, _ = _reader(pods)
    with pytest.raises(verify.Failed, match=message):
        verify.exempt([HOMELAB, OBSERVABILITY], get)


def test_the_gate_stage_reads_pods_as_metadata_only(tmp_path):
    """A `pods` list returns whole specs unless the read asks for metadata alone."""
    accepts = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            accepts[self.path.split("?")[0]] = self.headers["Accept"]
            body = json.dumps(
                {"items": [{"endpoints": [{"conditions": {"ready": True}}]}]}
                if "endpointslices" in self.path
                else {"items": []}
            ).encode()
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
    token = tmp_path / "token"
    token.write_text("tok\n")
    env = {
        "NETPOL_VERIFY_STAGE": "gate",
        "EXEMPT_SETS": json.dumps([OBSERVABILITY]),
        "GATE_TARGETS": json.dumps(
            [
                {
                    "service": "loki",
                    "namespace": "observability",
                    "plane": "observability",
                }
            ]
        ),
    }
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        lines = verify.run(env, token_file=str(token), base=base)
    finally:
        server.shutdown()
        server.server_close()
    assert lines[1] == "exempt ok: observability exempts exactly nothing"
    assert accepts == {
        "/api/v1/namespaces/observability/pods": verify.METADATA_ONLY,
        "/apis/discovery.k8s.io/v1/namespaces/observability/endpointslices": (
            "application/json"
        ),
    }


def _exempt_sets(rendered_docs) -> list[dict]:
    gate = job(rendered_docs)["spec"]["template"]["spec"]["initContainers"][0]
    return json.loads(env_of(gate)["EXEMPT_SETS"])


def test_the_flipped_job_checks_each_namespace_scoped_baseline(stubs):
    """The successor of the two exempt-set tasks: one row per namespace-scoped lever."""
    sets = _exempt_sets(docs(verify_render(stubs)))
    assert sets == [
        {
            "namespace": "homelab",
            "variable": "netpol_baseline_exempt_workloads",
            "apps": sorted(_context(stubs)["netpol_baseline_exempt_workloads"]),
        },
        {
            "namespace": "observability",
            "variable": "netpol_baseline_obs_exempt_workloads",
            "apps": [],
        },
    ]
    assert "registry" in sets[0]["apps"]


def test_a_label_scoped_baseline_drops_its_check_and_its_pod_grant(stubs):
    rendered = docs(verify_render(stubs, {"netpol_baseline_obs_scope": "label"}))
    assert [row["namespace"] for row in _exempt_sets(rendered)] == ["homelab"]
    observability = next(
        doc
        for doc in rendered
        if doc["kind"] == "Role" and doc["metadata"]["namespace"] == "observability"
    )
    assert [rule["resources"] for rule in observability["rules"]] == [
        ["endpointslices"]
    ]


def _legs_container(docs) -> dict:
    return job(docs)["spec"]["template"]["spec"]["containers"][0]


def _dialled_namespaces(stubs, levers) -> set[str]:
    """The namespaces of the URLs the legs container dials under `levers`."""
    rows = json.loads(
        env_of(_legs_container(docs(verify_render(stubs, levers))))["TARGETS"]
    )
    return {
        namespace
        for row in rows
        for namespace in re.findall(r"\.([\w-]+)\.svc\.cluster\.local", row["url"])
    }


@pytest.mark.parametrize(
    ("levers", "namespaces"),
    [
        ({}, {"homelab", "observability", "flux-system"}),
        ({"netpol_baseline_enforced": False}, {"observability", "flux-system"}),
        ({"netpol_baseline_obs_enforced": False}, {"homelab", "flux-system"}),
        (
            {"netpol_baseline_enforced": False, "netpol_baseline_obs_enforced": False},
            {"flux-system"},
        ),
    ],
    ids=["both-on", "homelab-off", "observability-off", "both-off"],
)
def test_each_lever_drops_only_its_own_plane(stubs, levers, namespaces):
    """The flux role owns flux-system's fence, so neither lever of this role turns it off."""
    assert _dialled_namespaces(stubs, levers) == namespaces


def test_a_service_port_row_dials_the_service_port(stubs):
    """A flux Service maps port 80 to another pod port, and a leg on the pod port reaches nothing."""
    defaults = yaml_fast.safe_load(
        (K8S_ROLES / TAG / "defaults" / "main.yml").read_text()
    )
    rows = [r for r in defaults["netpol_baseline_probe_targets"] if "service_port" in r]
    assert {r["service"] for r in rows} >= {"source-controller"}
    dialled = {
        row["url"]
        for row in json.loads(
            env_of(_legs_container(docs(verify_render(stubs))))["TARGETS"]
        )
    }
    for row in rows:
        host = f"http://{row['service']}.flux-system.svc.cluster.local"
        assert f"{host}:{row['service_port']}/" in dialled, row
        assert f"{host}:{row['port']}/" not in dialled, row
