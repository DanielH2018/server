"""The soak Job's restart legs over a pod with a native sidecar and a plain init container.

A native sidecar is an initContainer with `restartPolicy: Always`: kubelet reports it under
`initContainerStatuses` and kube-state-metrics as `kube_pod_init_container_*`. Its restarts
count like a container's; a plain init container's retries happen before the pod starts and
never count (#7804).

Run: uv run pytest scripts/renderer/tests/test_flux_soak_sidecars.py
"""

import pytest

from _soak_world import SCRAPE, T0, World, iso, pod, run

SIDECAR, PLAIN = "ghcr-resume", "setup"


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "soak-abc-x1")
    world = World()
    p = pod("authelia", "authelia-1")
    p["spec"] = {
        "initContainers": [
            {"name": PLAIN},
            {"name": SIDECAR, "restartPolicy": "Always"},
        ],
        "containers": [{"name": "app"}],
    }
    p["status"]["initContainerStatuses"] = [
        {"name": PLAIN, "restartCount": 0, "lastState": {}},
        {"name": SIDECAR, "restartCount": 0, "lastState": {}},
    ]
    world.pods["authelia"] = [p]
    _serve_init_container_series(world)
    return world


def _serve_init_container_series(world: World) -> None:
    """Answer kube-state-metrics' init-container series as `World` answers the containers'."""
    answer = world.query

    def names(p):
        spec = p.get("spec") or {}
        return [c["name"] for c in spec.get("containers") or [{"name": "app"}]] + [
            c["name"] for c in spec.get("initContainers") or []
        ]

    def query(promql, at):
        if "init_container_info" in promql and "restarts_total" not in promql:
            return [
                {
                    "metric": {
                        "namespace": p["metadata"]["namespace"],
                        "pod": p["metadata"]["name"],
                        "container": c["name"],
                        "restart_policy": c.get("restartPolicy", "Never"),
                    }
                }
                for p in world._pods()
                for c in (p.get("spec") or {}).get("initContainers") or []
            ]
        if "restarts_total" in promql:
            samples = [
                [t, "0"]
                for t in range(int(T0), int(world.now), SCRAPE)
                if not any(lo <= t <= hi for lo, hi in world.scrape_gaps)
            ]
            return [
                {
                    "metric": {
                        "namespace": p["metadata"]["namespace"],
                        "pod": p["metadata"]["name"],
                        "container": name,
                    },
                    "values": samples,
                }
                for p in world._pods()
                for name in names(p)
            ]
        return answer(promql, at)

    world.query = query


def restart_init(world: World, name: str, at: float) -> None:
    for status in world.pods["authelia"][0]["status"]["initContainerStatuses"]:
        if status["name"] == name:
            status["restartCount"] += 1
            status["lastState"] = {"terminated": {"finishedAt": iso(at)}}


def test_a_sidecar_restart_after_the_first_ready_fails_and_names_authelia(
    world, tmp_path, capsys
):
    restart_init(world, SIDECAR, T0 + 100)
    assert run(world, tmp_path) == (1, "failed-services: authelia\n")
    assert f"authelia/authelia-1/{SIDECAR}" in capsys.readouterr().err


def test_a_sidecar_restart_during_the_wait_fails(world, tmp_path, capsys):
    world.on_sleep = lambda w: restart_init(w, SIDECAR, T0 + 20)
    assert run(world, tmp_path) == (1, "failed-services: authelia\n")
    assert f"{SIDECAR} restarted during the soak" in capsys.readouterr().err


def test_a_sidecar_restart_inside_a_scrape_gap_fails_naming_the_gap(
    world, tmp_path, capsys
):
    restart_init(world, SIDECAR, T0 + 30)
    world.scrape_gaps = [(T0 - 1, T0 + 100)]
    assert run(world, tmp_path)[0] == 1
    err = capsys.readouterr().err
    assert f"{SIDECAR} restarted at {iso(T0 + 30)}" in err
    assert "inside a kube-state-metrics scrape gap" in err


def test_a_plain_init_containers_retries_never_count_beside_a_sidecars_restart(
    world, tmp_path, capsys
):
    restart_init(world, PLAIN, T0 + 100)
    restart_init(world, SIDECAR, T0 + 100)
    world.on_sleep = lambda w: restart_init(w, PLAIN, T0 + 20)
    assert run(world, tmp_path) == (1, "failed-services: authelia\n")
    failures = [
        line for line in capsys.readouterr().err.splitlines() if "authelia/" in line
    ]
    assert any(f"/{SIDECAR}" in line for line in failures)
    assert not any(f"/{PLAIN}" in line for line in failures)


def _split_restart_families(world: World) -> None:
    """Serve each restart family only its own containers, as kube-state-metrics does."""
    answer = world.query

    def query(promql, at):
        if "restarts_total" not in promql:
            return answer(promql, at)
        p = world.pods["authelia"][0]
        family = "initContainers" if "init_container" in promql else "containers"
        names = {c["name"] for c in p["spec"][family]}
        return [s for s in answer(promql, at) if s["metric"]["container"] in names]

    world.query = query


def test_a_sidecar_restart_reads_its_init_container_series_not_a_scrape_gap(
    world, tmp_path, capsys
):
    _split_restart_families(world)
    restart_init(world, SIDECAR, T0 + 100)
    assert run(world, tmp_path) == (1, "failed-services: authelia\n")
    err = capsys.readouterr().err
    assert f"authelia/authelia-1/{SIDECAR} restarted after its first Ready" in err
    assert "scrape gap" not in err
