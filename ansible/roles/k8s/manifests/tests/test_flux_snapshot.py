"""The snapshot Job snapshots each claim before its tag applies, and fails rather than guess.

The API server is a real localhost HTTP server over a fixture world, so every call goes
through `flux_job_connect.open_url` and the script reads real HTTP statuses. The clock and the
sleep are the test's own, so the readiness timeout costs nothing. The plan is
docs/target-architecture/deploy/d5-bulk-move.md, step 11 (D5.2a).

Run: uv run pytest ansible/roles/k8s/manifests/tests/test_flux_snapshot.py
"""

import http.server
import json
import threading
from datetime import datetime, timezone

import pytest

import flux_snapshot
from flux_snapshot import Claim, Config, Failed, Kube

TAG = "sonarr"
DIGEST8 = "0a1b2c3d"
TOKEN = "20261016120000"
LONGHORN = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
KUSTOMIZATION = (
    f"/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flux-system/kustomizations/{TAG}"
)


def _pvc_id(name: str) -> str:
    return f"media_{name}__PersistentVolumeClaim"


class World:
    """The objects the fake API server serves, and every call it received."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.kustomization: dict | None = {"status": {"inventory": {"entries": []}}}
        self.kustomization_status = 200
        self.pvcs: dict[str, str] = {}
        self.caps: dict[str, str] = {}
        self.snapshots: dict[str, dict] = {}
        self.becomes_ready = True
        self.listing_hides: set[str] = set()
        self.base = ""

    def claim(self, name: str, listed: bool = True) -> None:
        """A bound PVC `name` in `media` on the volume `pvc-<name>`, in the inventory."""
        self.pvcs[name] = f"pvc-{name}"
        if listed:
            self.inventory(name)

    def inventory(self, *names: str) -> None:
        assert self.kustomization is not None
        self.kustomization["status"]["inventory"]["entries"] += [
            {"id": _pvc_id(n), "v": "v1"} for n in names
        ]

    def snapshot(self, name: str, volume: str, created: str, removed=False, size=0):
        self.snapshots[name] = {
            "metadata": {"name": name, "creationTimestamp": created},
            "spec": {"volume": volume},
            "status": {"readyToUse": not removed, "markRemoved": removed, "size": size},
        }

    def methods(self, method: str) -> list[str]:
        return [path for m, path in self.calls if m == method]

    def answer(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        self.calls.append((method, path))
        if path == KUSTOMIZATION:
            if self.kustomization is None:
                return 404, {}
            return self.kustomization_status, self.kustomization
        if path.startswith("/api/v1/namespaces/media/persistentvolumeclaims/"):
            name = path.rsplit("/", 1)[1]
            if name not in self.pvcs:
                return 404, {}
            return 200, {"spec": {"volumeName": self.pvcs[name]}}
        if path.startswith("/api/v1/persistentvolumes/"):
            name = path.rsplit("/", 1)[1]
            return 200, {
                "spec": {"csi": {"driver": "driver.longhorn.io", "volumeHandle": name}}
            }
        if path.startswith(f"{LONGHORN}/volumes/"):
            name = path.rsplit("/", 1)[1]
            spec = {"snapshotMaxSize": self.caps[name]} if name in self.caps else {}
            return 200, {"spec": spec, "status": {"state": "attached"}}
        if path == f"{LONGHORN}/snapshots" and method == "GET":
            items = [
                s for n, s in self.snapshots.items() if n not in self.listing_hides
            ]
            return 200, {"items": items}
        if path == f"{LONGHORN}/snapshots" and method == "POST":
            assert body is not None
            name = body["metadata"]["name"]
            if name in self.snapshots:
                return 409, {"message": "already exists"}
            self.snapshot(name, body["spec"]["volume"], "2026-10-16T12:00:00Z")
            self.snapshots[name]["status"]["readyToUse"] = self.becomes_ready
            return 201, self.snapshots[name]
        if path.startswith(f"{LONGHORN}/snapshots/"):
            name = path.rsplit("/", 1)[1]
            if name not in self.snapshots:
                return 404, {}
            if method == "DELETE":
                self.snapshots[name]["status"]["markRemoved"] = True
            return 200, self.snapshots[name]
        return 500, {"message": f"the fixture serves no {method} {path}"}


@pytest.fixture
def world():
    world = World()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            status, answer = world.answer(self.command, self.path, body)
            raw = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_DELETE = _serve

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    world.base = f"http://127.0.0.1:{server.server_address[1]}"
    yield world
    server.shutdown()
    server.server_close()


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _run(world: World, *claims: str, retain: int = 3) -> list[str]:
    """Run the Job over `claims` in `media`; return what it logged."""
    cfg = Config(TAG, DIGEST8, tuple(Claim(c, "media") for c in claims), retain, 120, 2)
    clock, logged = Clock(), []
    flux_snapshot.run(
        Kube(world.base, "token"),
        cfg,
        now=lambda: datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc),
        sleep=clock.sleep,
        clock=clock,
        log=logged.append,
    )
    return logged


def _name(claim: str) -> str:
    return flux_snapshot.snapshot_name(TAG, DIGEST8, claim, TOKEN)


def _old(world: World, claim: str, *stamps: str, removed: str = "") -> list[str]:
    """Earlier runs' snapshots of `claim`, one per creation time; the `removed` one markRemoved."""
    names = []
    for stamp in stamps:
        name = f"autodeploy-{TAG}-ffffffff-config-{stamp.replace('-', '')}"
        world.snapshot(
            name, f"pvc-{claim}", f"{stamp}T00:00:00Z", removed=stamp == removed
        )
        names.append(name)
    return names


def test_a_claim_is_snapshotted_named_from_the_tag_digest_and_token(world):
    world.claim("sonarr-config")
    _run(world, "sonarr-config")
    assert _name("sonarr-config") == f"autodeploy-sonarr-{DIGEST8}-config-{TOKEN}"
    assert (
        world.snapshots[_name("sonarr-config")]["spec"]["volume"] == "pvc-sonarr-config"
    )


def test_a_snapshot_that_never_reports_ready_fails_before_any_prune(world):
    world.claim("sonarr-config")
    world.becomes_ready = False
    with pytest.raises(Failed, match="did not report readyToUse within 120s"):
        _run(world, "sonarr-config")
    assert world.methods("DELETE") == []


def test_an_existing_snapshot_of_the_same_name_fails_and_is_not_adopted(world):
    world.claim("sonarr-config")
    world.snapshot(_name("sonarr-config"), "pvc-sonarr-config", "2026-10-16T11:00:00Z")
    with pytest.raises(
        Failed,
        match=f"Snapshot CR longhorn-system/{_name('sonarr-config')} already exists",
    ):
        _run(world, "sonarr-config")
    assert f"{LONGHORN}/snapshots/{_name('sonarr-config')}" not in world.methods("GET")
    assert world.methods("DELETE") == []


def test_a_mark_removed_snapshot_does_not_count_toward_retention(world):
    world.claim("sonarr-config")
    # Retain 3: this run's plus two older live ones. The markRemoved CR sits between them, so
    # counting it would push one more live snapshot out of the window.
    oldest, *_ = _old(
        world,
        "sonarr-config",
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
        "2026-10-04",
        removed="2026-10-03",
    )
    _run(world, "sonarr-config", retain=3)
    assert world.methods("DELETE") == [f"{LONGHORN}/snapshots/{oldest}"]


def test_a_retain_below_two_still_keeps_two(world):
    world.claim("sonarr-config")
    oldest, _ = _old(world, "sonarr-config", "2026-10-01", "2026-10-02")
    _run(world, "sonarr-config", retain=0)
    assert world.methods("DELETE") == [f"{LONGHORN}/snapshots/{oldest}"]


def test_a_reached_cap_fails_naming_the_cap_before_any_claim_is_snapshotted(world):
    world.claim("sonarr-config")
    world.claim("sonarr-logs")
    world.caps["pvc-sonarr-logs"] = "100"
    world.snapshot(
        "autodeploy-sonarr-ffffffff-logs-1",
        "pvc-sonarr-logs",
        "2026-10-01T00:00:00Z",
        size=100,
    )
    with pytest.raises(
        Failed, match=r"spec\.snapshotMaxSize of 100 bytes, 0 bytes of headroom"
    ):
        _run(world, "sonarr-config", "sonarr-logs")
    assert world.methods("POST") == []


@pytest.mark.parametrize("cap", ["", "0"])
def test_an_empty_or_zero_cap_reads_as_uncapped(world, cap):
    world.claim("sonarr-config")
    world.caps["pvc-sonarr-config"] = cap
    _old(world, "sonarr-config", "2026-10-01")
    world.snapshots[next(iter(world.snapshots))]["status"]["size"] = 10**12
    _run(world, "sonarr-config")
    assert world.snapshots[_name("sonarr-config")]["status"]["readyToUse"]


def test_a_listing_that_lacks_the_new_snapshot_fails_before_any_delete(world):
    world.claim("sonarr-config")
    _old(world, "sonarr-config", "2026-10-01", "2026-10-02", "2026-10-03")
    world.listing_hides.add(_name("sonarr-config"))
    with pytest.raises(Failed, match=f"lacks {_name('sonarr-config')}"):
        _run(world, "sonarr-config")
    assert world.methods("DELETE") == []


def test_a_new_claim_absent_live_and_from_the_inventory_is_skipped_and_logged(world):
    world.claim("sonarr-config")
    logged = _run(world, "sonarr-config", "sonarr-new")
    assert any(line.startswith("skip media/sonarr-new:") for line in logged)
    assert _name("sonarr-config") in world.snapshots
    assert _name("sonarr-new") not in world.snapshots


def test_a_claim_the_inventory_lists_and_the_cluster_lacks_fails_naming_the_pvc(world):
    world.claim("sonarr-config")
    world.inventory("sonarr-new")
    with pytest.raises(Failed, match="PVC media/sonarr-new is gone"):
        _run(world, "sonarr-config", "sonarr-new")
    assert world.methods("POST") == []


def test_a_tag_that_never_applied_skips_every_absent_claim(world):
    world.kustomization = None
    logged = _run(world, "sonarr-config")
    assert logged == [
        "skip media/sonarr-config: absent live and from the inventory, so it is new in this revision"
    ]


def test_a_refused_inventory_read_fails_rather_than_reading_as_new(world):
    world.kustomization_status = 403
    with pytest.raises(Failed, match="Kustomization sonarr answered HTTP 403"):
        _run(world, "sonarr-config")


def test_a_snapshot_name_over_63_bytes_fails_before_any_read_of_the_claim(world):
    with pytest.raises(Failed, match="over Longhorn's 63-byte delete limit"):
        _run(world, "sonarr-" + "x" * 30)
    assert world.methods("POST") == []


def test_the_script_answers_help_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exit_:
        flux_snapshot.main(["--help"])
    assert exit_.value.code == 0
    assert "Snapshot a flux-owned role's claims" in capsys.readouterr().out
