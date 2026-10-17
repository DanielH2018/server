"""arr-notification's seed as an inert Job in sonarr-verify and radarr-verify (target slice D5).

The render tests copy each app's role, move `templates/verify-inert/` to `templates/verify/` as
the flip PR will, and render the copy, so the Job is checked as Flux will receive it while the
tree's own render still emits no `<app>-verify`. The run tests write out the rendered
ConfigMap's two files, the seed and its `flux_job_connect.py` copy, and run them as the Job's
container does, against a local server that keeps notifications the way the *arr API does.
The plan is docs/target-architecture/deploy/d5-bulk-move.md, *arr-notification's seed*.

Run: uv run pytest ansible/roles/k8s/arr-notification/tests/test_seed_job.py
"""

import json
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jinja2
import pytest

from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

APPS = ("sonarr", "radarr")
TEMPLATE = "templates/verify-inert/notification.yaml.j2"
DEFAULTS = yaml_fast.safe_load(
    (K8S_ROLES / "arr-notification/defaults/main.yml").read_text()
)
KEY = "fixture-key"
URL = "https://discord.example/api/webhooks/1/fixture"
# The Discord entry of `GET /api/v3/notification/schema`, cut to the keys the seed reads.
SCHEMA = {
    "implementation": "Discord",
    "configContract": "DiscordSettings",
    "presets": [{"name": "a preset"}],
    "includeHealthWarnings": False,
    "onGrab": False,
    "onDownload": False,
    "onHealthIssue": False,
    "onHealthRestored": False,
    "onApplicationUpdate": False,
    "supportsOnGrab": True,
    "fields": [
        {"order": 0, "name": "webHookUrl", "value": None},
        {"order": 1, "name": "username", "value": None},
        {"order": 4, "name": "grabFields", "value": [0, 1]},
    ],
}


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


@pytest.fixture(scope="module")
def flipped(stubs, tmp_path_factory) -> dict[str, list[dict]]:
    """Each app's `<app>-verify` documents, rendered from a copy whose Jobs left `verify-inert/`."""
    roles = tmp_path_factory.mktemp("roles")
    docs = {}
    for app in APPS:
        shutil.copytree(K8S_ROLES / app, roles / app)
        templates = roles / app / "templates"
        (templates / "verify-inert").rename(templates / "verify")
        rendered = cluster.render_tag(app, stubs, roles=roles)
        docs[app] = [
            doc
            for name, text in sorted(rendered.verify.items())
            if name != "kustomization.yaml"
            for doc in yaml_fast.safe_load_all(text)
            if doc
        ]
    return docs


def _seed(docs: list[dict]) -> dict:
    (job,) = [
        doc
        for doc in docs
        if doc["kind"] == "Job"
        and doc["metadata"]["annotations"]["homelab/job-kind"] == "mutation"
    ]
    return job


def _container(docs: list[dict]) -> dict:
    (container,) = _seed(docs)["spec"]["template"]["spec"]["containers"]
    return container


def test_the_two_apps_carry_the_same_template():
    """One file, byte for byte, so a fix to one app's seed Job cannot miss the other's."""
    sonarr, radarr = ((K8S_ROLES / app / TEMPLATE).read_bytes() for app in APPS)
    assert sonarr == radarr


@pytest.mark.parametrize("app", APPS)
def test_the_tree_renders_no_seed_job_while_ansible_owns_the_app(stubs, app):
    assert cluster.render_tag(app, stubs).verify == {}


@pytest.mark.parametrize("app", APPS)
def test_the_flipped_seed_job_passes_every_job_census_row(flipped, app):
    assert problems({f"{app}-verify": flipped[app]}) == []


@pytest.mark.parametrize("app", APPS)
def test_the_job_declares_what_the_ansible_seed_declares(flipped, app):
    """The non-secret inputs match what `tasks/main.yml` passes for the same app."""
    env = {e["name"]: e.get("value") for e in _container(flipped[app])["env"]}
    username = jinja2.Template(DEFAULTS["arr_notification_username"]).render(
        arr_notification_app=app
    )
    assert {k: env[k] for k in env if k.startswith("ARR_") and env[k] is not None} == {
        "ARR_APP": app,
        "ARR_HOST": f"{app}.homelab.svc.cluster.local",
        "ARR_NOTIFICATION_NAME": DEFAULTS["arr_notification_name"],
        "ARR_NOTIFICATION_USERNAME": username,
        "ARR_NOTIFICATION_TRIGGERS": json.dumps(DEFAULTS["arr_notification_triggers"]),
        "ARR_NOTIFICATION_INCLUDE_HEALTH_WARNINGS": "false",
        "ARR_DEPLOYMENT_DIGEST8": env["ARR_DEPLOYMENT_DIGEST8"],
    }


@pytest.mark.parametrize("app", APPS)
def test_every_secret_the_job_reads_is_rendered_before_it_runs(flipped, stubs, app):
    """The key from `<app>`'s own render, the webhook from a Secret in `<app>-verify`."""
    refs = {
        (e["name"], ref["name"], ref["key"])
        for e in _container(flipped[app])["env"]
        if (ref := (e.get("valueFrom") or {}).get("secretKeyRef"))
    }
    assert refs == {
        ("ARR_API_KEY", f"{app}-exportarr", "api_key"),
        ("ARR_DISCORD_WEBHOOK_URL", f"{app}-notification", "webhook_url"),
    }
    own = {
        (doc["metadata"]["name"], key)
        for text in cluster.render_tag(app, stubs).files.values()
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc.get("kind") == "Secret"
        for key in doc.get("stringData") or {}
    }
    assert (f"{app}-exportarr", "api_key") in own
    (secret,) = [doc for doc in flipped[app] if doc["kind"] == "Secret"]
    assert secret["metadata"]["name"] == f"{app}-notification"
    assert secret["stringData"] == {"webhook_url": stubs["arr_discord_webhook_url"]}


@pytest.mark.parametrize("app", APPS)
def test_the_seed_job_reads_no_role_but_its_app_and_arr_notification(app):
    """The deployer's scan routes an arr-notification change to both apps through this lookup."""
    text = (K8S_ROLES / app / TEMPLATE).read_text()
    assert set(re.findall(r"/roles/k8s/([^/'\"]+)/", text)) == {"arr-notification"}


class _Arr:
    """A local server keeping Connect notifications as the *arr API does, and counting writes."""

    def __init__(self, rows: list[dict]):
        self.rows = [dict(row) for row in rows]
        self.writes: list[str] = []
        state = self

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

            def do_GET(self):
                bodies = {
                    "/api/v3/notification": state.rows,
                    "/api/v3/notification/schema": [SCHEMA],
                }
                if self.headers.get("X-Api-Key") != KEY:
                    self._answer(401, {"message": "Unauthorized"})
                elif self.path not in bodies:
                    self._answer(404, {})
                else:
                    self._answer(200, bodies[self.path])

            def do_POST(self):
                row = {**self._body(), "id": len(state.rows) + 1}
                state.rows.append(row)
                state.writes.append("POST")
                self._answer(201, row)

            def do_PUT(self):
                row = self._body()
                state.rows = [row if r["id"] == row["id"] else r for r in state.rows]
                state.writes.append("PUT")
                self._answer(202, row)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _run(docs: list[dict], directory, arr: _Arr) -> subprocess.CompletedProcess:
    """The rendered seed, run with the Job's own environment pointed at `arr`."""
    env = {e["name"]: e["value"] for e in _container(docs)["env"] if "value" in e}
    env |= {
        "ARR_HOST": "127.0.0.1",
        "ARR_PORT": str(arr.server.server_address[1]),
        "ARR_API_KEY": KEY,
        "ARR_DISCORD_WEBHOOK_URL": URL,
    }
    return subprocess.run(
        [sys.executable, "-E", "-s", str(directory / "seed_arr_notification.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _drifted(app: str) -> dict:
    """A live row the seed must rewrite: a trigger turned on by hand and a stale webhook."""
    row = {key: value for key, value in SCHEMA.items() if key != "presets"}
    row = json.loads(json.dumps(row))
    row |= {"id": 1, "name": DEFAULTS["arr_notification_name"], "onGrab": True}
    row["fields"][0]["value"] = "https://discord.example/api/webhooks/0/stale"
    row["fields"][1]["value"] = app.capitalize()
    return row


@pytest.mark.parametrize("start", ["no-row", "drifted-row"])
@pytest.mark.parametrize("app", APPS)
def test_a_second_run_changes_nothing(flipped, tmp_path, app, start):
    """`homelab/rerun-safe` names this test: the Job's second run writes nothing."""
    (volume,) = _seed(flipped[app])["spec"]["template"]["spec"]["volumes"]
    configmap = next(
        doc
        for doc in flipped[app]
        if doc["kind"] == "ConfigMap"
        and doc["metadata"]["name"] == volume["configMap"]["name"]
    )
    for key, text in configmap["data"].items():
        (tmp_path / key).write_text(text)
    arr = _Arr([] if start == "no-row" else [_drifted(app)])
    try:
        first = _run(flipped[app], tmp_path, arr)
        assert first.returncode == 0, first.stderr
        assert first.stdout.splitlines()[-1] == "SEED_CHANGED: 1"
        assert arr.writes == ["POST" if start == "no-row" else "PUT"]
        second = _run(flipped[app], tmp_path, arr)
    finally:
        arr.close()
    assert second.returncode == 0, second.stderr
    assert second.stdout.splitlines()[-1] == "SEED_CHANGED: 0"
    assert len(arr.writes) == 1
    (row,) = arr.rows
    assert sorted(k for k in row if k.startswith("on") and row[k]) == sorted(
        DEFAULTS["arr_notification_triggers"]
    )
    assert URL not in first.stdout + first.stderr + second.stdout + second.stderr
