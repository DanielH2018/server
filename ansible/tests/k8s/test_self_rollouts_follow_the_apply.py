"""The two roles that roll their own workloads follow the record and the apply.

claude-otel (its restart loop) and pihole (roll_one.yml) set `manifests_rollout: ''`, opt out
of the shared restart and the batch drain, and restart their workloads through a private task
after `k8s/manifests` returns. Two issues are pinned here, over the harness in
`_release_expectation.py`:

Issue #1902: they declare those workloads to the record as `manifests_self_rollouts`, and
three things hold -- the record decides `restart` for them from the same facts (the red-proof
pair: a changed render names grafana `restart: true`, an unchanged one `false`), each role's
declaration equals the loop its private restart iterates (a seventh workload added to the loop
alone re-opens the gap with both halves of the pair still green), and the private restart runs
after the include_role that writes the record.

Issue #1994: the skip #1988 added to the shared restart reaches those two private restarts.
The pod-template fingerprints cover every `manifests_self_rollouts` entry, each private
restart reads `manifests_rolled_by_apply` for its own workload, and claude-otel's entries
carry the namespace the fingerprints must read in -- the fingerprints default to
`k8s_namespace`, its six workloads live in `observability`, and a read in the wrong namespace
fails on both sides and reads as "not rolled", which is the double roll the issue names.
pihole's gate sits on the restart task inside roll_one.yml rather than on the include, so the
sibling check and the rollout wait still follow a roll the apply started.

Run: uv run pytest ansible/tests/k8s/test_self_rollouts_follow_the_apply.py
"""

from _helpers import ANSIBLE, load_tasks, load_yaml, render_expr, task_named, walk_tasks
from _release_expectation import (
    CLAUDE_OTEL,
    FACT_EXPR,
    LOOP_EXPR,
    MAIN,
    OBSERVABILITY_NAMESPACE,
    PIHOLE,
    claude_otel_self_rollouts,
    include_role_vars,
    rollouts as _rollouts,
)


def _pairs(entries):
    return {(e["kind"], e["name"]) for e in entries}


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


_SELF_ROLLOUT_READERS_IN_MAIN = (
    "Check that every manifests_extra_rollouts",
    "List the workloads whose pod template is fingerprinted",
)


def test_a_self_rolling_role_records_only_what_it_declares():
    """The var reaches the record and the fingerprints (#1994) and nothing else: the shared
    restart's loop and the drain queue read `manifests_extra_rollouts`, never
    `manifests_self_rollouts`. Those roles opted out of both, and a declaration that
    re-enrolled them would take both Pi-holes down at once."""
    readers = set()
    for task in walk_tasks(MAIN):
        text = str(task)
        if "manifests_self_rollouts" in text:
            assert task["name"].startswith(_SELF_ROLLOUT_READERS_IN_MAIN), task["name"]
            readers.add(task["name"].split(" for ")[0])
    assert len(readers) == len(_SELF_ROLLOUT_READERS_IN_MAIN), readers
    assert "manifests_self_rollouts" in LOOP_EXPR
    targets = task_named(MAIN, "List the workloads whose pod template is fingerprinted")
    fingerprinted = render_expr(
        targets["ansible.builtin.set_fact"]["manifests_fingerprint_targets"],
        manifests_service="pihole",
        manifests_rollout="",
        manifests_rollout_kind="deploy",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=include_role_vars(PIHOLE)["manifests_self_rollouts"],
    )
    assert {e["name"] for e in fingerprinted} == {"pihole", "pihole-2"}, fingerprinted
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
        manifests_self_rollouts=claude_otel_self_rollouts(),
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
        manifests_self_rollouts=claude_otel_self_rollouts(),
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
        manifests_self_rollouts=claude_otel_self_rollouts(),
        manifests_apply={"stdout": "daemonset.apps/otel-collector created"},
    )
    assert rollouts["otel-collector"]["restart"] is False
    assert rollouts["grafana"]["restart"] is True


def test_claude_otel_declares_the_same_workloads_its_private_restart_rolls():
    """A seventh workload added to the restart loop alone re-opens the gap with the pair
    above still green, so the declaration is held equal to the loop."""
    declared = _pairs(claude_otel_self_rollouts())
    restarted = _pairs(_claude_otel_private_restart()["loop"])
    assert declared == restarted, (declared, restarted)
    assert ("deploy", "grafana") in declared


def test_claude_otel_private_restart_reads_the_same_facts_as_the_record():
    when = " ".join(_claude_otel_private_restart()["when"])
    for ingredient in (
        "manifests_render is changed",
        "manifests_secret_render is changed",
        "manifests_rolled_by_apply.get(",
    ):
        assert ingredient in when, ingredient
        assert ingredient in FACT_EXPR, ingredient
    assert " created" in when
    assert "manifests_rolled_by_apply.get(item.name, false)" in when


def test_claude_otel_declares_the_namespace_the_fingerprints_must_read():
    """Its six workloads live outside k8s_namespace, which the fingerprints default to. A
    declaration without the namespace passes every other test here and double-rolls in the
    play: both reads fail, every entry reads "not rolled", the private restart fires."""
    declared = claude_otel_self_rollouts()
    assert declared, "the declaration rendered empty"
    assert {e["namespace"] for e in declared} == {OBSERVABILITY_NAMESPACE}, declared
    assert (
        OBSERVABILITY_NAMESPACE
        != load_yaml(ANSIBLE / "inventory/group_vars/all.yml")["k8s_namespace"]
    ), "the trap this test guards no longer exists"
    private = _claude_otel_private_restart()["ansible.builtin.command"]["cmd"]
    assert "-n {{ k8s_observability_namespace }}" in private, private


def test_claude_otel_skips_its_private_restart_of_a_workload_the_apply_rolled():
    """The accept/reject pair for #1994, rendered through the private restart's own `when`:
    the template moved (an image bump) skips the restart; a ConfigMap-only change, where the
    template held, still fires it."""
    # The new clause alone: the `created` clause beside it carries a `\.apps/` regex
    # escape that Ansible's templar accepts and plain Jinja rejects, and the ingredient test
    # above already holds the clause list.
    (clause,) = [
        c for c in _claude_otel_private_restart()["when"] if "rolled_by_apply" in c
    ]
    item = {"kind": "deploy", "name": "grafana"}
    rolled = dict(item=item, manifests_rolled_by_apply={"grafana": True})
    held = dict(item=item, manifests_rolled_by_apply={"grafana": False})
    unseen = dict(item=item, manifests_rolled_by_apply={})
    assert not render_expr("{{ " + clause + " }}", **rolled)
    assert render_expr("{{ " + clause + " }}", **held)
    assert render_expr("{{ " + clause + " }}", **unseen)
    # And the record agrees, so `probe.py health` expects no `restartedAt` of the skip.
    rollouts = _rollouts(
        manifests_service="claude-otel",
        manifests_rollout="",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=claude_otel_self_rollouts(),
        manifests_apply={"stdout": "deployment.apps/grafana configured"},
        manifests_rolled_by_apply={"grafana": True},
    )
    assert rollouts["grafana"]["restart"] is False
    assert rollouts["loki"]["restart"] is True


def test_pihole_an_image_bump_expects_both_instances_to_roll():
    """roll_one.yml fires on `manifests_image_changed`, which is `manifests_service in
    k8s_rebuilt_images`; the record keys on each entry's `image`, so both instances carry
    `image: pihole` or pihole-2 reads as a miss."""
    declared = include_role_vars(PIHOLE)["manifests_self_rollouts"]
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
    declared = include_role_vars(PIHOLE)["manifests_self_rollouts"]
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
    # The fourth ingredient (#1994) gates the restart task inside roll_one.yml, not the
    # include: the sibling check and the rollout wait still follow a roll the apply started.
    assert "manifests_rolled_by_apply" not in when
    assert _pihole_restart_task()["when"] == (
        "not manifests_rolled_by_apply.get(pihole_instance, false)"
    )


def _pihole_restart_task():
    return task_named(
        load_tasks(PIHOLE / "tasks/roll_one.yml"), "Restart Pi-hole instance"
    )


def test_pihole_skips_its_restart_of_an_instance_the_apply_rolled_but_still_waits():
    """The accept/reject pair for #1994 on pihole: an instance whose template moved is not
    restarted again, one whose template held (a ConfigMap-only change) is, and the wait
    that follows carries no such gate -- it is the only thing in the play that follows the
    roll the apply started on a Recreate Deployment."""
    when = _pihole_restart_task()["when"]
    assert not render_expr(
        "{{ " + when + " }}",
        pihole_instance="pihole",
        manifests_rolled_by_apply={"pihole": True, "pihole-2": False},
    )
    assert render_expr(
        "{{ " + when + " }}",
        pihole_instance="pihole-2",
        manifests_rolled_by_apply={"pihole": True, "pihole-2": False},
    )
    assert render_expr(
        "{{ " + when + " }}", pihole_instance="pihole", manifests_rolled_by_apply={}
    )
    wait = task_named(load_tasks(PIHOLE / "tasks/roll_one.yml"), "Wait for serving")
    assert "when" not in wait, wait
    rollouts = _rollouts(
        manifests_service="pihole",
        manifests_rollout="",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=include_role_vars(PIHOLE)["manifests_self_rollouts"],
        manifests_apply={"stdout": "deployment.apps/pihole configured"},
        manifests_rolled_by_apply={"pihole": True, "pihole-2": True},
    )
    assert not any(r["restart"] for r in rollouts.values()), rollouts


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
