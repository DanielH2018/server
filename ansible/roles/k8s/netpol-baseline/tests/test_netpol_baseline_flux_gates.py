"""The Flux forms of netpol-baseline's lever asserts and live exempt-set gates (#5685).

`tasks/main.yml` runs both before its apply, and neither is part of the probe. Under Flux no
task runs.

- The lever asserts become a check over the render context: the variables
  `renderer_lib.cluster.tag_context` resolves for the tag, the context Flux's render reads.
  Flux takes no `-e`, so the repo's defaults and inventory are every value a lever can hold,
  and CI runs this before a typo can merge. The predicate is the Ansible assert's.
- The exempt-set gates become the `exempt` check of the verify Job's `gate` initContainer,
  which runs after the apply. The script tests run it against fake API reads; the render
  tests read the flipped Job.

Run: uv run pytest ansible/roles/k8s/netpol-baseline/tests/test_netpol_baseline_flux_gates.py
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from _netpol_baseline_flipped import TAG, docs, env_of, flipped_render, job, write_stubs

import netpol_baseline_verify as verify
from lib.k8s_roles import K8S_ROLES
from renderer.renderer_lib import cluster

BOOL_LEVERS = ("netpol_baseline_enforced", "netpol_baseline_obs_enforced")
SCOPE_LEVERS = ("netpol_baseline_scope", "netpol_baseline_obs_scope")


def lever_problems(ctx: dict) -> list[str]:
    """The levers in `ctx` that the "Check the netpol-baseline levers" assert would refuse."""
    return [
        name for name in BOOL_LEVERS if str(ctx[name]).lower() not in ("true", "false")
    ] + [name for name in SCOPE_LEVERS if ctx[name] not in ("label", "namespace")]


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    return write_stubs(tmp_path_factory)


def _context(stubs, overrides=None) -> dict:
    return cluster.tag_context(K8S_ROLES / TAG, TAG, {**stubs, **(overrides or {})})


def test_the_rendered_levers_are_clean(stubs):
    ctx = _context(stubs)
    assert {name: ctx[name] for name in BOOL_LEVERS + SCOPE_LEVERS} == {
        "netpol_baseline_enforced": True,
        "netpol_baseline_obs_enforced": True,
        "netpol_baseline_scope": "namespace",
        "netpol_baseline_obs_scope": "namespace",
    }
    assert lever_problems(ctx) == []


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


def test_the_flipped_job_checks_each_namespace_scoped_baseline(stubs, tmp_path):
    """The successor of the two exempt-set tasks: one row per namespace-scoped lever."""
    sets = _exempt_sets(docs(flipped_render(stubs, tmp_path)))
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


def test_a_label_scoped_baseline_drops_its_check_and_its_pod_grant(stubs, tmp_path):
    rendered = docs(
        flipped_render(stubs, tmp_path, {"netpol_baseline_obs_scope": "label"})
    )
    assert [row["namespace"] for row in _exempt_sets(rendered)] == ["homelab"]
    observability = next(
        doc
        for doc in rendered
        if doc["kind"] == "Role" and doc["metadata"]["namespace"] == "observability"
    )
    assert [rule["resources"] for rule in observability["rules"]] == [
        ["endpointslices"]
    ]
