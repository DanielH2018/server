"""jellyfin's snapshot-cap Job script against a fixture API server.

`World` answers the script's three calls from a dict of objects and records every call, so a
test can read what the script asked for and what it wrote. The render of the Job itself is
tested in test_jellyfin_verify.py, and its RBAC against these calls in
ansible/tests/k8s/test_rbac_against_script.py.

Run: uv run pytest ansible/roles/k8s/jellyfin/tests/test_jellyfin_snapshot_cap.py
"""

import io
import json
import urllib.error
from collections.abc import Mapping
from email.message import Message

import jellyfin_snapshot_cap
import pytest

CLAIM_PATH = "/api/v1/namespaces/homelab/persistentvolumeclaims/jellyfin-config"
VOLUME = "pvc-4f1c"
VOLUME_PATH = f"{jellyfin_snapshot_cap.VOLUMES}/{VOLUME}"
CAP = "17179869184"
ENV = {"CAP_NAMESPACE": "homelab", "CAP_CLAIM": "jellyfin-config", "CAP_MAX_SIZE": CAP}


class World(jellyfin_snapshot_cap.Kube):
    """A fixture API server holding one claim and its Longhorn volume."""

    def __init__(self, volume_name: str = VOLUME, cap: str = "0"):
        super().__init__("https://api", "token")
        self.objects = {
            CLAIM_PATH: {"spec": {"volumeName": volume_name}},
            VOLUME_PATH: {"spec": {"snapshotMaxSize": cap}},
        }
        self.patch_status = 200
        self.calls: list[tuple[str, str]] = []

    def call(
        self, method: str, path: str, body: Mapping | None = None
    ) -> tuple[int, dict]:
        self.calls.append((method, path))
        if path not in self.objects:
            return 404, {"message": f"{path} not found"}
        if method == "PATCH":
            if self.patch_status != 200:
                return self.patch_status, {"message": "admission webhook denied"}
            assert body is not None
            self.objects[path]["spec"].update(body["spec"])
        return 200, self.objects[path]

    def methods(self, method: str) -> list[str]:
        return [path for m, path in self.calls if m == method]


def test_an_uncapped_volume_is_patched_to_the_rendered_cap(capsys):
    world = World()
    assert jellyfin_snapshot_cap.main([], ENV, world) == 0
    assert world.objects[VOLUME_PATH]["spec"]["snapshotMaxSize"] == CAP
    assert world.methods("PATCH") == [VOLUME_PATH]
    assert f"capped snapshots at {CAP}, was 0" in capsys.readouterr().out


def test_a_second_run_changes_nothing(capsys):
    """`homelab/rerun-safe` names this test: the Job's second run reads twice and writes nothing."""
    world = World()
    assert jellyfin_snapshot_cap.main([], ENV, world) == 0
    first = list(world.calls)
    assert jellyfin_snapshot_cap.main([], ENV, world) == 0
    second = world.calls[len(first) :]
    assert [m for m, _ in second] == ["GET", "GET"]
    assert "already caps snapshots" in capsys.readouterr().out


def test_a_claim_with_no_volume_fails_without_a_write(capsys):
    world = World(volume_name="")
    assert jellyfin_snapshot_cap.main([], ENV, world) == 1
    assert world.methods("PATCH") == []
    assert "has no volumeName yet" in capsys.readouterr().err


def test_a_refused_patch_fails_the_job_naming_the_answer(capsys):
    world = World()
    world.patch_status = 400
    assert jellyfin_snapshot_cap.main([], ENV, world) == 1
    assert "answered HTTP 400: admission webhook denied" in capsys.readouterr().err


def test_a_missing_volume_fails_the_job(capsys):
    world = World(volume_name="pvc-gone")
    assert jellyfin_snapshot_cap.main([], ENV, world) == 1
    assert "volume pvc-gone answered HTTP 404" in capsys.readouterr().err


class _Response(io.BytesIO):
    status = 200


def test_the_patch_goes_as_a_merge_patch():
    """The API server answers a PATCH sent as plain `application/json` with 415."""
    sent = []

    def record(request, *, timeout):
        sent.append(request)
        return _Response(b"{}")

    kube = jellyfin_snapshot_cap.Kube("https://api", "token", opener=record)
    kube.call("PATCH", VOLUME_PATH, {"spec": {"snapshotMaxSize": CAP}})
    (request,) = sent
    assert request.get_method() == "PATCH"
    assert request.get_header("Content-type") == "application/merge-patch+json"
    assert json.loads(request.data) == {"spec": {"snapshotMaxSize": CAP}}


def test_an_error_status_is_returned_as_an_answer():
    def refuse(request, *, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            Message(),
            io.BytesIO(b'{"message":"no"}'),
        )

    kube = jellyfin_snapshot_cap.Kube("https://api", "token", opener=refuse)
    assert kube.call("GET", VOLUME_PATH) == (403, {"message": "no"})


def test_the_script_answers_help_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exit_:
        jellyfin_snapshot_cap.main(["--help"], ENV, World())
    assert exit_.value.code == 0
    assert "jellyfin's snapshot-cap Job" in capsys.readouterr().out
