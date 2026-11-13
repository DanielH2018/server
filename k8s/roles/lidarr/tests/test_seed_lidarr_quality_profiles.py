"""Lidarr's quality profiles must be declared, not left as UI state.

The decision tests drive `find_profile` / `declared_body` / `needs_update`, the three functions
that choose create, rewrite or no-op. Each rule is an accept/reject pair, so a check that
stopped matching fails its own test rather than passing on both halves. The render tests check
the Job as Flux receives it, and the run test executes the rendered script against a fixture
API twice.

Run: uv run pytest k8s/roles/lidarr/tests
"""

import copy
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import seed_lidarr_quality_profiles as seed
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

KEY = "fixture-key"
DEFAULTS = yaml_fast.safe_load((K8S_ROLES / "lidarr/defaults/main.yml").read_text())
PROFILES = DEFAULTS["lidarr_quality_profiles"]

# Lidarr's own quality names and ids, in the order its schema lists them, cut to the ones the
# declarations name plus a neighbour of each kind (a 24-bit FLAC, a lower MP3, a Vorbis tier).
QUALITIES = [
    (0, "Unknown"),
    (1, "MP3-192"),
    (2, "MP3-VBR-V0"),
    (3, "MP3-256"),
    (10, "AAC-256"),
    (12, "AAC-VBR"),
    (4, "MP3-320"),
    (11, "AAC-320"),
    (16, "OGG Vorbis Q8"),
    (15, "OGG Vorbis Q9"),
    (14, "OGG Vorbis Q10"),
    (6, "FLAC"),
    (21, "FLAC 24bit"),
    (7, "ALAC"),
]


def _leaf(quality_id: int, name: str, allowed: bool = False) -> dict:
    return {
        "id": 0,
        "name": None,
        "quality": {"id": quality_id, "name": name},
        "items": [],
        "allowed": allowed,
    }


def _grouped(allowed: set[str]) -> list[dict]:
    """Items as Lidarr ships them: the lossy qualities from MP3-VBR-V0 up in one group."""
    leaves = [_leaf(i, n, n in allowed) for i, n in QUALITIES]
    grouped = [
        leaf
        for leaf in leaves
        if leaf["quality"]["name"] in {n for _, n in QUALITIES[1:11]}
    ]
    group = {
        "id": 1006,
        "name": "High Quality Lossy",
        "quality": None,
        "items": grouped,
        "allowed": any(leaf["allowed"] for leaf in grouped),
    }
    return [leaves[0], group, *leaves[11:]]


def _profile(name: str, profile_id: int = 0, allowed: set[str] | None = None) -> dict:
    return {
        "id": profile_id,
        "name": name,
        "upgradeAllowed": False,
        "cutoff": 0,
        "items": _grouped(allowed or set()),
        "minFormatScore": 0,
        "cutoffFormatScore": 0,
        "formatItems": [{"format": 5, "name": "a format", "score": 7}],
    }


def declare(existing: dict, spec: dict) -> dict:
    return seed.declared_body(
        existing,
        name=spec["name"],
        allowed=spec["allowed"],
        cutoff=spec["cutoff"],
        upgrade_allowed=spec["upgrade_allowed"],
    )


STANDARD, LOSSLESS = PROFILES


def allowed_names(body: dict) -> set[str]:
    return {i["quality"]["name"] for i in body["items"] if i["allowed"]}


# ── the match key ───────────────────────────────────────────────────────────────────────────


def test_a_profile_is_found_by_its_name() -> None:
    shipped = _profile("Standard", 3)
    assert seed.find_profile([_profile("Any", 1), shipped], "Standard") is shipped


def test_a_name_that_matches_nothing_is_not_adopted() -> None:
    # The reject half: Standard must not be mistaken for a similarly named profile.
    assert seed.find_profile([_profile("Standard (old)", 3)], "Standard") is None


# ── what the declaration says ───────────────────────────────────────────────────────────────


def test_the_lossy_profile_allows_exactly_the_decided_qualities() -> None:
    body = declare(_profile("Standard", 3), STANDARD)
    assert allowed_names(body) == set(STANDARD["allowed"])
    assert "AAC-256" in allowed_names(body)
    assert "MP3-256" not in allowed_names(body)
    assert not {n for n in allowed_names(body) if "Vorbis" in n}
    assert body["cutoff"] == 4
    assert body["upgradeAllowed"] is True


def test_the_lossless_profile_allows_16_bit_flac_and_nothing_else() -> None:
    body = declare(_profile("Lossless", 2, {"FLAC", "FLAC 24bit", "ALAC"}), LOSSLESS)
    assert allowed_names(body) == {"FLAC"}
    assert body["cutoff"] == 6


def test_a_group_is_split_so_one_member_can_be_allowed_alone() -> None:
    # MP3-320 and AAC-320 share Lidarr's group; allowing the first must not allow the second.
    body = declare(
        _profile("X"), {**STANDARD, "allowed": ["MP3-320"], "cutoff": "MP3-320"}
    )
    assert all(not i["items"] and i["quality"] for i in body["items"])
    assert allowed_names(body) == {"MP3-320"}


def test_every_quality_survives_the_flattening_in_order() -> None:
    body = declare(_profile("X"), STANDARD)
    assert [i["quality"]["name"] for i in body["items"]] == [n for _, n in QUALITIES]


def test_fields_the_app_holds_survive_the_rewrite() -> None:
    body = declare(_profile("Standard", 3), STANDARD)
    assert body["formatItems"] == [{"format": 5, "name": "a format", "score": 7}]
    assert body["id"] == 3


def test_a_quality_name_the_app_does_not_carry_fails_the_run() -> None:
    with pytest.raises(seed.SeedError, match="Opus 192"):
        declare(_profile("Standard", 3), {**STANDARD, "allowed": ["Opus 192"]})


def test_a_cutoff_outside_the_allowed_set_fails_the_run() -> None:
    with pytest.raises(seed.SeedError, match="cutoff"):
        declare(_profile("Standard", 3), {**STANDARD, "cutoff": "MP3-256"})


# ── the rewrite decision ────────────────────────────────────────────────────────────────────


def test_a_profile_already_declared_is_not_rewritten() -> None:
    declared = declare(_profile("Standard", 3), STANDARD)
    assert not seed.needs_update(declared, declare(declared, STANDARD))


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda b: b.update(upgradeAllowed=False), id="upgrades switched off"
        ),
        pytest.param(lambda b: b.update(cutoff=2), id="a moved cutoff"),
        pytest.param(
            lambda b: b["items"][2].update(allowed=False),
            id="a declared quality unallowed",
        ),
        pytest.param(
            lambda b: b["items"][3].update(allowed=True), id="an extra quality allowed"
        ),
    ],
)
def test_drift_from_the_declaration_is_rewritten(mutate) -> None:
    drifted = declare(_profile("Standard", 3), STANDARD)
    mutate(drifted)
    assert seed.needs_update(drifted, declare(drifted, STANDARD))


# ── the Job, as Flux receives it ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def verify_docs(tmp_path_factory) -> list[dict]:
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    rendered = cluster.render_tag("lidarr", load_stubs(path))
    return [
        doc
        for name, text in sorted(rendered.verify.items())
        if name != "kustomization.yaml"
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


def _jobs(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d["kind"] == "Job"]


def _quality_job(docs: list[dict]) -> dict:
    (job,) = [
        j
        for j in _jobs(docs)
        if j["metadata"]["name"].startswith("lidarr-quality-profiles-seed-")
    ]
    return job


def test_lidarr_verify_passes_every_job_census_row(verify_docs) -> None:
    assert problems({"lidarr-verify": verify_docs}) == []
    assert len(_jobs(verify_docs)) == 2


def test_the_job_reads_the_key_lidarr_applies_and_the_declared_profiles(
    verify_docs,
) -> None:
    (container,) = _quality_job(verify_docs)["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e for e in container["env"]}
    assert env["LIDARR_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "lidarr-exportarr",
        "key": "api_key",
    }
    assert json.loads(env["LIDARR_QUALITY_PROFILES"]["value"]) == PROFILES
    assert env["LIDARR_URL"]["value"] == "http://lidarr.homelab.svc.cluster.local:8686"


# ── the run ─────────────────────────────────────────────────────────────────────────────────


class _Lidarr:
    """A local server keeping quality profiles as Lidarr's `/api/v1` does, counting writes."""

    def __init__(self, rows: list[dict]):
        self.rows = copy.deepcopy(rows)
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
                    "/api/v1/qualityprofile": state.rows,
                    "/api/v1/qualityprofile/schema": _profile(""),
                }
                if self.headers.get("X-Api-Key") != KEY:
                    self._answer(401, {})
                elif self.path not in bodies:
                    self._answer(404, {})
                else:
                    self._answer(200, bodies[self.path])

            def do_POST(self):
                row = {**self._body(), "id": len(state.rows) + 10}
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


def _run(job: dict, directory, lidarr: _Lidarr) -> subprocess.CompletedProcess:
    (container,) = job["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e["value"] for e in container["env"] if "value" in e}
    env |= {
        "LIDARR_URL": f"http://127.0.0.1:{lidarr.server.server_address[1]}",
        "LIDARR_API_KEY": KEY,
    }
    return subprocess.run(
        [
            sys.executable,
            "-E",
            "-s",
            str(directory / "seed_lidarr_quality_profiles.py"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("start", ["shipped-profiles", "no-profiles"])
def test_a_second_run_changes_nothing(verify_docs, tmp_path, start) -> None:
    """`homelab/rerun-safe` names this test: the Job's second run writes nothing.

    From Lidarr's shipped `Standard` and `Lossless` the first run updates both, and from an
    empty list it creates both. `Any` is on the server in the first case and must survive.
    """
    job = _quality_job(verify_docs)
    (volume,) = job["spec"]["template"]["spec"]["volumes"]
    configmap = next(
        d
        for d in verify_docs
        if d["kind"] == "ConfigMap"
        and d["metadata"]["name"] == volume["configMap"]["name"]
    )
    for key, text in configmap["data"].items():
        (tmp_path / key).write_text(text)
    shipped = [_profile("Any", 1), _profile("Lossless", 2), _profile("Standard", 3)]
    lidarr = _Lidarr(shipped if start == "shipped-profiles" else [])
    try:
        first = _run(job, tmp_path, lidarr)
        assert first.returncode == 0, first.stderr
        verb = "updated" if start == "shipped-profiles" else "created"
        assert first.stdout.splitlines() == [
            f"lidarr: quality profile 'Standard' {verb}",
            f"lidarr: quality profile 'Lossless' {verb}",
        ]
        assert lidarr.writes == ["PUT" if verb == "updated" else "POST"] * 2
        second = _run(job, tmp_path, lidarr)
    finally:
        lidarr.close()
    assert second.returncode == 0, second.stderr
    assert second.stdout.splitlines() == [
        "lidarr: quality profile 'Standard' already declares this spec — no change",
        "lidarr: quality profile 'Lossless' already declares this spec — no change",
    ]
    assert len(lidarr.writes) == 2
    by_name = {row["name"]: row for row in lidarr.rows}
    assert allowed_names(by_name["Standard"]) == set(STANDARD["allowed"])
    assert allowed_names(by_name["Lossless"]) == {"FLAC"}
    assert ("Any" in by_name) is (start == "shipped-profiles")
