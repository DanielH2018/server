"""The release record names which workloads an apply must roll — the play half of issue #1867.

`probe.py health` reads `rollouts[].restart` from the record `release_stamp.yml` writes and
fails a workload it names whose `restartedAt` is not newer than the record's `applied_at`.
That is only sound if two things hold in the play, and both are pinned here rather than
assumed: the expectation is decided from the SAME facts the `rollout restart` tasks read, and
the stamp is written BEFORE those restarts run (so a restart that reaches the workload stamps
a strictly later time).

The expression is rendered through Ansible's own filters and tests, against fake registers,
so each branch has an accept and a reject: a changed render queues the primary and every
extra; an unchanged one queues nothing; a role with `manifests_rollout: ''` records no entry
at all (six roles roll nothing by design, and a clock-based gate would fail every one); a
workload this apply CREATED is not expected to restart, matching the restart tasks' own
guard.

WHAT THE HARNESS CANNOT REACH. ansible-core 2.21's `default` filter recognises only its own
Undefined, so every var below is passed defined, and the `| default(...)` fallbacks on
`manifests_rollout`, `manifests_rollout_kind` and an extra's `kind`/`image` are exercised by
the real play alone — the same idiom the extra restart task in main.yml has relied on since
it shipped.

Run: uv run pytest ansible/tests/k8s/test_release_stamp_rollout_expectation.py
"""

import re

from _helpers import ANSIBLE, load_tasks, render_expr, task_named, walk_tasks

MANIFESTS_TASKS = ANSIBLE / "roles/k8s/manifests/tasks"
STAMP = load_tasks(MANIFESTS_TASKS / "release_stamp.yml")
MAIN = load_tasks(MANIFESTS_TASKS / "main.yml")
EXPECT = task_named(STAMP, "Work out which workloads this apply must roll")
LOOP_EXPR = EXPECT["loop"]
FACT_EXPR = EXPECT["ansible.builtin.set_fact"]["manifests_release_rollouts"]

BASE = dict(
    manifests_service="prowlarr",
    manifests_rollout="prowlarr",
    manifests_rollout_kind="deploy",
    manifests_extra_rollouts=[
        {"name": "flaresolverr", "kind": "deploy", "image": "flaresolverr"}
    ],
    manifests_render={"changed": True},
    manifests_secret_render={"changed": False},
    manifests_apply={"stdout": "deployment.apps/prowlarr configured"},
    k8s_rebuilt_images=[],
)


def _rollouts(**over):
    """Run the loop + set_fact the way Ansible does: one append per target, accumulating."""
    ctx = {**BASE, **over}
    acc = []
    for target in render_expr(LOOP_EXPR, **ctx):
        acc = render_expr(
            FACT_EXPR,
            manifests_release_rollouts=acc,
            manifests_release_target=target,
            **ctx,
        )
    return {r["name"]: r for r in acc}


def test_the_record_carries_the_rollouts_field():
    """Non-vacuity for the reader: the key `release_expected_restarts` looks for is written."""
    write = task_named(STAMP, "Write the release record")
    assert (
        write["vars"]["manifests_release_record"]["rollouts"]
        == "{{ manifests_release_rollouts }}"
    )
    reset = task_named(STAMP, "Reset the rollout expectation")
    assert reset["ansible.builtin.set_fact"]["manifests_release_rollouts"] == []


def test_the_stamp_runs_after_the_image_fact_and_before_the_restarts():
    """Order in main.yml: image fact -> stamp -> rollout restart. The gate compares the
    record's `applied_at` against the restart's `restartedAt`, so the stamp must precede the
    restart; and the expectation reads `k8s_rebuilt_images`, which is play-scoped, but the
    restart tasks read `manifests_image_changed`, set by the fact task — keeping that first
    keeps the two readers looking at one answer."""
    names = [str(t.get("name", "")) for t in walk_tasks(MAIN)]
    image = next(
        i for i, n in enumerate(names) if n.startswith("Note whether a rebuilt image")
    )
    stamp = next(
        i for i, n in enumerate(names) if n.startswith("Record the applied release")
    )
    restart = next(
        i for i, n in enumerate(names) if n.startswith("Roll the deployment after")
    )
    assert image < stamp < restart, names[image : restart + 1]


def test_the_expectation_reads_the_same_facts_as_the_restart_task():
    """A restart condition that gains an ingredient must reach the record too."""
    restart = task_named(MAIN, "Roll the deployment after a config change")
    when = " ".join(restart["when"])
    for ingredient in (
        "manifests_render is changed",
        "manifests_secret_render is changed",
        "' created'",
    ):
        assert ingredient in when, ingredient
        assert ingredient in FACT_EXPR, ingredient
    assert "manifests_image_changed" in when
    assert "k8s_rebuilt_images" in FACT_EXPR


def test_a_changed_render_queues_the_primary_and_every_extra():
    rollouts = _rollouts()
    assert rollouts == {
        "prowlarr": {"name": "prowlarr", "kind": "deploy", "restart": True},
        "flaresolverr": {"name": "flaresolverr", "kind": "deploy", "restart": True},
    }


def test_an_unchanged_render_queues_nothing():
    """The idempotent re-run: entries are recorded, none expects a restart."""
    rollouts = _rollouts(manifests_render={"changed": False})
    assert {n: r["restart"] for n, r in rollouts.items()} == {
        "prowlarr": False,
        "flaresolverr": False,
    }


def test_a_secret_change_alone_queues_a_restart():
    rollouts = _rollouts(
        manifests_render={"changed": False}, manifests_secret_render={"changed": True}
    )
    assert rollouts["prowlarr"]["restart"] is True


def test_a_rebuilt_image_queues_only_the_workload_it_belongs_to():
    rollouts = _rollouts(
        manifests_render={"changed": False}, k8s_rebuilt_images=["flaresolverr"]
    )
    assert rollouts["prowlarr"]["restart"] is False
    assert rollouts["flaresolverr"]["restart"] is True


def test_a_workload_this_apply_created_is_not_expected_to_restart():
    rollouts = _rollouts(manifests_apply={"stdout": "deployment.apps/prowlarr created"})
    assert rollouts["prowlarr"]["restart"] is False
    assert rollouts["flaresolverr"]["restart"] is True


def test_a_created_daemonset_reads_its_own_apply_prefix():
    rollouts = _rollouts(
        manifests_rollout_kind="daemonset",
        manifests_extra_rollouts=[],
        manifests_apply={"stdout": "daemonset.apps/prowlarr created"},
    )
    assert rollouts == {
        "prowlarr": {"name": "prowlarr", "kind": "daemonset", "restart": False}
    }


def test_a_role_that_rolls_nothing_records_no_entry():
    """`manifests_rollout: ''` skips the shared restart, so no restart is ever expected of
    it — the case that rules out a clock-based predicate."""
    assert _rollouts(manifests_rollout="", manifests_extra_rollouts=[]) == {}


def test_roles_that_roll_nothing_still_exist_in_the_tree():
    """Non-vacuity for the case above: the shape it protects is live, by name."""
    opted_out = {
        p.parent.parent.name
        for p in (ANSIBLE / "roles/k8s").glob("*/tasks/main.yml")
        if re.search(r"manifests_rollout:\s*(''|\"\")", p.read_text())
    }
    assert {"cloudflare-ddns", "pihole", "claude-otel"} <= opted_out, opted_out
