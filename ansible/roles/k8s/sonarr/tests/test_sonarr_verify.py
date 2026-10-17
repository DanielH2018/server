"""sonarr's inert verify Job: its script fails each broken library, and its render passes the census.

The render tests copy the sonarr role, move `templates/verify-inert/` to `templates/verify/` as
the flip PR will, and render the copy, so the Job is checked as Flux will receive it while the
tree's own render still emits no `sonarr-verify`. The script tests run the rendered ConfigMap's
two files, the script and its `flux_job_connect.py` copy, as the Job's container runs them,
against a local HTTP server that answers like sonarr's API. The plan is
docs/target-architecture/deploy/d5-bulk-move.md, *A verify conversion lands as inert code*.

Run: uv run pytest ansible/roles/k8s/sonarr/tests/test_sonarr_verify.py
"""

import json
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "sonarr"
# A path into a role's tree, as any lookup names it; group 1 is the role.
ROLE_PATH = re.compile(r"/roles/k8s/([^/'\"]+)/")
KEY = "fixture-key"
GOOD_FOLDERS = [{"path": "/data/media/tv", "accessible": True, "freeSpace": 2 * 2**40}]
GOOD_SERIES = [
    {"path": "/data/media/tv/Show", "statistics": {"sizeOnDisk": 5 * 2**30}},
    {"path": "/data/media/tv/Other", "statistics": {"sizeOnDisk": 0}},
]


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _flip(roles, stubs, overrides=None) -> list[dict]:
    """The `sonarr-verify` documents a copy of the role renders once its Job leaves `verify-inert/`."""
    shutil.copytree(K8S_ROLES / TAG, roles / TAG)
    templates = roles / TAG / "templates"
    (templates / "verify-inert").rename(templates / "verify")
    rendered = cluster.render_tag(TAG, {**stubs, **(overrides or {})}, roles=roles)
    return [
        doc
        for name, text in sorted(rendered.verify.items())
        if name != "kustomization.yaml"
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


@pytest.fixture(scope="module")
def flipped(stubs, tmp_path_factory) -> list[dict]:
    return _flip(tmp_path_factory.mktemp("roles"), stubs)


def _job(docs: list[dict]) -> dict:
    """The verify Job; arr-notification's seed Job shares `sonarr-verify` with it."""
    (job,) = [
        doc
        for doc in docs
        if doc["kind"] == "Job"
        and doc["metadata"]["annotations"]["homelab/job-kind"] == "verify"
    ]
    return job


@pytest.fixture(scope="module")
def script_dir(flipped, tmp_path_factory):
    """The rendered ConfigMap's files, written out as the Job's volume mounts them."""
    directory = tmp_path_factory.mktemp("verify")
    configmap = next(
        doc
        for doc in flipped
        if doc["kind"] == "ConfigMap" and "sonarr_verify.py" in doc["data"]
    )
    for key, text in configmap["data"].items():
        (directory / key).write_text(text)
    return directory


def _serve(folders, series, status=200):
    """A local server that answers `rootfolder` and `series` as sonarr's API does."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            bodies = {"/api/v3/rootfolder": folders, "/api/v3/series": series}
            if self.headers.get("X-Api-Key") != KEY:
                code, body = 401, {"message": "Unauthorized"}
            elif self.path not in bodies:
                code, body = 404, {}
            else:
                code, body = status, bodies[self.path]
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run(script_dir, folders=GOOD_FOLDERS, series=GOOD_SERIES, key=KEY, status=200):
    server = _serve(folders, series, status)
    try:
        env = {
            "SONARR_URL": f"http://127.0.0.1:{server.server_address[1]}",
            "SONARR_API_KEY": key,
            "SONARR_DEPLOYMENT_DIGEST8": "abcd1234",
        }
        return subprocess.run(
            [sys.executable, "-E", "-s", str(script_dir / "sonarr_verify.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()


def test_an_intact_library_passes(script_dir):
    result = _run(script_dir)
    assert result.returncode == 0, result.stderr
    assert "root folders resolve inside the pod: /data/media/tv" in result.stdout
    assert "2 series, every path under /data, and 5.0 GiB" in result.stdout
    assert KEY not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("folders", "series", "message"),
    [
        ([], GOOD_SERIES, "sonarr reports no root folder"),
        (
            [{**GOOD_FOLDERS[0], "accessible": False}],
            GOOD_SERIES,
            "root folders that do not resolve in the pod: ['/data/media/tv']",
        ),
        (
            [{**GOOD_FOLDERS[0], "path": "/tv"}],
            GOOD_SERIES,
            "root folders that do not resolve in the pod: ['/tv']",
        ),
        (
            [{"path": "/data/media/tv", "accessible": True}],
            GOOD_SERIES,
            "root folders that do not resolve in the pod",
        ),
        (
            GOOD_FOLDERS,
            [*GOOD_SERIES, {"path": "/tv/Old", "statistics": {"sizeOnDisk": 1}}],
            "these paths sit outside /data: ['/tv/Old']",
        ),
        (
            GOOD_FOLDERS,
            [{**s, "statistics": {"sizeOnDisk": 0}} for s in GOOD_SERIES],
            "none has an episode file on disk",
        ),
        (GOOD_FOLDERS, [], "0 series, and none has an episode file on disk"),
    ],
    ids=[
        "no-root-folder",
        "inaccessible",
        "outside-data",
        "no-free-space",
        "series-outside-data",
        "empty-on-disk",
        "no-series",
    ],
)
def test_a_broken_library_fails_the_job(script_dir, folders, series, message):
    result = _run(script_dir, folders=folders, series=series)
    assert result.returncode == 1
    assert message in result.stderr


def test_a_rejected_key_fails_the_job_without_printing_it(script_dir):
    result = _run(script_dir, key="wrong-key")
    assert result.returncode == 1
    assert "GET /api/v3/rootfolder answered HTTP 401" in result.stderr
    assert "wrong-key" not in result.stdout + result.stderr


def test_an_answer_that_is_not_a_list_fails_the_job(script_dir):
    result = _run(script_dir, folders={"message": "starting"})
    assert result.returncode == 1
    assert "answered with JSON that is not a list" in result.stderr


def test_the_tree_renders_no_sonarr_verify_while_ansible_owns_sonarr(stubs):
    assert cluster.render_tag(TAG, stubs).verify == {}


def test_the_flipped_job_passes_every_job_census_row(flipped):
    assert sorted(doc["kind"] for doc in flipped) == [
        "ConfigMap",
        "ConfigMap",
        "Job",
        "Job",
        "Secret",
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


def test_the_census_fails_a_script_that_dials_without_the_helper(flipped):
    """The red case for the helper row: the script's own `urlopen` is refused."""
    docs = [dict(doc) for doc in flipped]
    index = next(
        i
        for i, doc in enumerate(docs)
        if doc["kind"] == "ConfigMap" and "sonarr_verify.py" in doc["data"]
    )
    data = dict(docs[index]["data"])
    data["sonarr_verify.py"] += "\nimport urllib.request\nurllib.request.urlopen('x')\n"
    docs[index] = {**docs[index], "data": data}
    found = problems({f"{TAG}-verify": docs})
    assert any("import urllib.request" in line for line in found)


def test_the_flipped_job_reads_sonarrs_service_with_sonarrs_key(flipped, stubs):
    pod = _job(flipped)["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    env = {e["name"]: e for e in container["env"]}
    assert env["SONARR_URL"]["value"] == "http://sonarr.homelab.svc.cluster.local:8989"
    assert env["SONARR_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "sonarr-exportarr",
        "key": "api_key",
    }
    assert pod["automountServiceAccountToken"] is False
    assert _job(flipped)["spec"]["template"]["metadata"]["labels"] == {
        "app": "sonarr-verify"
    }
    flux = yaml_fast.safe_load((K8S_ROLES / "flux/defaults/main.yml").read_text())
    assert container["image"] == flux["flux_job_image"]


def _secret_refs(job: dict) -> set[tuple[str, str]]:
    """Each `(Secret name, key)` pair the Job's containers read by `secretKeyRef`."""
    pod = job["spec"]["template"]["spec"]
    return {
        (ref["name"], ref["key"])
        for container in pod["containers"]
        for env in container.get("env") or []
        if (ref := (env.get("valueFrom") or {}).get("secretKeyRef"))
    }


def _secret_keys(stubs) -> set[tuple[str, str]]:
    """Each `(Secret name, key)` pair sonarr's own render applies."""
    return {
        (doc["metadata"]["name"], key)
        for text in cluster.render_tag(TAG, stubs).files.values()
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc.get("kind") == "Secret"
        for key in {**(doc.get("data") or {}), **(doc.get("stringData") or {})}
    }


def test_sonarrs_own_render_holds_the_secret_the_job_reads(flipped, stubs):
    """Every Secret key the Job reads is one sonarr's render applies, or its pod cannot start."""
    refs = _secret_refs(_job(flipped))
    assert refs, "the flipped Job reads no Secret, so this check reads nothing"
    assert refs - _secret_keys(stubs) == set()


def test_a_secret_sonarr_does_not_render_is_flagged(flipped, stubs):
    """The red case: a Job pointed at a Secret sonarr never applies fails the check above."""
    job = json.loads(json.dumps(_job(flipped)))
    (container,) = job["spec"]["template"]["spec"]["containers"]
    for env in container["env"]:
        if "valueFrom" in env:
            env["valueFrom"]["secretKeyRef"] = {"name": "sonarr-api", "key": "key"}
    assert _secret_refs(job) - _secret_keys(stubs) == {("sonarr-api", "key")}


def test_a_deployment_change_renames_the_job(stubs, tmp_path):
    """A new sonarr image changes the Deployment, so Flux runs the renamed Job again."""
    names = [
        _job(_flip(tmp_path / str(n), stubs, {"sonarr_k8s_image": image}))["metadata"][
            "name"
        ]
        for n, image in enumerate(
            ("lscr.io/linuxserver/sonarr:4.0.20", "lscr.io/linuxserver/sonarr:4.0.21")
        )
    ]
    assert names[0] != names[1]


def test_the_staged_job_reads_no_other_roles_file():
    """The helper and the image come from `render_tag`, never a lookup into another role.

    The deployer redeploys a role when a file it `lookup()`s in another role changes, and its
    scan reads `verify-inert/` too. tdarr's test has the same guard and its red case.
    `notification.yaml.j2` is arr-notification's seed Job, and its lookups into that role are
    deliberate: arr-notification's own test_seed_job.py pins them.
    """
    staged = sorted((K8S_ROLES / TAG / "templates" / "verify-inert").glob("*.j2"))
    assert [p.name for p in staged] == [
        "job.yaml.j2",
        "notification.yaml.j2",
        "rbac.yaml.j2",
    ]
    staged = [p for p in staged if p.name != "notification.yaml.j2"]
    assert [
        (path.name, role)
        for path in staged
        for role in ROLE_PATH.findall(path.read_text())
        if role != TAG
    ] == []
