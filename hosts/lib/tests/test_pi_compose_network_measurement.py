"""What `docker compose up` does with a project network whose name an unlabelled network holds (H7.26).

The live Pi's `proxy` and `lifecycle` networks were created by `docker network create`, so they
carry none of the labels Compose puts on a network it creates. H7's *The old step list*, H7.5,
asks for this measurement before the networks stay `external: true`. Moving a network into the
project is only safe if Compose would adopt the existing one; if it refuses, or keeps the
network without its labels, the project cannot own it short of recreating it, which stops every
attached container on the Pi.

The test runs against a real daemon, so it carries the `image` marker and runs in
`.github/workflows/image-leg.yml`. It creates its own network and image under a random name and
removes both. It asserts the property the decision rests on, whichever way Compose answers:
Compose never recreates or relabels the existing network. It prints Compose's own output, which
the image leg's log records.

Run: uv run pytest -m image -n 0 hosts/lib/tests/test_pi_compose_network_measurement.py
"""

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from lib.proc_testing import run

pytestmark = pytest.mark.image


def _docker(*args: str, input: str | None = None, cwd: Path | None = None):
    return run(
        ["docker", *args], env=dict(os.environ), input=input, cwd=cwd, timeout=300
    )


@pytest.fixture
def daemon() -> None:
    if shutil.which("docker") and _docker("info").returncode == 0:
        return
    if os.environ.get("CI"):
        pytest.fail("CI must run the image leg, and no Docker daemon answered")
    pytest.skip("no Docker daemon here; CI runs the image leg")


@pytest.fixture
def network(daemon: None):
    name = f"h726-measure-{uuid.uuid4().hex[:8]}"
    assert _docker("network", "create", name).returncode == 0
    yield name
    _docker("compose", "-p", name, "down", "--remove-orphans")
    _docker("network", "rm", name)
    _docker("image", "rm", "-f", f"{name}:0")


def _inspect(name: str) -> dict:
    found = _docker("network", "inspect", name)
    assert found.returncode == 0, found.stderr
    return json.loads(found.stdout)[0]


def test_compose_never_adopts_an_unlabelled_network_into_its_project(network, tmp_path):
    before = _inspect(network)
    assert not before["Labels"], before["Labels"]
    # An image built from nothing, so the case pulls nothing; `--no-start` never runs it.
    built = _docker(
        "build", "-t", f"{network}:0", "-", input="FROM scratch\nLABEL h726=1\n"
    )
    assert built.returncode == 0, built.stderr
    (tmp_path / "compose.yaml").write_text(
        f"services:\n  probe:\n    image: {network}:0\n    command: [/none]\n"
        f"    pull_policy: never\n    networks: [shared]\n"
        f"networks:\n  shared:\n    name: {network}\n"
    )

    up = _docker("compose", "-p", network, "up", "--no-start", cwd=tmp_path)
    print(
        f"compose up exited {up.returncode}\nstdout:\n{up.stdout}\nstderr:\n{up.stderr}"
    )

    after = _inspect(network)
    assert after["Id"] == before["Id"], "Compose recreated the existing network"
    assert not any(
        k.startswith("com.docker.compose.") for k in after["Labels"] or {}
    ), f"Compose labelled the existing network: {after['Labels']}"
