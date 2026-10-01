"""`restart_on` narrows which change signal queues a restart for one rollout entry.

`k8s/manifests` has three signals that queue a `rollout restart` and make the release record
expect a fresh `restartedAt`: the ordinary render changed, the secret render changed, or
`k8s/image-builder` rebuilt the role's image. All three are per-ROLE, which is wrong for a role
that renders several workloads: observability renders six from eight manifests, so one changed
manifest read as "all six changed" and restarted all six (issue #2858).

A rollout entry can now name a subset in `restart_on`. An entry that names none is the role
saying nothing it renders needs a restart TASK for that workload — its config is hashed into
its own pod template, so the apply rolls it and `manifests_rolled_by_apply` reports it.

Two ways that goes quiet, both guarded here:

  * **A misspelt signal.** `restart_on: [secrets]` matches nothing, so the workload is never
    restarted for a rotation and the release record never expects one. Nothing fails.
  * **The default stops naming all three.** Every role that does not narrow relies on it, so a
    dropped signal silently stops restarting ~63 roles.

What a narrowed entry then DOES is rendered through Ansible's own filters in
`ansible/tests/k8s/test_self_rollouts_follow_the_apply.py`, over the harness in
`ansible/tests/_release_expectation.py`. This module covers the two failures that harness
cannot see, because both leave every rendered branch intact.
"""

from _helpers import REPO, load_defaults, load_tasks
from _role_census import role_dirs


_MANIFESTS = REPO / "ansible/roles/k8s/manifests"
_SIGNALS = frozenset({"config", "secret", "image"})

# Roles known to narrow. Keeps the census below non-vacuous: a renamed variable would leave it
# iterating nothing and passing.
_EXPECTED_NARROWERS = frozenset({"observability"})

_TRIGGERS_VAR = "manifests_target_triggers"


def _default_triggers():
    return load_defaults(_MANIFESTS)["manifests_restart_triggers_default"]


def _narrowing_roles():
    """{role: {workload name: restart_on}} for every k8s role that narrows an entry."""
    found = {}
    for role_dir in role_dirs():
        narrowed = {
            entry["name"]: entry["restart_on"]
            for value in (load_defaults(role_dir) or {}).values()
            if isinstance(value, list)
            for entry in value
            if isinstance(entry, dict) and "name" in entry and "restart_on" in entry
        }
        if narrowed:
            found[role_dir.name] = narrowed
    return found


def _stamp_rollouts_task():
    """The set_fact task in release_stamp.yml that builds `manifests_release_rollouts`."""
    return next(
        task
        for task in load_tasks(_MANIFESTS / "tasks" / "release_stamp.yml")
        if "manifests_release_rollouts" in str(task.get("ansible.builtin.set_fact", ""))
        and task.get("loop_control", {}).get("loop_var") == "manifests_release_target"
    )


def test_the_default_still_names_every_signal():
    assert set(_default_triggers()) == _SIGNALS, (
        "manifests_restart_triggers_default no longer names all three signals. Every role that "
        "does not set restart_on reads it, so a dropped signal stops restarting them all."
    )


def test_every_narrowed_entry_names_only_real_signals():
    narrowers = _narrowing_roles()
    assert set(narrowers) >= _EXPECTED_NARROWERS, (
        f"no restart_on found for {sorted(_EXPECTED_NARROWERS - set(narrowers))}. The census "
        "reads role defaults; a renamed variable leaves it checking nothing."
    )
    for role, entries in sorted(narrowers.items()):
        for name, triggers in sorted(entries.items()):
            unknown = set(triggers) - _SIGNALS
            assert not unknown, (
                f"{role}'s {name} declares restart_on: {triggers}, and {sorted(unknown)} is not "
                f"a signal roles/k8s/manifests knows ({sorted(_SIGNALS)}). A name that matches "
                "nothing is never restarted, and nothing reports it."
            )


def test_release_stamp_gates_each_signal_on_the_entrys_triggers():
    expression = str(_stamp_rollouts_task()["ansible.builtin.set_fact"])
    for signal in sorted(_SIGNALS):
        assert f"'{signal}' in {_TRIGGERS_VAR}" in expression, (
            f"the release record's restart clause no longer gates the {signal!r} signal on the "
            "entry's own triggers, so a narrowed entry still expects a restart it never gets — "
            "probe.py health reads that as NOT ROLLED."
        )
