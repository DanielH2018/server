"""prowlarr's app-keys seed: it writes only a key Prowlarr rejects, and reruns hourly and on a rotation.

The run tests write out the rendered ConfigMap's two files, the seed and its
`flux_job_connect.py` copy, and run them as the Job's container does, against a local server
that keeps applications the way Prowlarr's API does: it masks `apiKey` on a read, keeps the
stored key for a masked write, and tests an entry with its stored key.

Run: uv run pytest k8s/roles/prowlarr/tests/test_seed_prowlarr_app_keys.py
"""

import json
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import seed_prowlarr_app_keys as seed
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from lib.repo_paths import REPO, SECRETS
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.renderer_lib.cronjob_gate import GATE_INFIX, RERUN_SAFE, attach_gates
from renderer.tests._job_census import problems

TAG = "prowlarr"
VERIFY = f"{TAG}-verify"
CRONJOB = "prowlarr-app-keys"
LONG_TERM_STATUS_HOURS = 6
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


def _docs(texts: dict[str, str]) -> list[dict]:
    return [
        doc
        for name, text in sorted(texts.items())
        if name != "kustomization.yaml"
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


def _render(stubs, roles=K8S_ROLES) -> dict[str, list[dict]]:
    """`cluster/prowlarr/` and `cluster/prowlarr-verify/` with the gate Jobs attached."""
    (tag,) = attach_gates([cluster.render_tag(TAG, stubs, roles=roles)], [TAG], roles)
    return {TAG: _docs(tag.files), VERIFY: _docs(tag.verify)}


@pytest.fixture(scope="module")
def bundle(stubs) -> dict[str, list[dict]]:
    return _render(stubs)


def _job(bundle: dict[str, list[dict]]) -> dict:
    (job,) = [
        doc
        for doc in bundle[VERIFY]
        if doc["kind"] == "Job"
        and doc["metadata"]["name"].startswith(f"{CRONJOB}{GATE_INFIX}")
    ]
    return job


def _cronjob(bundle: dict[str, list[dict]]) -> dict:
    (cronjob,) = [
        doc
        for doc in bundle[TAG]
        if doc["kind"] == "CronJob" and doc["metadata"]["name"] == CRONJOB
    ]
    return cronjob


def _container(bundle: dict[str, list[dict]]) -> dict:
    (container,) = _job(bundle)["spec"]["template"]["spec"]["containers"]
    return container


def _secret_refs(bundle: dict[str, list[dict]]) -> dict[str, tuple[str, str]]:
    return {
        e["name"]: (ref["name"], ref["key"])
        for e in _container(bundle)["env"]
        if (ref := (e.get("valueFrom") or {}).get("secretKeyRef"))
    }


def _failures(*rows: dict) -> list[seed.Failure]:
    failures = seed.parse_failures(json.dumps(list(rows)))
    assert failures is not None
    return failures


def test_key_is_rejected_on_an_api_key_failure():
    assert seed.key_is_rejected(_failures(URL_FAILURE, KEY_FAILURE))


def test_key_is_rejected_on_no_other_failure():
    assert not seed.key_is_rejected(_failures(URL_FAILURE))


def test_find_application_takes_the_one_entry_of_its_kind():
    entries = seed.parse_applications(
        [{"id": 1, "implementation": "Sonarr"}, {"id": 2, "implementation": "Radarr"}]
    )
    assert seed.find_application(entries, "Radarr").id == 2


def test_an_applications_answer_that_is_no_list_is_refused():
    with pytest.raises(seed.SeedError, match="answered no list"):
        seed.parse_applications(None)


@pytest.mark.parametrize("count", [0, 2])
def test_find_application_refuses_none_or_two(count):
    entries = seed.parse_applications(
        [{"id": n, "implementation": "Sonarr"} for n in range(count)]
    )
    with pytest.raises(seed.SeedError, match=f"{count} Sonarr applications"):
        seed.find_application(entries, "Sonarr")


def test_the_seed_gate_passes_every_job_census_row(bundle):
    _job(bundle)
    assert problems(bundle) == []


def test_the_gate_is_a_rerun_safe_mutation(bundle):
    assert _job(bundle)["metadata"]["annotations"] == {
        "homelab/job-kind": "mutation",
        RERUN_SAFE: "k8s/roles/prowlarr/tests/test_seed_prowlarr_app_keys.py"
        "::test_a_second_run_writes_nothing",
    }


def _period_hours(schedule: str) -> int | None:
    """The hours between runs of `schedule`, or None for a shape with no fixed period.

    Only `M * * * *` and `M */N * * *` have one; a fixed hour or a list runs at set times.
    """
    match = re.fullmatch(r"\d+ \*(?:/(\d+))? \* \* \*", schedule)
    return int(match[1] or 1) if match else None


@pytest.mark.parametrize(
    ("schedule", "period"),
    [
        ("23 * * * *", 1),
        ("23 */4 * * *", 4),
        ("23 2 * * *", None),
        ("23 0,12 * * *", None),
        ("23 * * * 0", None),
    ],
)
def test_period_hours_reads_only_a_fixed_period(schedule, period):
    assert _period_hours(schedule) == period


def test_the_seed_reruns_on_a_schedule_inside_the_long_term_status_window(bundle):
    """A revert changes no render, so only a scheduled run rewrites the key it restored."""
    schedule = _cronjob(bundle)["spec"]["schedule"]
    period = _period_hours(schedule)
    assert period is not None, f"{schedule!r} has no fixed period"
    assert period < LONG_TERM_STATUS_HOURS


def test_the_job_reads_each_key_from_its_apps_exportarr_secret(bundle, stubs):
    """Each Secret is one the named app's own render applies, filled from `<app>_api_key`."""
    refs = _secret_refs(bundle)
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


def test_prowlarr_verify_waits_on_every_app_whose_secret_the_job_reads(bundle):
    """`flux_verify_after` holds the Job until each app's Secret is applied and its pod rolled."""
    read = {
        name.removesuffix("-exportarr") for name, _key in _secret_refs(bundle).values()
    }
    waits = set(cluster.bundle_ordering().verify_after.get(TAG, ()))
    assert read - {TAG} <= waits


def _render_tree_copy(
    tree, stubs, rotate: str | None = None, edit_deployment: bool = False
) -> dict[str, list[dict]]:
    """`_render` over a copy of the roles it reads and `cluster-vars.source`.

    With `rotate`, that key's ciphertext digest is replaced first, as a rotation replaces it.
    With `edit_deployment`, prowlarr's Deployment renders another log level.
    """
    for role in (TAG, "arr-notification"):
        shutil.copytree(K8S_ROLES / role, tree / "k8s" / "roles" / role)
    helper = tree / cluster.FLUX_JOB_CONNECT.relative_to(REPO)
    helper.parent.mkdir(parents=True)
    shutil.copy(cluster.FLUX_JOB_CONNECT, helper)
    if edit_deployment:
        deployment = tree / "k8s" / "roles" / TAG / "templates" / "deployment.yaml.j2"
        text = deployment.read_text()
        assert "{{ prowlarr_k8s_log_level }}" in text
        deployment.write_text(text.replace("{{ prowlarr_k8s_log_level }}", "trace"))
    (tree / "secrets").mkdir()
    source = tree / "secrets" / "cluster-vars.source"
    shutil.copy(SECRETS / "cluster-vars.source", source)
    if rotate:
        lines = source.read_text().splitlines()
        (row,) = [n for n, line in enumerate(lines) if line.startswith(f"{rotate} ")]
        key, _digest, *rest = lines[row].split()
        lines[row] = " ".join([key, "0" * 64, *rest])
        source.write_text("\n".join(lines) + "\n")
    return _render(stubs, roles=tree / "k8s" / "roles")


@pytest.mark.parametrize("app", APPS)
def test_a_key_rotation_alone_renames_the_seed_job(stubs, tmp_path, app):
    """A `secretKeyRef` moves no spec, so the digest is what makes Flux run the seed again."""
    before = _job(_render_tree_copy(tmp_path / "a", stubs))
    after = _job(_render_tree_copy(tmp_path / "b", stubs, rotate=f"{app}_api_key"))
    assert before["metadata"]["name"] != after["metadata"]["name"]


def test_a_prowlarr_deployment_change_renames_the_seed_job(stubs, tmp_path):
    """The gate's role digest covers the Deployment, so a revision that rolls prowlarr reseeds."""
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
        for doc in docs[TAG]
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


def test_a_second_run_writes_nothing(bundle, tmp_path):
    """`homelab/rerun-safe` names this test: stale keys are written once, then left alone."""
    prowlarr = _Prowlarr(
        {"Sonarr": "stale", "Radarr": "stale", "Lidarr": KEYS["Lidarr"]}
    )
    try:
        first = _run(bundle, tmp_path, prowlarr)
        second = _run(bundle, tmp_path, prowlarr)
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


def test_a_failure_other_than_the_key_writes_nothing(bundle, tmp_path):
    prowlarr = _Prowlarr({app: "stale" for app in KEYS}, failure=URL_FAILURE)
    try:
        result = _run(bundle, tmp_path, prowlarr)
    finally:
        prowlarr.close()
    assert result.returncode != 0
    assert "Unable to complete test" in result.stderr
    assert prowlarr.writes == []


def test_a_missing_application_fails_the_job(bundle, tmp_path):
    prowlarr = _Prowlarr({"Sonarr": KEYS["Sonarr"], "Radarr": KEYS["Radarr"]})
    try:
        result = _run(bundle, tmp_path, prowlarr)
    finally:
        prowlarr.close()
    assert result.returncode != 0
    assert "0 Lidarr applications" in result.stderr
    assert prowlarr.writes == []
