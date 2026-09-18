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

The two roles that opt out of the shared restart and roll their own workloads through
private tasks (claude-otel's loop and pihole's roll_one.yml) are pinned in
`test_self_rollouts_follow_the_apply.py`, over the same harness (`_release_expectation.py`).

WHAT THE HARNESS CANNOT REACH. ansible-core 2.21's `default` filter recognises only its own
Undefined, so every var below is passed defined, and the `| default(...)` fallbacks on
`manifests_rollout`, `manifests_rollout_kind` and an extra's `kind`/`image` are exercised by
the real play alone — the same idiom the extra restart task in main.yml has relied on since
it shipped.

Run: uv run pytest ansible/tests/k8s/test_release_stamp_rollout_expectation.py
"""

import re

from _helpers import ANSIBLE, render_expr, task_named, walk_tasks
from _release_expectation import (
    FACT_EXPR,
    MAIN,
    STAMP,
    rollouts as _rollouts,
)


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


def test_the_template_fingerprints_bracket_the_apply_and_precede_the_stamp():
    """Order in main.yml: before-read -> apply -> after-read -> rolled-by-apply fact -> stamp.
    The fact is what the record and the restart tasks read (#1988), so it must be set from a
    read taken AFTER the apply and BEFORE the record is written; a before-read taken after the
    apply would never see the template move."""
    names = [str(t.get("name", "")) for t in walk_tasks(MAIN)]

    def at(prefix):
        return next(i for i, n in enumerate(names) if n.startswith(prefix))

    before = at("Fingerprint the pod templates before the apply")
    apply = at("Apply manifests")
    after = at("Fingerprint the pod templates after the apply")
    reset = at("Reset which workloads the apply itself rolled")
    fact = at("Note which workloads the apply itself rolled")
    stamp = at("Record the applied release")
    assert before < apply < after < reset < fact < stamp, names[before : stamp + 1]


_FINGERPRINT_GATE = "manifests_render is changed or manifests_secret_render is changed"


def test_the_template_fingerprints_read_the_template_not_the_generation():
    """`.metadata.generation` bumps on any spec change — navidrome and terraria template
    `replicas:` — so a replicas change beside a ConfigMap change would read as rolled and skip
    the restart the ConfigMap needs. Both reads must hash the pod template, and the same list
    of targets the restart tasks use."""
    for side in ("before", "after"):
        task = task_named(MAIN, f"Fingerprint the pod templates {side} the apply")
        cmd = task["ansible.builtin.shell"]["cmd"]
        assert "jsonpath='{.spec.template}'" in cmd, cmd
        assert ".metadata.generation" not in cmd
        assert "sha256sum" in cmd, "the register must carry a hash, never the template"
        assert task["loop"] == "{{ manifests_fingerprint_targets }}"
        assert "-n {{ item.namespace | default(k8s_namespace) }}" in cmd, (
            "a self rollout outside k8s_namespace (claude-otel) reads nothing otherwise"
        )
        assert task["failed_when"] is False, (
            "a workload the apply creates has no before side"
        )
        assert task["when"] == _FINGERPRINT_GATE, side
    note = task_named(MAIN, "Note which workloads the apply itself rolled")
    assert note["when"] == _FINGERPRINT_GATE, "consumer must carry the producers' gate"
    replicas = [
        p
        for p in (ANSIBLE / "roles/k8s").glob("*/templates/deployment*.j2")
        if re.search(r"^\s*replicas: \{\{", p.read_text(), re.MULTILINE)
    ]
    assert replicas, "the generation trap this test names no longer exists in the tree"


def test_the_expectation_reads_the_same_facts_as_the_restart_task():
    """A restart condition that gains an ingredient must reach the record too."""
    restart = task_named(MAIN, "Roll the deployment after a config change")
    when = " ".join(restart["when"])
    for ingredient in (
        "manifests_render is changed",
        "manifests_secret_render is changed",
        "' created'",
        "manifests_rolled_by_apply.get(",
    ):
        assert ingredient in when, ingredient
        assert ingredient in FACT_EXPR, ingredient
    assert "manifests_image_changed" in when
    assert "k8s_rebuilt_images" in FACT_EXPR
    extras = task_named(MAIN, "Roll the extra deployments")
    assert "manifests_rolled_by_apply.get(item.name" in " ".join(extras["when"])


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


def test_a_workload_the_apply_itself_rolled_is_not_expected_to_restart():
    """The red half of #1988's pair: an image-pin bump changes the render AND the pod
    template, so the apply rolls prowlarr and no restart is queued for it. flaresolverr's
    template did not move, so the same render change still restarts it."""
    rollouts = _rollouts(
        manifests_rolled_by_apply={"prowlarr": True, "flaresolverr": False}
    )
    assert rollouts["prowlarr"]["restart"] is False
    assert rollouts["flaresolverr"]["restart"] is True


def test_a_changed_render_with_an_unchanged_template_is_still_expected_to_restart():
    """The green half: a ConfigMap-only change leaves every template as it was, which is
    the case the restart exists for."""
    rollouts = _rollouts(
        manifests_rolled_by_apply={"prowlarr": False, "flaresolverr": False}
    )
    assert rollouts["prowlarr"]["restart"] is True
    assert rollouts["flaresolverr"]["restart"] is True


def test_a_target_the_fingerprints_never_saw_is_still_expected_to_restart():
    """The fingerprints run only when the render changed, so an image-only trigger leaves the
    dict empty; every restart task reads that as "not rolled" and restarts, and the record
    must expect the same."""
    rollouts = _rollouts(
        manifests_render={"changed": False},
        k8s_rebuilt_images=["prowlarr"],
        manifests_rolled_by_apply={},
    )
    assert rollouts["prowlarr"]["restart"] is True


_ROLLED_TASK = task_named(MAIN, "Note which workloads the apply itself rolled")
_ROLLED_EXPR = _ROLLED_TASK["ansible.builtin.set_fact"]["manifests_rolled_by_apply"]


def _rolled(before, after):
    """Run the combine loop the way Ansible does, over zipped before/after read results."""
    acc = {}
    for pair in zip(before, after, strict=True):
        acc = render_expr(_ROLLED_EXPR, manifests_rolled_by_apply=acc, item=list(pair))
    return acc


def _read(name, rc=0, stdout="hash-a"):
    return {"item": {"name": name, "kind": "deploy"}, "rc": rc, "stdout": stdout}


def test_a_template_that_moved_across_the_apply_reads_as_rolled():
    assert _rolled([_read("prowlarr")], [_read("prowlarr", stdout="hash-b")]) == {
        "prowlarr": True
    }


def test_a_template_that_held_across_the_apply_reads_as_not_rolled():
    assert _rolled([_read("prowlarr")], [_read("prowlarr")]) == {"prowlarr": False}


def test_a_read_that_failed_on_either_side_reads_as_not_rolled():
    """A missing workload (created by this apply) or a transient kubectl error must fall
    through to the restart — the recoverable direction — never to a skipped one."""
    absent = _read("prowlarr", rc=1, stdout="hash-of-empty")
    assert _rolled([absent], [_read("prowlarr")]) == {"prowlarr": False}
    assert _rolled([_read("prowlarr")], [absent]) == {"prowlarr": False}
    skipped = {"item": {"name": "prowlarr"}, "skipped": True}
    assert _rolled([skipped], [_read("prowlarr")]) == {"prowlarr": False}


def test_the_rolled_fact_is_reset_per_service():
    reset = task_named(MAIN, "Reset which workloads the apply itself rolled")
    assert reset["ansible.builtin.set_fact"]["manifests_rolled_by_apply"] == {}


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
