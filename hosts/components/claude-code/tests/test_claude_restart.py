"""Behaviour of the broker that homelab-claude-restart@<name>.service runs.

Each test drives `main` through its seams: a scripted kubectl, a fixed clock and a list that
stands in for Discord.
"""

import datetime as dt
import json

import claude_restart as broker
import pytest
from _setup_render import render_component_text

# Friday 2026-11-20 14:00 UTC. The weekly reboot is Sunday 07:30.
NOW = dt.datetime(2026, 11, 20, 14, 0, tzinfo=dt.UTC).timestamp()
DAY = 86400


def deployment(generation=4, observed=None, updated=1, unavailable=0, flux="docs"):
    return {
        "metadata": {
            "generation": generation,
            "labels": {broker.FLUX_NAME_LABEL: flux} if flux else {},
        },
        "spec": {"replicas": 1},
        "status": {
            "observedGeneration": generation if observed is None else observed,
            "updatedReplicas": updated,
            "unavailableReplicas": unavailable,
        },
    }


class World:
    """A config on disk and a cluster whose Deployment the restart moves on by one generation."""

    def __init__(self, tmp_path, allowlist=None):
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.hold = tmp_path / "hold"
        self.config = tmp_path / "config.json"
        # The config the package ships, so a key the template renames fails here.
        self.cfg = json.loads(
            render_component_text("claude-code", "claude-restart-config.json.j2")
        ) | {
            "allowlist": {"docs": {"namespace": "homelab", "deployment": "docs-web"}}
            if allowlist is None
            else allowlist,
            "state_dir": str(self.state),
            "hold_file": str(self.hold),
        }
        self.live = deployment()
        self.kustomization_ready = True
        self.writes = True
        self.rollout_rc = 0
        self.calls = []
        self.posts = []

    def runner(self, namespace, timeout):
        def kubectl(*args):
            self.calls.append((namespace, *args))
            if args[:2] == ("get", "deployment"):
                return 0, json.dumps(self.live)
            if args[:2] == ("get", "kustomization"):
                status = "True" if self.kustomization_ready else "False"
                return 0, json.dumps(
                    {"status": {"conditions": [{"type": "Ready", "status": status}]}}
                )
            if args[:2] == ("rollout", "restart"):
                if self.writes:
                    self.live["metadata"]["generation"] += 1
                    self.live["status"]["observedGeneration"] += 1
                return 0, "deployment.apps/docs-web restarted"
            if args[:2] == ("rollout", "status"):
                return self.rollout_rc, ""
            raise AssertionError(f"unexpected kubectl {args}")

        return kubectl

    def run(self, name="docs", now=NOW):
        self.config.write_text(json.dumps(self.cfg))
        rc = broker.main(
            ["claude_restart.py", name],
            config_path=str(self.config),
            now=lambda: now,
            runner=self.runner,
            post=lambda content, cfg: self.posts.append(content),
        )
        return rc, (self.state / "verdict").read_text()

    def restarts(self):
        return [c for c in self.calls if c[1:3] == ("rollout", "restart")]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_a_listed_workload_is_restarted_by_the_entrys_names_and_reported(world) -> None:
    rc, verdict = world.run()
    assert rc == 0
    assert verdict.startswith("RESTART-RESULT: name=docs verdict=restarted")
    # The namespace and Deployment are the entry's, and the manager is not a `kubectl` one.
    assert world.restarts() == [
        (
            "homelab",
            "rollout",
            "restart",
            "deployment/docs-web",
            "--field-manager=claude-restart",
        )
    ]
    assert len(world.posts) == 1 and "docs" in world.posts[0]
    logged = (world.state / "log.jsonl").read_text().splitlines()
    assert [json.loads(line)["name"] for line in logged] == ["docs"]


def test_an_empty_allowlist_refuses_every_name_and_posts_nothing(tmp_path) -> None:
    world = World(tmp_path, allowlist={})
    rc, verdict = world.run()
    assert rc == 0
    assert "verdict=refused cause=not-listed" in verdict
    assert world.calls == [] and world.posts == []


def test_the_hold_file_refuses_a_listed_workload(world) -> None:
    world.hold.touch()
    rc, verdict = world.run()
    assert rc == 0 and "verdict=refused cause=hold" in verdict
    assert world.calls == []
    world.hold.unlink()
    assert "verdict=restarted" in world.run()[1]


def test_the_weekly_reboot_mute_refuses_because_a_failure_would_page_nobody(
    world,
) -> None:
    sunday_0740 = dt.datetime(2026, 11, 22, 7, 40, tzinfo=dt.UTC).timestamp()
    rc, verdict = world.run(now=sunday_0740)
    assert rc == 0 and "verdict=refused cause=muted" in verdict
    assert world.calls == []


def test_a_second_restart_of_one_workload_waits_out_its_cooldown(world) -> None:
    world.run()
    rc, verdict = world.run(now=NOW + DAY - 60)
    assert rc == 0 and "verdict=refused cause=rate" in verdict
    assert "next allowed 2026-11-21T14:00Z" in verdict
    assert len(world.restarts()) == 1
    assert "verdict=restarted" in world.run(now=NOW + DAY)[1]


def test_the_daily_cap_counts_every_workloads_restarts(tmp_path) -> None:
    names = ["a", "b", "c", "d"]
    world = World(
        tmp_path,
        allowlist={n: {"namespace": "homelab", "deployment": n} for n in names},
    )
    verdicts = [world.run(name=n, now=NOW + i)[1] for i, n in enumerate(names)]
    assert ["verdict=restarted" in v for v in verdicts] == [True, True, True, False]
    assert "cause=rate" in verdicts[3] and "the cap is 3 a day" in verdicts[3]


@pytest.mark.parametrize(
    "change",
    [
        lambda w: w.live["status"].update(observedGeneration=3),
        lambda w: w.live["status"].update(updatedReplicas=0),
        lambda w: w.live["status"].update(unavailableReplicas=1),
        lambda w: setattr(w, "kustomization_ready", False),
    ],
    ids=["unobserved", "updating", "unavailable", "kustomization-not-ready"],
)
def test_a_deployment_mid_rollout_or_mid_apply_is_refused_as_busy(
    world, change
) -> None:
    change(world)
    rc, verdict = world.run()
    assert rc == 0 and "verdict=refused cause=busy" in verdict
    assert world.restarts() == []


def test_a_restart_that_changed_no_generation_fails_and_still_counts(world) -> None:
    """kubectl prints success for an identity that cannot write (ADR 0012)."""
    world.writes = False
    rc, verdict = world.run()
    assert rc == 1 and "verdict=failed cause=no-write" in verdict
    assert len(world.posts) == 1
    assert "cause=rate" in world.run(now=NOW + 60)[1]


def test_a_rollout_that_does_not_finish_fails(world) -> None:
    world.rollout_rc = 1
    rc, verdict = world.run()
    assert rc == 1 and "verdict=failed cause=rollout" in verdict
    assert len(world.posts) == 1


def test_a_name_outside_the_units_shape_exits_2_before_any_read(world) -> None:
    world.config.write_text(json.dumps(world.cfg))
    rc = broker.main(
        ["claude_restart.py", "docs --all"],
        config_path=str(world.config),
        runner=world.runner,
    )
    assert rc == 2
    assert world.calls == [] and not (world.state / "verdict").exists()
