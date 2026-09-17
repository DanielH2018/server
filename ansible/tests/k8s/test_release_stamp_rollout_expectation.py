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

The second half (issue #1902) covers the two roles that opt out of the shared restart and roll
their own workloads through private tasks -- claude-otel's loop and pihole's roll_one.yml.
They declare those workloads to the record as `manifests_self_rollouts`, and three things are
pinned: the record decides `restart` for them from the same facts (the red-proof pair the
issue asks for: a changed render names grafana `restart: true`, an unchanged one `false`),
each role's declaration equals the loop its private restart iterates (a seventh workload
added to the loop alone re-opens the gap with both halves of the pair still green), and the
private restart runs after the include_role that writes the record.

WHAT THE HARNESS CANNOT REACH. ansible-core 2.21's `default` filter recognises only its own
Undefined, so every var below is passed defined, and the `| default(...)` fallbacks on
`manifests_rollout`, `manifests_rollout_kind` and an extra's `kind`/`image` are exercised by
the real play alone — the same idiom the extra restart task in main.yml has relied on since
it shipped.

Run: uv run pytest ansible/tests/k8s/test_release_stamp_rollout_expectation.py
"""

import re

from _helpers import ANSIBLE, load_tasks, render_expr, task_named, walk_tasks

from _helpers import load_yaml

MANIFESTS_TASKS = ANSIBLE / "roles/k8s/manifests/tasks"
STAMP = load_tasks(MANIFESTS_TASKS / "release_stamp.yml")
MAIN = load_tasks(MANIFESTS_TASKS / "main.yml")
CLAUDE_OTEL = ANSIBLE / "roles/k8s/claude-otel"
PIHOLE = ANSIBLE / "roles/k8s/pihole"
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
    manifests_self_rollouts=[],
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
    """Non-vacuity for the case above: the shape it protects is live, by name.

    cloudflare-ddns's `manifests_rollout: ''` is protected by the record's silence (no
    entry, no expectation). claude-otel and pihole ALSO restart their own workloads through
    private `rollout restart` tasks, and declare them as `manifests_self_rollouts` so the
    record holds the expectation for them -- the tests below this one."""
    opted_out = {
        p.parent.parent.name
        for p in (ANSIBLE / "roles/k8s").glob("*/tasks/main.yml")
        if re.search(r"manifests_rollout:\s*(''|\"\")", p.read_text())
    }
    assert {"cloudflare-ddns", "pihole", "claude-otel"} <= opted_out, opted_out


# --- roles that roll their own workloads (issue #1902) -------------------------------------


def _include_role_vars(role_dir):
    """The `vars:` a role hands to `include_role: k8s/manifests`."""
    for task in walk_tasks(load_tasks(role_dir / "tasks/main.yml")):
        include = task.get("ansible.builtin.include_role") or task.get("include_role")
        if isinstance(include, dict) and include.get("name") == "k8s/manifests":
            return task["vars"]
    raise AssertionError(f"{role_dir.name} does not include k8s/manifests")


def _pairs(entries):
    return {(e["kind"], e["name"]) for e in entries}


def _claude_otel_self_rollouts():
    """The declaration as the play sees it: a template over the role's defaults."""
    declared = _include_role_vars(CLAUDE_OTEL)["manifests_self_rollouts"]
    defaults = load_yaml(CLAUDE_OTEL / "defaults/main.yml")
    return render_expr(declared, **defaults)


def _claude_otel_private_restart():
    return task_named(
        load_tasks(CLAUDE_OTEL / "tasks/main.yml"),
        "Restart the telemetry workloads after a config change",
    )


def _pihole_private_restart():
    return task_named(
        load_tasks(PIHOLE / "tasks/main.yml"),
        "Roll the Pi-hole instances one at a time",
    )


def test_a_self_rolling_role_records_only_what_it_declares():
    """The var reaches the record and nothing else: the shared restart's loop and the drain
    queue read `manifests_extra_rollouts`, never `manifests_self_rollouts`. Those roles opted
    out of both, and a declaration that re-enrolled them would take both Pi-holes down at
    once."""
    for task in walk_tasks(MAIN):
        text = str(task)
        if "manifests_self_rollouts" in text:
            assert task["name"].startswith(
                "Check that every manifests_extra_rollouts"
            ), task["name"]
    assert "manifests_self_rollouts" in LOOP_EXPR
    assert "manifests_self_rollouts" not in str(
        task_named(MAIN, "Roll the extra deployments after")
    )
    assert "manifests_self_rollouts" not in str(
        task_named(MAIN, "Queue the batch drain for the extra rollouts")
    )


def test_claude_otel_a_changed_render_expects_grafana_to_roll():
    """The red half of the issue's pair: the role where 19 dead panels sat behind a 1/1 pod."""
    rollouts = _rollouts(
        manifests_service="claude-otel",
        manifests_rollout="",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=_claude_otel_self_rollouts(),
        manifests_apply={"stdout": "deployment.apps/grafana configured"},
    )
    assert rollouts["grafana"] == {"name": "grafana", "kind": "deploy", "restart": True}
    assert rollouts["otel-collector"]["kind"] == "daemonset"
    assert all(r["restart"] for r in rollouts.values()), rollouts


def test_claude_otel_an_unchanged_render_expects_nothing_to_roll():
    """The green half: an idempotent re-run names the six with `restart: false`."""
    rollouts = _rollouts(
        manifests_service="claude-otel",
        manifests_rollout="",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=_claude_otel_self_rollouts(),
        manifests_render={"changed": False},
    )
    assert rollouts["grafana"]["restart"] is False
    assert not any(r["restart"] for r in rollouts.values()), rollouts


def test_claude_otel_a_created_daemonset_is_not_expected_to_roll():
    """The private loop's guard is `.apps/<name> created`, kind-agnostic; the record's is
    per kind, so the DaemonSet's prefix has to resolve or the record would expect a restart
    of a workload the loop skipped."""
    rollouts = _rollouts(
        manifests_service="claude-otel",
        manifests_rollout="",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=_claude_otel_self_rollouts(),
        manifests_apply={"stdout": "daemonset.apps/otel-collector created"},
    )
    assert rollouts["otel-collector"]["restart"] is False
    assert rollouts["grafana"]["restart"] is True


def test_claude_otel_declares_the_same_workloads_its_private_restart_rolls():
    """A seventh workload added to the restart loop alone re-opens the gap with the pair
    above still green, so the declaration is held equal to the loop."""
    declared = _pairs(_claude_otel_self_rollouts())
    restarted = _pairs(_claude_otel_private_restart()["loop"])
    assert declared == restarted, (declared, restarted)
    assert ("deploy", "grafana") in declared


def test_claude_otel_private_restart_reads_the_same_facts_as_the_record():
    when = " ".join(_claude_otel_private_restart()["when"])
    for ingredient in (
        "manifests_render is changed",
        "manifests_secret_render is changed",
    ):
        assert ingredient in when, ingredient
        assert ingredient in FACT_EXPR, ingredient
    assert " created" in when


def test_pihole_an_image_bump_expects_both_instances_to_roll():
    """roll_one.yml fires on `manifests_image_changed`, which is `manifests_service in
    k8s_rebuilt_images`; the record keys on each entry's `image`, so both instances carry
    `image: pihole` or pihole-2 reads as a miss."""
    declared = _include_role_vars(PIHOLE)["manifests_self_rollouts"]
    rollouts = _rollouts(
        manifests_service="pihole",
        manifests_rollout="",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=declared,
        manifests_render={"changed": False},
        k8s_rebuilt_images=["pihole"],
    )
    assert {n: r["restart"] for n, r in rollouts.items()} == {
        "pihole": True,
        "pihole-2": True,
    }
    unchanged = _rollouts(
        manifests_service="pihole",
        manifests_rollout="",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=declared,
        manifests_render={"changed": False},
    )
    assert not any(r["restart"] for r in unchanged.values()), unchanged


def test_pihole_declares_the_same_instances_roll_one_restarts():
    declared = _include_role_vars(PIHOLE)["manifests_self_rollouts"]
    restart = _pihole_private_restart()
    assert {e["name"] for e in declared} == set(restart["loop"])
    assert {e["kind"] for e in declared} == {"deploy"}
    roll_one = (PIHOLE / "tasks/roll_one.yml").read_text()
    assert "rollout restart deploy/{{ pihole_instance }}" in roll_one
    when = str(restart["when"])
    for ingredient in (
        "manifests_render is changed",
        "manifests_secret_render is changed",
        "manifests_image_changed",
    ):
        assert ingredient in when, ingredient


def test_the_private_restarts_run_after_the_record_is_written():
    """The gate compares `applied_at` against `restartedAt`, so the include_role that writes
    the record must precede the private restart in each role."""
    for role_dir, restart_name in (
        (CLAUDE_OTEL, "Restart the telemetry workloads after a config change"),
        (PIHOLE, "Roll the Pi-hole instances one at a time"),
    ):
        names = [
            str(t.get("name", ""))
            for t in walk_tasks(load_tasks(role_dir / "tasks/main.yml"))
        ]
        include = next(
            i
            for i, t in enumerate(walk_tasks(load_tasks(role_dir / "tasks/main.yml")))
            if (t.get("ansible.builtin.include_role") or {}).get("name")
            == "k8s/manifests"
        )
        restart = next(i for i, n in enumerate(names) if n.startswith(restart_name))
        assert include < restart, (role_dir.name, names[include], names[restart])
