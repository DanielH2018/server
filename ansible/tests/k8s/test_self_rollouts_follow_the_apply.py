"""The roles that declare `manifests_self_rollouts` follow the record and the apply.

pihole (roll_one.yml) sets `manifests_rollout: ''`, opts out of the shared restart and the
batch drain, and restarts its workloads through a private task after `k8s/manifests` returns.
observability declares its workloads the same way, but it is flux-owned, so the renderer's
restart annotation rolls them and no task of its own does (D5.1e). These properties are pinned
here, over the harness in `_release_expectation.py`:

They declare those workloads to the record as `manifests_self_rollouts`, and
two things hold -- the record decides `restart` for them from the same facts (the red-proof
pair: a changed render names grafana `restart: true`, an unchanged one `false`), and pihole's
private restart runs after the include_role that writes the record.

The pod-template fingerprints cover every `manifests_self_rollouts` entry, and observability's
entries carry the namespace the fingerprints must read in -- the fingerprints default to
`k8s_namespace`, its workloads live in `observability`, and a read in the wrong namespace
fails on both sides and reads as "not rolled", which is the double roll.
pihole's gate sits on the restart task inside roll_one.yml rather than on the include, so the
sibling check and the rollout wait still follow a roll the apply started.

Observability's entries all declare `restart_on: []`, so the record stamps `restart: false`
for each. Its ConfigMaps are hashed into their own pod templates, so the apply rolls those.
The entries carry no `rolled_by_role`, which would drop them from the fingerprints.

The shared apply carries `deployment.yaml`, which holds `pihole` alone: `pihole-2` is applied
later, by tasks/apply_instance_2.yml, and the record is written before that task runs -- so a
plain `pihole-2` entry would name a `restart: true` no `restartedAt` ever satisfies.

The `pihole-2` entry therefore declares `rolled_by_role: true`. The marker says the stamp may
not decide: the entry is recorded with `restart: false` and out of the pod-template fingerprints,
and roll_one.yml raises the expectation afterwards through the shared
`k8s/manifests` `tasks/rollout_amend.yml` -- `restart: true` only where it issued a
`rollout restart`, which is the only roll that stamps a `restartedAt`. What this file pins is
the DECLARATION: that pihole names both instances, marks the deferred one, and records it
`restart: false`. The amend itself is
ansible/tests/k8s/test_rollout_amend_raises_a_deferred_expectation.py, and what covers the second
instance at deploy time is ansible/tests/services/test_pihole_redundancy.py.

Run: uv run pytest ansible/tests/k8s/test_self_rollouts_follow_the_apply.py
"""

from _helpers import load_tasks, load_yaml, render_expr, task_named, walk_tasks
from lib.repo_paths import ALL_VARS
from _release_expectation import (
    LOOP_EXPR,
    MAIN,
    OBSERVABILITY_NAMESPACE,
    PIHOLE,
    observability_self_rollouts,
    include_role_vars,
    rollouts as _rollouts,
)


def _pairs(entries):
    return {(e["kind"], e["name"]) for e in entries}


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
    """The var reaches the record and the fingerprints and nothing else: the shared
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
    # Instance 1 only, even though the declaration names both: the shared
    # apply does not touch pihole-2's pod template, so fingerprinting it across that apply could
    # only ever read "not rolled" — two kubectl calls for a known answer. `rolled_by_role` is
    # what excludes it; apply_instance_2.yml fingerprints it across its OWN apply and writes the
    # answer to the same fact.
    assert {e["name"] for e in fingerprinted} == {"pihole"}, fingerprinted
    assert "manifests_self_rollouts" not in str(
        task_named(MAIN, "Roll the extra deployments after")
    )
    assert "manifests_self_rollouts" not in str(
        task_named(MAIN, "Queue the batch drain for the extra rollouts")
    )


def _observability_rollouts(**over):
    return _rollouts(
        manifests_service="observability",
        manifests_rollout="",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=observability_self_rollouts(),
        **over,
    )


def test_observability_a_changed_render_expects_nothing_to_roll():
    """The red half. Every workload here declares `restart_on` without `config`, because
    each ConfigMap is hashed into its own pod template — the apply rolls the one that changed
    and `manifests_rolled_by_apply` reports it, so no restart TASK runs and the record must
    expect no `restartedAt`. Expecting one is not a quiet failure: `probe.py health` reads it
    as NOT ROLLED and fails the deploy."""
    rollouts = _observability_rollouts(
        manifests_apply={"stdout": "deployment.apps/grafana configured"},
    )
    assert rollouts["otel-collector"]["kind"] == "daemonset"
    assert not any(r["restart"] for r in rollouts.values()), rollouts


def test_observability_a_changed_secret_expects_only_its_secret_readers_to_roll():
    """The stamp leaves a Secret's restart to the role. `manifests_secret_render` changes for
    any one Secret file, so a stamp that decided from it would hold every reader to a roll
    that an Alertmanager config edit never made. Every entry is stamped `restart: false`, and
    the renderer's restart annotation rolls the readers of the changed Secret alone."""
    rollouts = _observability_rollouts(
        manifests_render={"changed": False},
        manifests_secret_render={"changed": True},
        manifests_apply={"stdout": "deployment.apps/grafana configured"},
    )
    assert set(rollouts) >= {"prometheus", "grafana", "alertmanager"}, rollouts
    assert not any(r["restart"] for r in rollouts.values()), rollouts
    assert not any("rolled_by_role" in r for r in rollouts.values()), rollouts


def test_observability_an_unchanged_render_expects_nothing_to_roll():
    """An idempotent re-run names every workload with `restart: false`."""
    rollouts = _observability_rollouts(manifests_render={"changed": False})
    assert rollouts["grafana"]["restart"] is False
    assert not any(r["restart"] for r in rollouts.values()), rollouts


def test_observability_declares_the_namespace_the_fingerprints_must_read():
    """Its workloads live outside k8s_namespace, which the fingerprints default to. A
    declaration without the namespace passes every other test here and double-rolls in the
    play: both reads fail, every entry reads "not rolled", the private restart fires."""
    declared = observability_self_rollouts()
    assert declared, "the declaration rendered empty"
    assert {e["namespace"] for e in declared} == {OBSERVABILITY_NAMESPACE}, declared
    assert OBSERVABILITY_NAMESPACE != load_yaml(ALL_VARS)["k8s_namespace"], (
        "the trap this test guards no longer exists"
    )


def test_pihole_an_image_bump_expects_instance_one_to_roll():
    """roll_one.yml fires on `manifests_image_changed`, which is `manifests_service in
    k8s_rebuilt_images`; the record keys on the entry's `image`, so dropping `image: pihole`
    makes the rebuilt-image trigger read as a miss and the record expect no roll.

    pihole-2 reads `false` on the same run, and must: the stamp runs before apply_instance_2.yml,
    so it cannot know whether that apply will roll instance 2 by changing its pod template
    (stamping no `restartedAt`) or roll_one.yml will restart it (stamping one). `rolled_by_role`
    is what pins it false, and the amend at the bottom of this file is what raises it.

    The unchanged half is the reject: with nothing rebuilt and no changed render the record must
    expect no restart at all, or every idempotent re-run fails `probe.py health`."""
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
        "pihole-2": False,
    }
    assert rollouts["pihole-2"]["rolled_by_role"] is True, rollouts["pihole-2"]
    assert "rolled_by_role" not in rollouts["pihole"], rollouts["pihole"]
    unchanged = _rollouts(
        manifests_service="pihole",
        manifests_rollout="",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=declared,
        manifests_render={"changed": False},
    )
    assert not any(r["restart"] for r in unchanged.values()), unchanged


def test_pihole_declares_both_instances_and_marks_the_deferred_one():
    """The declaration covers the whole restart loop, with `rolled_by_role: true`
    on the instance whose manifest the shared apply does not carry.

    Both halves matter. Dropping the entry loses the expectation entirely. Dropping the marker makes the stamp claim a `restart: true` that an
    apply-rolled instance 2 can never satisfy, failing every image bump at `probe.py health`.
    The loop and the declaration must also still name the same instances, so a third instance
    added to one alone is caught."""
    declared = include_role_vars(PIHOLE)["manifests_self_rollouts"]
    restart = _pihole_private_restart()
    declared_names = {e["name"] for e in declared}
    assert declared_names == set(restart["loop"]) == {"pihole", "pihole-2"}, declared
    assert {e["name"] for e in declared if e.get("rolled_by_role")} == {"pihole-2"}, (
        "instance 2 is the one the shared apply does not carry, so it is the one whose "
        "`restart` the stamp may not decide"
    )
    assert {e["kind"] for e in declared} == {"deploy"}
    roll_one = (PIHOLE / "tasks/roll_one.yml").read_text()
    assert "rollout restart deploy/{{ pihole_instance }}" in roll_one
    when = str(restart["when"])
    for ingredient in (
        "manifests_render is changed",
        "manifests_secret_render is changed",
        "manifests_image_changed",
        # The fourth trigger: instance 2's Deployment renders into
        # its own directory, so a change to it alone moves none of the three facts above and the
        # apply that carries it would never run.
        "manifests_deferred_render",
    ):
        assert ingredient in when, ingredient
    # The fourth ingredient gates the restart task inside roll_one.yml, not the
    # include: the sibling check and the rollout wait still follow a roll the apply started.
    assert "manifests_rolled_by_apply" not in when
    gate = str(_pihole_restart_task()["when"])
    assert "manifests_rolled_by_apply.get(pihole_instance, false)" in gate, gate
    # The second apply's verdict, keyed the same way.
    assert "pihole_k8s_rolled_by_own_apply" in gate, gate


def _pihole_restart_task():
    return task_named(
        load_tasks(PIHOLE / "tasks/roll_one.yml"), "Restart Pi-hole instance"
    )


def _pihole_restart_fires(instance: str, **overrides) -> bool:
    """Render every clause of the restart task's `when` for one loop item, the way Ansible
    ANDs a list. The trigger facts default to "the shared apply changed", so a caller varies
    only the fact under test."""
    ctx = {
        "pihole_instance": instance,
        "manifests_render": {"changed": True},
        "manifests_apply": {"changed": True},
        "manifests_secret_render": {"changed": False},
        "manifests_image_changed": False,
        "manifests_deferred_render": {"changed": False},
        "pihole_k8s_instance_2_apply": {"changed": False},
        "manifests_rolled_by_apply": {},
        "pihole_k8s_rolled_by_own_apply": {},
        **overrides,
    }
    return all(
        render_expr("{{ " + str(clause) + " }}", **ctx) in (True, "True")
        for clause in _pihole_restart_task()["when"]
    )


def test_pihole_skips_its_restart_of_an_instance_the_apply_rolled_but_still_waits():
    """The accept/reject pair on pihole: an instance whose template moved is not
    restarted again, one whose template held (a ConfigMap-only change) is, and the wait
    that follows carries no such gate -- it is the only thing in the play that follows the
    roll the apply started on a Recreate Deployment."""
    rolled = {"pihole": True, "pihole-2": False}
    assert not _pihole_restart_fires("pihole", manifests_rolled_by_apply=rolled)
    assert _pihole_restart_fires("pihole-2", manifests_rolled_by_apply=rolled)
    assert _pihole_restart_fires("pihole")
    # The same pair over the second apply's own verdict: instance 2 is not restarted on
    # top of the apply that just rolled it, and instance 1's restart is untouched by that answer.
    own = {"pihole-2": True}
    assert not _pihole_restart_fires("pihole-2", pihole_k8s_rolled_by_own_apply=own)
    assert _pihole_restart_fires("pihole", pihole_k8s_rolled_by_own_apply=own)
    assert _pihole_restart_fires(
        "pihole-2", pihole_k8s_rolled_by_own_apply={"pihole-2": False}
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


def test_pihole_a_comment_only_edit_restarts_neither_instance():
    """The reject half. A YAML-comment edit moves the rendered bytes while the matching
    apply prints every object `unchanged`, so the restart must wait for that apply's own
    verdict, per instance. Instance 1 reads the shared apply; instance 2 reads its own apply
    for deployment-2.yaml, and the shared apply for the ConfigMap both instances mount."""
    # Shared files: bytes moved, shared apply unchanged.
    for instance in ("pihole", "pihole-2"):
        assert not _pihole_restart_fires(instance, manifests_apply={"changed": False})
    # deployment-2.yaml.j2: deferred bytes moved, pihole-2's own apply unchanged.
    quiet = {
        "manifests_render": {"changed": False},
        "manifests_apply": {"changed": False},
        "manifests_deferred_render": {"changed": True},
        "pihole_k8s_instance_2_apply": {"changed": False},
    }
    assert not _pihole_restart_fires("pihole-2", **quiet)
    assert not _pihole_restart_fires("pihole", **quiet)


def test_pihole_a_changed_apply_restarts_the_instance_it_carries():
    """The accept half. A changed shared apply restarts both instances (the ConfigMap is
    shared), and a changed pihole-2 apply restarts pihole-2 alone. The secret render and an
    image rebuild still restart without any apply verdict: the first because
    verify_secret_keys.yml patches a Secret after an apply that printed `unchanged`."""
    assert _pihole_restart_fires("pihole")
    assert _pihole_restart_fires("pihole-2")
    own = {
        "manifests_render": {"changed": False},
        "manifests_apply": {"changed": False},
        "manifests_deferred_render": {"changed": True},
        "pihole_k8s_instance_2_apply": {"changed": True},
    }
    assert _pihole_restart_fires("pihole-2", **own)
    assert not _pihole_restart_fires("pihole", **own)
    unapplied = {
        "manifests_render": {"changed": False},
        "manifests_apply": {"changed": False},
    }
    assert _pihole_restart_fires(
        "pihole", **unapplied, manifests_secret_render={"changed": True}
    )
    assert _pihole_restart_fires("pihole-2", **unapplied, manifests_image_changed=True)


def test_pihole_instance_two_apply_is_changed_only_by_a_real_apply():
    """pihole-2's apply verdict decides a restart, so its `changed_when` must read
    kubectl's `created`/`configured` and stay false for `unchanged` and for a dry run."""
    task = task_named(
        load_tasks(PIHOLE / "tasks/apply_instance_2.yml"), "Apply pihole-2's Deployment"
    )

    def changed(stdout: str, dry_run: bool = False) -> bool:
        expr = "{{ " + str(task["changed_when"]) + " }}"
        ctx = {
            "k8s_dry_run": dry_run,
            "pihole_k8s_instance_2_apply": {"stdout": stdout},
        }
        return render_expr(expr, **ctx) in (True, "True")

    assert changed("deployment.apps/pihole-2 configured")
    assert changed("deployment.apps/pihole-2 created")
    assert not changed("deployment.apps/pihole-2 unchanged")
    assert not changed("deployment.apps/pihole-2 configured", dry_run=True)
