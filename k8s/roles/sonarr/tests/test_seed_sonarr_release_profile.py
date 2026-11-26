"""sonarr's release-profile seed: it declares the air-date profile and its Auto Tagging rules.

The script tests run the rendered ConfigMap's two files, as the Job's container runs them,
against a local HTTP server that keeps release profiles, Auto Tagging rules and tags the way
Sonarr's v5 API does.

Run: uv run pytest k8s/roles/sonarr/tests/test_seed_sonarr_release_profile.py
"""

import json
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
KEY = "fixture-key"
SCRIPT = "seed_sonarr_release_profile.py"
DEFAULTS = yaml_fast.safe_load((K8S_ROLES / "sonarr/defaults/main.yml").read_text())
NAME = DEFAULTS["sonarr_k8s_air_date_profile_name"]
TAGS = [{"id": 1, "label": "anime"}, {"id": 2, "label": "kids"}]
OTHER_PROFILE = {
    "id": 7,
    "name": "x265 only",
    "enabled": True,
    "required": ["x265"],
    "ignored": [],
    "airDateRestriction": False,
    "airDateGracePeriod": 0,
    "allowSeasonPackWithoutAllEpisodesAired": False,
    "indexerIds": [],
    "tags": [2],
    "excludedTags": [],
}


@pytest.fixture(scope="module")
def flipped(tmp_path_factory) -> list[dict]:
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    rendered = cluster.render_tag(TAG, load_stubs(path))
    return [
        doc
        for name, text in sorted(rendered.verify.items())
        if name != "kustomization.yaml"
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


def _seed_job(docs: list[dict]) -> dict:
    (job,) = [
        doc
        for doc in docs
        if doc["kind"] == "Job"
        and doc["metadata"]["name"].startswith("sonarr-release-profile-")
    ]
    return job


@pytest.fixture(scope="module")
def script_dir(flipped, tmp_path_factory):
    directory = tmp_path_factory.mktemp("seed")
    configmap = next(
        doc for doc in flipped if doc["kind"] == "ConfigMap" and SCRIPT in doc["data"]
    )
    for key, text in configmap["data"].items():
        (directory / key).write_text(text)
    return directory


@pytest.fixture(scope="module")
def declaration(flipped) -> str:
    (container,) = _seed_job(flipped)["spec"]["template"]["spec"]["containers"]
    return next(
        e["value"] for e in container["env"] if e["name"] == "SONARR_RELEASE_PROFILE"
    )


class FakeSonarr:
    """Sonarr's v5 release-profile, auto-tagging, tag and command endpoints, with the writes."""

    def __init__(self, profiles: list[dict], tags: list[dict] = TAGS):
        self.store = {
            "releaseprofile": [dict(p) for p in profiles],
            "autotagging": [],
            "tag": [dict(t) for t in tags],
            "command": [],
        }
        self.writes: list[tuple[str, str, dict]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, code: int, body) -> None:
                payload = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _body(self) -> dict:
                return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

            def _resource(self) -> str:
                return self.path.removeprefix("/api/v5/").split("/")[0]

            def do_GET(self):
                if self.headers.get("X-Api-Key") != KEY:
                    return self._answer(401, {"message": "Unauthorized"})
                resource = self._resource()
                if resource not in fake.store:
                    return self._answer(404, {})
                return self._answer(200, fake.store[resource])

            def do_POST(self):
                body = self._body()
                fake.writes.append(("POST", self.path, body))
                items = fake.store[self._resource()]
                created = {**body, "id": 1 + max((i["id"] for i in items), default=0)}
                items.append(created)
                self._answer(201, created)

            def do_PUT(self):
                body = self._body()
                fake.writes.append(("PUT", self.path, body))
                resource = self._resource()
                fake.store[resource] = [
                    body if i["id"] == body["id"] else i for i in fake.store[resource]
                ]
                self._answer(202, body)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def run(self, script_dir, declaration: str):
        env = {
            "SONARR_URL": f"http://127.0.0.1:{self.server.server_address[1]}",
            "SONARR_API_KEY": KEY,
            "SONARR_RELEASE_PROFILE": declaration,
            "SONARR_DEPLOYMENT_DIGEST8": "abcd1234",
        }
        return subprocess.run(
            [sys.executable, "-E", "-s", str(script_dir / SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sonarr():
    fakes: list[FakeSonarr] = []

    def make(profiles: list[dict], tags: list[dict] = TAGS) -> FakeSonarr:
        fakes.append(FakeSonarr(profiles, tags))
        return fakes[-1]

    yield make
    for fake in fakes:
        fake.close()


def _declared(fake: FakeSonarr) -> dict:
    (profile,) = [p for p in fake.store["releaseprofile"] if p["name"] == NAME]
    return profile


def _profile_writes(fake: FakeSonarr) -> list[tuple[str, str]]:
    return [w[:2] for w in fake.writes if "releaseprofile" in w[1]]


def _with(declaration: str, **changes) -> str:
    return json.dumps({**json.loads(declaration), **changes})


def test_an_absent_profile_is_created_with_the_air_date_rule(
    script_dir, declaration, sonarr
):
    fake = sonarr([OTHER_PROFILE])
    result = fake.run(script_dir, declaration)
    assert result.returncode == 0, result.stderr
    assert "created release profile" in result.stdout
    profile = _declared(fake)
    assert profile["airDateRestriction"] is True
    assert profile["airDateGracePeriod"] == -24
    assert profile["excludedTags"] == [1]
    assert profile["tags"] == [] and profile["indexerIds"] == []
    assert _profile_writes(fake) == [("POST", "/api/v5/releaseprofile")]


def test_an_excluded_series_type_gets_a_rule_that_tags_its_series(
    script_dir, declaration, sonarr
):
    fake = sonarr([])
    result = fake.run(script_dir, declaration)
    assert result.returncode == 0, result.stderr
    (rule,) = fake.store["autotagging"]
    assert rule["tags"] == [1]
    assert rule["removeTagsAutomatically"] is False
    (spec,) = rule["specifications"]
    assert spec["implementation"] == "SeriesTypeSpecification"
    assert spec["fields"] == [{"name": "value", "value": 2}]
    assert fake.store["command"] == [{"name": "RefreshSeries", "id": 1}]


def test_a_missing_series_type_tag_is_created_and_excluded(
    script_dir, declaration, sonarr
):
    fake = sonarr([], tags=[{"id": 2, "label": "kids"}])
    result = fake.run(script_dir, declaration)
    assert result.returncode == 0, result.stderr
    assert {"id": 3, "label": "anime"} in fake.store["tag"]
    assert fake.store["autotagging"][0]["tags"] == [3]
    assert _declared(fake)["excludedTags"] == [3]


def test_a_second_run_changes_nothing(script_dir, declaration, sonarr):
    fake = sonarr([OTHER_PROFILE])
    assert fake.run(script_dir, declaration).returncode == 0
    fake.writes.clear()
    result = fake.run(script_dir, declaration)
    assert result.returncode == 0, result.stderr
    assert "already matches; no change" in result.stdout
    assert fake.writes == []


def test_a_drifted_rule_is_corrected_and_starts_a_refresh(
    script_dir, declaration, sonarr
):
    fake = sonarr([OTHER_PROFILE])
    assert fake.run(script_dir, declaration).returncode == 0
    (rule,) = fake.store["autotagging"]
    rule["removeTagsAutomatically"] = True
    rule["specifications"][0]["fields"] = [{"name": "value", "value": 0}]
    fake.writes.clear()
    result = fake.run(script_dir, declaration)
    assert result.returncode == 0, result.stderr
    assert "removeTagsAutomatically, specifications" in result.stdout
    assert [w[:2] for w in fake.writes] == [
        ("PUT", f"/api/v5/autotagging/{rule['id']}"),
        ("POST", "/api/v5/command"),
    ]


def test_a_drifted_profile_is_corrected_and_others_are_left_alone(
    script_dir, declaration, sonarr
):
    drifted = {
        **OTHER_PROFILE,
        "id": 3,
        "name": NAME,
        "required": [],
        "airDateRestriction": True,
        "airDateGracePeriod": 0,
        "tags": [],
        "excludedTags": [],
    }
    fake = sonarr([OTHER_PROFILE, drifted])
    result = fake.run(script_dir, declaration)
    assert result.returncode == 0, result.stderr
    assert "airDateGracePeriod, excludedTags" in result.stdout
    assert _profile_writes(fake) == [("PUT", "/api/v5/releaseprofile/3")]
    assert _declared(fake)["airDateGracePeriod"] == -24
    assert OTHER_PROFILE in fake.store["releaseprofile"]


def test_a_missing_excluded_tag_fails_without_writing_the_profile(
    script_dir, declaration, sonarr
):
    fake = sonarr([])
    result = fake.run(script_dir, _with(declaration, excluded_tags=["early"]))
    assert result.returncode == 1
    assert "Sonarr has no tag ['early']" in result.stderr
    assert _profile_writes(fake) == []


def test_an_unknown_series_type_fails_and_writes_nothing(
    script_dir, declaration, sonarr
):
    fake = sonarr([])
    result = fake.run(script_dir, _with(declaration, excluded_series_types=["cartoon"]))
    assert result.returncode == 1
    assert "Sonarr has no series type ['cartoon']" in result.stderr
    assert fake.writes == []


def test_the_seed_job_is_a_declared_mutation_that_passes_the_census(flipped):
    job = _seed_job(flipped)
    assert job["metadata"]["annotations"] == {
        "homelab/job-kind": "mutation",
        "homelab/rerun-safe": (
            "k8s/roles/sonarr/tests/test_seed_sonarr_release_profile.py"
            "::test_a_second_run_changes_nothing"
        ),
    }
    assert job["spec"]["template"]["metadata"]["labels"] == {"app": "sonarr-verify"}
    assert problems({f"{TAG}-verify": flipped}) == []


def test_the_declaration_is_the_roles_defaults(declaration):
    assert json.loads(declaration) == {
        "name": NAME,
        "air_date_grace_hours": DEFAULTS["sonarr_k8s_air_date_grace_hours"],
        "excluded_tags": DEFAULTS["sonarr_k8s_air_date_excluded_tags"],
        "excluded_series_types": DEFAULTS["sonarr_k8s_air_date_excluded_series_types"],
    }
