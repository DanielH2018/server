"""prowlarr's app-keys seed: it writes only a key Prowlarr rejects, and its Job reruns on a rotation.

The run tests write out the rendered ConfigMap's two files, the seed and its
`flux_job_connect.py` copy, and run them as the Job's container does, against a local server
that keeps applications the way Prowlarr's API does: it masks `apiKey` on a read, keeps the
stored key for a masked write, and tests an entry with its stored key.

Run: uv run pytest k8s/roles/prowlarr/tests/test_seed_prowlarr_app_keys.py
"""

import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import seed_prowlarr_app_keys as seed
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from lib.repo_paths import SECRETS
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "prowlarr"
APPS = ("sonarr", "radarr", "lidarr")
MASK = "********"
PROWLARR_KEY = "prowlarr-key"
# The key each app accepts, as its `<app>-exportarr` Secret holds it.
KEYS = {app.capitalize(): f"{app}-key" for app in APPS}
KEY_FAILURE = {"propertyName": "ApiKey", "errorMessage": "API Key is invalid"}
URL_FAILURE = {"propertyName": "BaseUrl", "errorMessage": "Unable to complete test"}


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _docs(rendered: cluster.RenderedTag) -> list[dict]:
    return [
        doc
        for name, text in sorted(rendered.verify.items())
        if name != "kustomization.yaml"
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


@pytest.fixture(scope="module")
def verify_docs(stubs) -> list[dict]:
    return _docs(cluster.render_tag(TAG, stubs))


def _job(docs: list[dict]) -> dict:
    (job,) = [
        doc
        for doc in docs
        if doc["kind"] == "Job"
        and doc["metadata"]["name"].startswith("prowlarr-app-keys-")
    ]
    return job


def _container(docs: list[dict]) -> dict:
    (container,) = _job(docs)["spec"]["template"]["spec"]["containers"]
    return container


def _secret_refs(docs: list[dict]) -> dict[str, tuple[str, str]]:
    return {
        e["name"]: (ref["name"], ref["key"])
        for e in _container(docs)["env"]
        if (ref := (e.get("valueFrom") or {}).get("secretKeyRef"))
    }


def test_key_is_rejected_on_an_api_key_failure():
    assert seed.key_is_rejected([URL_FAILURE, KEY_FAILURE])


def test_key_is_rejected_on_no_other_failure():
    assert not seed.key_is_rejected([URL_FAILURE])


def test_find_application_takes_the_one_entry_of_its_kind():
    entries = [
        {"id": 1, "implementation": "Sonarr"},
        {"id": 2, "implementation": "Radarr"},
    ]
    assert seed.find_application(entries, "Radarr")["id"] == 2


@pytest.mark.parametrize("count", [0, 2])
def test_find_application_refuses_none_or_two(count):
    entries = [{"id": n, "implementation": "Sonarr"} for n in range(count)]
    with pytest.raises(seed.SeedError, match=f"{count} Sonarr applications"):
        seed.find_application(entries, "Sonarr")


def test_the_seed_job_passes_every_job_census_row(verify_docs):
    _job(verify_docs)
    assert problems({f"{TAG}-verify": verify_docs}) == []


def test_the_job_reads_each_key_from_its_apps_exportarr_secret(verify_docs, stubs):
    """Each Secret is one the named app's own render applies, filled from `<app>_api_key`."""
    refs = _secret_refs(verify_docs)
    assert refs == {
        f"{app.upper()}_API_KEY": (f"{app}-exportarr", "api_key")
        for app in (TAG, *APPS)
    }
    for app in (TAG, *APPS):
        (secret,) = [
            doc
            for text in cluster.render_tag(app, stubs).files.values()
            for doc in yaml_fast.safe_load_all(text)
            if doc
            and doc.get("kind") == "Secret"
            and doc["metadata"]["name"] == f"{app}-exportarr"
        ]
        assert secret["stringData"]["api_key"] == stubs[f"{app}_api_key"]


def test_prowlarr_verify_waits_on_every_app_whose_secret_the_job_reads(verify_docs):
    """`flux_verify_after` holds the Job until each app's Secret is applied and its pod rolled."""
    read = {
        name.removesuffix("-exportarr")
        for name, _key in _secret_refs(verify_docs).values()
    }
    waits = set(cluster.bundle_ordering().verify_after.get(TAG, ()))
    assert read - {TAG} <= waits


def _render_tree_copy(
    tree, stubs, rotate: str | None = None, edit_deployment: bool = False
) -> list[dict]:
    """`prowlarr-verify` rendered from a copy of the roles it reads and `cluster-vars.source`.

    With `rotate`, that key's ciphertext digest is replaced first, as a rotation replaces it.
    With `edit_deployment`, prowlarr's Deployment template gains a line.
    """
    for role in (TAG, "arr-notification"):
        shutil.copytree(K8S_ROLES / role, tree / "k8s" / "roles" / role)
    if edit_deployment:
        deployment = tree / "k8s" / "roles" / TAG / "templates" / "deployment.yaml.j2"
        deployment.write_text(deployment.read_text() + "# a Deployment-only edit\n")
    (tree / "secrets").mkdir()
    source = tree / "secrets" / "cluster-vars.source"
    shutil.copy(SECRETS / "cluster-vars.source", source)
    if rotate:
        lines = source.read_text().splitlines()
        (row,) = [n for n, line in enumerate(lines) if line.startswith(f"{rotate} ")]
        key, _digest, *rest = lines[row].split()
        lines[row] = " ".join([key, "0" * 64, *rest])
        source.write_text("\n".join(lines) + "\n")
    return _docs(cluster.render_tag(TAG, stubs, roles=tree / "k8s" / "roles"))


@pytest.mark.parametrize("app", APPS)
def test_a_key_rotation_alone_renames_the_seed_job(stubs, tmp_path, app):
    """A `secretKeyRef` moves no spec, so the digest is what makes Flux run the seed again."""
    before = _job(_render_tree_copy(tmp_path / "a", stubs))
    after = _job(_render_tree_copy(tmp_path / "b", stubs, rotate=f"{app}_api_key"))
    assert before["metadata"]["name"] != after["metadata"]["name"]


def test_a_prowlarr_deployment_change_renames_the_seed_job(stubs, tmp_path):
    """A revert leaves no render change, so the next revision that rolls prowlarr reruns the seed."""
    before = _job(_render_tree_copy(tmp_path / "a", stubs))
    after = _job(_render_tree_copy(tmp_path / "b", stubs, edit_deployment=True))
    assert before["metadata"]["name"] != after["metadata"]["name"]


class _Prowlarr:
    """A local server keeping applications as Prowlarr's API does, and counting writes."""

    def __init__(self, stored: dict[str, str], failure: dict | None = None):
        self.rows: list[dict] = [
            {
                "id": n,
                "name": implementation,
                "implementation": implementation,
                "enable": True,
                "fields": [
                    {"name": "baseUrl", "value": f"http://{implementation.lower()}:1"},
                    {"name": "apiKey", "value": stored[implementation]},
                    {"name": "authPassword", "value": "secret"},
                ],
            }
            for n, implementation in enumerate(stored, start=1)
        ]
        self.writes: list[int] = []
        state = self

        def masked(row):
            row = json.loads(json.dumps(row))
            for field in row["fields"]:
                if field["name"] in ("apiKey", "authPassword"):
                    field["value"] = MASK
            return row

        def unmasked(body):
            (row,) = [r for r in state.rows if r["id"] == body["id"]]
            stored = {f["name"]: f["value"] for f in row["fields"]}
            body = json.loads(json.dumps(body))
            for field in body["fields"]:
                if field["value"] == MASK:
                    field["value"] = stored[field["name"]]
            return body

        def key_of(row):
            return next(f["value"] for f in row["fields"] if f["name"] == "apiKey")

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, code, body):
                payload = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _body(self):
                return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

            def _test(self, row):
                if failure:
                    return [failure]
                return (
                    [] if key_of(row) == KEYS[row["implementation"]] else [KEY_FAILURE]
                )

            def do_GET(self):
                if self.headers.get("X-Api-Key") != PROWLARR_KEY:
                    self._answer(401, {})
                elif self.path == "/api/v1/applications":
                    self._answer(200, [masked(r) for r in state.rows])
                else:
                    self._answer(404, {})

            def do_POST(self):
                if self.path != "/api/v1/applications/test":
                    self._answer(404, {})
                    return
                failures = self._test(unmasked(self._body()))
                self._answer(400 if failures else 200, failures or {})

            def do_PUT(self):
                row = unmasked(self._body())
                if failures := self._test(row):
                    self._answer(400, failures)
                    return
                state.rows = [row if r["id"] == row["id"] else r for r in state.rows]
                state.writes.append(row["id"])
                self._answer(202, masked(row))

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _run(docs, directory, prowlarr: _Prowlarr) -> subprocess.CompletedProcess:
    """The rendered seed, run with the Job's own environment pointed at `prowlarr`."""
    (volume,) = _job(docs)["spec"]["template"]["spec"]["volumes"]
    (configmap,) = [
        doc
        for doc in docs
        if doc["kind"] == "ConfigMap"
        and doc["metadata"]["name"] == volume["configMap"]["name"]
    ]
    for key, text in configmap["data"].items():
        (directory / key).write_text(text)
    env = {e["name"]: e["value"] for e in _container(docs)["env"] if "value" in e}
    env |= {f"{app.upper()}_API_KEY": key for app, key in KEYS.items()}
    env |= {
        "PROWLARR_URL": f"http://127.0.0.1:{prowlarr.server.server_address[1]}",
        "PROWLARR_API_KEY": PROWLARR_KEY,
    }
    return subprocess.run(
        [sys.executable, "-E", "-s", str(directory / "seed_prowlarr_app_keys.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_second_run_writes_nothing(verify_docs, tmp_path):
    """`homelab/rerun-safe` names this test: stale keys are written once, then left alone."""
    prowlarr = _Prowlarr(
        {"Sonarr": "stale", "Radarr": "stale", "Lidarr": KEYS["Lidarr"]}
    )
    try:
        first = _run(verify_docs, tmp_path, prowlarr)
        second = _run(verify_docs, tmp_path, prowlarr)
    finally:
        prowlarr.close()
    assert first.returncode == 0, first.stderr
    assert first.stdout.splitlines() == [
        "prowlarr applications: 2 keys written (Sonarr, Radarr), 1 unchanged"
    ]
    assert second.returncode == 0, second.stderr
    assert second.stdout.splitlines() == [
        "prowlarr applications: 0 keys written, 3 unchanged"
    ]
    assert prowlarr.writes == [1, 2]
    fields = {
        r["implementation"]: {f["name"]: f["value"] for f in r["fields"]}
        for r in prowlarr.rows
    }
    assert {app: f["apiKey"] for app, f in fields.items()} == KEYS
    assert {f["authPassword"] for f in fields.values()} == {"secret"}
    output = first.stdout + first.stderr + second.stdout + second.stderr
    assert not any(key in output for key in KEYS.values())


def test_a_failure_other_than_the_key_writes_nothing(verify_docs, tmp_path):
    prowlarr = _Prowlarr({app: "stale" for app in KEYS}, failure=URL_FAILURE)
    try:
        result = _run(verify_docs, tmp_path, prowlarr)
    finally:
        prowlarr.close()
    assert result.returncode != 0
    assert "Unable to complete test" in result.stderr
    assert prowlarr.writes == []


def test_a_missing_application_fails_the_job(verify_docs, tmp_path):
    prowlarr = _Prowlarr({"Sonarr": KEYS["Sonarr"], "Radarr": KEYS["Radarr"]})
    try:
        result = _run(verify_docs, tmp_path, prowlarr)
    finally:
        prowlarr.close()
    assert result.returncode != 0
    assert "0 Lidarr applications" in result.stderr
    assert prowlarr.writes == []
