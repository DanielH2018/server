"""`k8s/manifests` tasks/rollout_amend.yml raises an expectation the stamp could not decide.

release_stamp.yml runs before the owning role's private apply, so for a workload whose own
manifest that later apply carries — pihole's instance 2, a deferred manifest since #2899 — it
cannot decide `restart`. The entry declares `rolled_by_role: true`, the record says
`restart: false`, and this file is what the owner calls afterwards (#2902).

Two properties, each with the input it must accept and the input it must reject:

  * only a `kubectl rollout restart` raises the expectation, because it is the only roll that
    stamps a `restartedAt`. A pod template the owner's own apply changed rolls the workload and
    stamps nothing, so raising it there would fail a roll that happened — the state #2884 avoided
    by dropping the entry altogether;
  * the amend can lower an expectation to nothing and never invent one. A record that is missing
    or unreadable leaves `restart: false`, which is no expectation rather than a false one.

The declaration side — that pihole names both instances and marks the deferred one — is in
ansible/tests/k8s/test_self_rollouts_follow_the_apply.py.

Run: uv run pytest ansible/tests/k8s/test_rollout_amend_raises_a_deferred_expectation.py
"""

import base64
import json

from _helpers import ANSIBLE, load_tasks, render_expr, task_named, walk_tasks
from _release_expectation import PIHOLE

AMEND = load_tasks(ANSIBLE / "roles/k8s/manifests/tasks/rollout_amend.yml")


def _amended(recorded, restart, workload="pihole-2"):
    """The `rollouts[]` the amend writes, accumulated one entry at a time as the play does."""
    task = task_named(AMEND, "Rewrite the rollout expectation")
    acc = []
    for entry in recorded:
        acc = render_expr(
            task["ansible.builtin.set_fact"]["manifests_amend_rollouts"],
            manifests_amend_rollouts=acc,
            item=entry,
            manifests_amend_workload=workload,
            manifests_amend_restart=restart,
        )
    return {r["name"]: r for r in acc}


_RECORDED = [
    {"name": "pihole", "kind": "deploy", "restart": True},
    {"name": "pihole-2", "kind": "deploy", "restart": False, "rolled_by_role": True},
]


def test_a_restart_the_owning_role_issued_raises_the_recorded_expectation():
    """The accept half. roll_one.yml restarted pihole-2, so a `restartedAt` newer than the
    record's `applied_at` exists and `probe.py health` must require it — that is the post-hoc
    check #2902 filed as missing."""
    amended = _amended(_RECORDED, restart=True)
    assert amended["pihole-2"]["restart"] is True
    assert amended["pihole-2"]["rolled_by_role"] is True, (
        "the marker says who owns the roll and must survive the amend"
    )
    assert amended["pihole"] == _RECORDED[0], (
        "every other entry is carried through untouched"
    )


def test_a_roll_the_owning_apply_did_leaves_the_expectation_alone():
    """The reject half, and the one that matters most. When apply_instance_2.yml rolled pihole-2
    by changing its pod template, no `restartedAt` was stamped — so raising the expectation would
    fail a roll that did happen, which is exactly what #2884 avoided by dropping the entry. The
    amend must leave `restart: false` on that run."""
    amended = _amended(_RECORDED, restart=False)
    assert amended["pihole-2"]["restart"] is False
    assert amended["pihole"]["restart"] is True


def test_an_unreadable_record_amends_nothing():
    """The amend's whole safety argument is this direction: it can lower an expectation to
    nothing but never invent one. A first-ever deploy whose stamp has not written a record yet,
    or an unreadable one, leaves the `slurp` with no content — the loop must then read zero
    entries and the write must be skipped, rather than rewriting the record from a partial
    reading of itself.

    The accept half is a record that IS readable: the same expressions must find its entries.
    """
    task = task_named(AMEND, "Rewrite the rollout expectation")
    write = task_named(AMEND, "Write the amended release record")
    recorded = json.dumps({"service": "pihole", "rollouts": _RECORDED})
    readable = {"content": base64.b64encode(recorded.encode()).decode()}

    assert render_expr(task["loop"], manifests_amend_slurp=readable) == _RECORDED
    assert render_expr(task["loop"], manifests_amend_slurp={}) == []
    assert render_expr(
        "{{ " + write["when"] + " }}", manifests_amend_rollouts=_RECORDED
    )
    assert not render_expr("{{ " + write["when"] + " }}", manifests_amend_rollouts=[])


def test_the_amend_is_driven_by_whether_the_restart_task_ran():
    """The value pihole passes is the restart task's own register, not a re-derivation of the
    gate. A `when:` clause copied into roll_one.yml's amend would drift from the gate on the
    restart task, and the record would then claim a `restartedAt` nothing stamped."""
    restart = task_named(
        load_tasks(PIHOLE / "tasks/roll_one.yml"), "Restart Pi-hole instance"
    )
    assert restart["register"] == "pihole_k8s_restarted"
    amend = task_named(
        load_tasks(PIHOLE / "tasks/roll_one.yml"), "Record that pihole-2"
    )
    assert amend["ansible.builtin.include_role"]["tasks_from"] == "rollout_amend.yml"
    assert (
        amend["vars"]["manifests_amend_restart"]
        == "{{ pihole_k8s_restarted is changed }}"
    )
    assert amend["vars"]["manifests_amend_workload"] == "pihole-2"
    assert str(amend["when"]) == "pihole_instance == 'pihole-2'"


def test_the_amend_runs_after_the_wait_that_proves_the_roll_finished():
    """Ahead of the `rollout status` wait the record would claim a roll still in flight, and a
    failure in that wait would leave the claim behind. The whole point of the field is that a
    later `probe.py health` can trust it."""
    names = [
        str(t.get("name", ""))
        for t in walk_tasks(load_tasks(PIHOLE / "tasks/roll_one.yml"))
    ]
    wait = next(i for i, n in enumerate(names) if n.startswith("Wait for serving"))
    amend = next(i for i, n in enumerate(names) if n.startswith("Record that pihole-2"))
    assert wait < amend, names
