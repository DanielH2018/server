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
Compose never recreates or relabels the existing network. It fails when Compose never reached
the network step, so an unrelated error cannot pass as a measurement. It writes which answer
Compose gave, with Compose's own output, to the image leg's job summary.

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

# The two answers the decision rests on. Either one leaves the existing network untouched, so
# moving it into the project would need a recreate; anything else fails the measurement.
ADOPTED = "adopted the unlabelled network"
REFUSED = "refused the unlabelled network"


def _docker(*args: str, input: str | None = None, cwd: Path | None = None):
    return run(
        ["docker", *args], env=dict(os.environ), input=input, cwd=cwd, timeout=300
    )


@pytest.fixture
def daemon() -> None:
    if shutil.which("docker") and _docker("info").returncode == 0:
        plugin = _docker("compose", "version")
        assert plugin.returncode == 0, (
            f"the daemon answers but Compose does not: {plugin.stderr}"
        )
        return
    if os.environ.get("CI"):
        pytest.fail("CI must run the image leg, and no Docker daemon answered")
    pytest.skip("no Docker daemon here; CI runs the image leg")


@pytest.fixture
def network(daemon: None):
    name = f"h726-net-{uuid.uuid4().hex[:8]}"
    assert _docker("network", "create", name).returncode == 0
    yield name
    _docker("compose", "-p", _project(name), "down", "--remove-orphans")
    _docker("network", "rm", name)
    _docker("image", "rm", "-f", f"{_project(name)}:0")


def _project(network: str) -> str:
    """The project and image name: one that does not contain the network's name."""
    return network.replace("h726-net-", "h726-proj-")


def _inspect(name: str) -> dict:
    found = _docker("network", "inspect", name)
    assert found.returncode == 0, found.stderr
    return json.loads(found.stdout)[0]


def test_compose_never_adopts_an_unlabelled_network_into_its_project(network, tmp_path):
    before = _inspect(network)
    assert not before["Labels"], before["Labels"]
    # An image built from nothing, so the case pulls nothing; `--no-start` never runs it.
    project = _project(network)
    built = _docker(
        "build", "-t", f"{project}:0", "-", input="FROM scratch\nLABEL h726=1\n"
    )
    assert built.returncode == 0, built.stderr
    (tmp_path / "compose.yaml").write_text(
        f"services:\n  probe:\n    image: {project}:0\n    command: [/none]\n"
        f"    pull_policy: never\n    networks: [shared]\n"
        f"networks:\n  shared:\n    name: {network}\n"
    )

    config = _docker("compose", "-p", project, "config", "-q", cwd=tmp_path)
    assert config.returncode == 0, f"the fixture project is invalid: {config.stderr}"

    up = _docker("compose", "-p", project, "up", "--no-start", cwd=tmp_path)
    outcome = _outcome(up, network)
    _record(outcome, up)

    after = _inspect(network)
    assert after["Id"] == before["Id"], "Compose recreated the existing network"
    assert not any(
        k.startswith("com.docker.compose.") for k in after["Labels"] or {}
    ), f"Compose labelled the existing network: {after['Labels']}"
    if outcome == ADOPTED:
        attached = _inspect(network)["Containers"] or {}
        created = _docker(
            "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={project}"
        )
        assert created.stdout.strip(), "Compose exited 0 but created no container"
        # `--no-start` leaves the container unattached until it starts, so the network's
        # container list stays empty; the container's own config names the network.
        cid = created.stdout.split()[0]
        nets = _docker("inspect", "-f", "{{json .HostConfig.NetworkMode}}", cid)
        assert network in nets.stdout or network in str(attached), nets.stdout


def _outcome(up, network: str) -> str:
    """Which way Compose answered, or a failure that never reached the network step."""
    if up.returncode == 0:
        return ADOPTED
    assert network in up.stderr, (
        f"Compose failed before the network step (exit {up.returncode}):\n{up.stderr}"
    )
    return REFUSED


def _record(outcome: str, up) -> None:
    """Print Compose's answer, and add it to the job summary when the image leg runs."""
    text = (
        f"H7.26 Compose network measurement: {outcome} (exit {up.returncode})\n\n"
        f"```\n{up.stderr.strip()}\n```\n"
    )
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as handle:
            handle.write(text)
