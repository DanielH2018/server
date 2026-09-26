#!/usr/bin/env python3
"""Guards on prune_backups.yml, the playbook that deletes backups nothing else will prune.

Every mode deletes something irreversible, and each has its own safety floor. The floors are the
reason the playbook exists, and each is a one-line edit away from being lost, so each has a test
here that renders the mode's own Jinja against fixture backups and watches it refuse.

  DRY RUN BY DEFAULT, EVERY MODE. Each delete is gated on `prune_apply`, which defaults to false.

  migrated-chain: THE REPLACEMENT MUST ALREADY HAVE A BACKUP. A migrated volume starts with none:
  its old chain belongs to a Longhorn volume that no longer exists. Deleting the chain before the
  replacement has been backed up leaves the service with no recovery point at all. The selection
  takes only orphans, and only this claim's: Longhorn records the PVC inside the KubernetesStatus
  label, a JSON string, and a bare substring match would let `n8n-data` also select
  `n8n-data-something`.

  seeds: THE ROTATION FLOOR. A seed goes only when its volume already holds `prune_seed_floor`
  Completed backups carrying a RecurringJob label, the state retain keeps. The selection needs
  both the `seed-` name and the absent RecurringJob label; either alone matches something that
  is not a seed.

  b2-drain: THE LIVE-VOLUME LIST. scripts/backup/b2_drain.py refuses a live volume and refuses
  everything on an empty list (its own tests cover that). The play's part is to always hand the
  script that list, and to refuse before staging an empty one.

Run: uv run pytest ansible/tests/longhorn/test_prune_backups.py
"""

import json

from _helpers import ANSIBLE
from _helpers import load_yaml
from _helpers import render_expr


PLAY = ANSIBLE / "prune_backups.yml"
MODES = frozenset({"migrated-chain", "seeds", "b2-drain"})


def _play() -> dict:
    return load_yaml(PLAY)[0]


def _mode_file(mode: str):
    return ANSIBLE / "prune_backups" / "tasks" / f"{mode}.yml"


def _tasks(mode: str) -> list:
    return load_yaml(_mode_file(mode))


def _task(mode: str, name: str) -> dict:
    return next(t for t in _tasks(mode) if t.get("name") == name)


def _names(mode: str) -> list[str]:
    return [t["name"] for t in _tasks(mode) if isinstance(t, dict) and "name" in t]


def _test(expression: str, **context):
    """Render a bare `when:`/`that:` expression the way Ansible evaluates it."""
    return render_expr("{{ " + expression + " }}", **context)


def _backup(name, volume, *, state="Completed", job=None, pvc=None) -> dict:
    labels = {}
    if job:
        labels["RecurringJob"] = job
    if pvc:
        # Longhorn writes this label as compact JSON, with no space after the colon.
        labels["KubernetesStatus"] = json.dumps(
            {"pvcName": pvc, "namespace": "homelab"}, separators=(",", ":")
        )
    return {
        "metadata": {"name": name},
        "status": {"volumeName": volume, "state": state, "labels": labels},
    }


# --- shared -----------------------------------------------------------------------------------


def test_the_mode_assert_names_every_mode_and_each_has_a_task_file() -> None:
    play = _play()
    assert set(play["vars"]["prune_modes"]) == MODES
    for mode in MODES:
        assert _mode_file(mode).is_file(), (
            f"prune_mode={mode} includes a file that is missing"
        )
    guard = next(t for t in play["pre_tasks"] if "ansible.builtin.assert" in t)
    that = guard["ansible.builtin.assert"]["that"]
    modes = play["vars"]["prune_modes"]
    assert _test(that, prune_mode="seeds", prune_modes=modes) is True
    assert _test(that, prune_mode="drop-everything", prune_modes=modes) is False
    assert _test(that, prune_modes=modes) is False
    for mode in MODES:
        assert mode in guard["ansible.builtin.assert"]["fail_msg"]


def test_every_delete_is_gated_on_apply_and_apply_defaults_off() -> None:
    assert _play()["vars"]["prune_apply"] is False
    for mode in MODES:
        writes = [t for t in _tasks(mode) if t.get("changed_when") is True]
        assert writes, (
            f"{mode} has no deleting task, so this check would pass vacuously"
        )
        for task in writes:
            assert _test(task["when"], prune_apply=False) is False, (mode, task["name"])
            assert _test(task["when"], prune_apply="true") is True, (mode, task["name"])


def test_the_cost_of_deleting_through_longhorn_is_written_down() -> None:
    """The list reads as cheap because it is short; the cost is per block, not per object."""
    text = PLAY.read_text()
    assert "Class C" in text and "1.28" in text, (
        "the per-block deletion cost is the reason the migrated-chain mode exists instead of the "
        "reaper, and it has to survive someone reading only the header"
    )


# --- migrated-chain ---------------------------------------------------------------------------

_CURRENT = "pvc-new"
_LIVE = [_CURRENT, "pvc-other"]


def _chain_facts(backups: list[dict], claim: str = "sonarr-config") -> dict:
    fact = _task("migrated-chain", "Keep only the ones belonging to this claim")[
        "ansible.builtin.set_fact"
    ]
    orphans_expr = _task(
        "migrated-chain", "Narrow to Completed backups whose volume no longer exists"
    )["ansible.builtin.set_fact"]["prune_chain_orphans"]
    # custom-columns pads names to the column width; the fixture does too.
    context = {
        "prune_claim": claim,
        "prune_chain_backups": {"stdout": json.dumps({"items": backups})},
        "prune_chain_live_volumes": {"stdout_lines": [f"{v}   " for v in _LIVE]},
        "prune_chain_current_pv": {"stdout": _CURRENT},
    }
    context["prune_chain_orphans"] = render_expr(orphans_expr, **context)
    return {
        "context": context,
        "stranded": render_expr(fact["prune_chain_stranded"], **context),
        "current": render_expr(fact["prune_chain_current_backups"], **context),
    }


def _chain_floor_passes(current_backups: list[str]) -> bool:
    that = _task(
        "migrated-chain",
        "Refuse to drop the old chain until the replacement has one of its own",
    )["ansible.builtin.assert"]["that"]
    return _test(that, prune_chain_current_backups=current_backups)


def test_migrated_chain_floor_refuses_a_replacement_with_no_completed_backup() -> None:
    old_chain = [
        _backup(f"old-{i}", "pvc-gone", job="weekly-backup-d6", pvc="sonarr-config")
        for i in range(2)
    ]
    facts = _chain_facts(
        [*old_chain, _backup("new-err", _CURRENT, state="Error", pvc="sonarr-config")]
    )
    assert facts["stranded"] == ["old-0", "old-1"]
    assert _chain_floor_passes(facts["current"]) is False


def test_migrated_chain_floor_allows_a_replacement_with_a_completed_backup() -> None:
    old_chain = [_backup("old-0", "pvc-gone", pvc="sonarr-config")]
    facts = _chain_facts([*old_chain, _backup("new-ok", _CURRENT, pvc="sonarr-config")])
    assert facts["current"] == ["new-ok"]
    assert _chain_floor_passes(facts["current"]) is True


def test_migrated_chain_floor_is_checked_before_the_delete() -> None:
    names = _names("migrated-chain")
    assert names.index(
        "Refuse to drop the old chain until the replacement has one of its own"
    ) < names.index("Delete the stranded backups")


def test_migrated_chain_takes_only_this_claims_completed_orphans() -> None:
    facts = _chain_facts(
        [
            _backup("mine", "pvc-gone", pvc="n8n-data"),
            _backup("longer-name", "pvc-gone-2", pvc="n8n-data-something"),
            _backup("live-chain", "pvc-other", pvc="n8n-data"),
            _backup("errored", "pvc-gone", state="Error", pvc="n8n-data"),
        ],
        claim="n8n-data",
    )
    assert facts["stranded"] == ["mine"]


def test_migrated_chain_refuses_a_volume_list_without_the_claims_own_volume() -> None:
    """An empty or malformed list makes every backup an orphan; the guard must catch it first."""
    task = _task(
        "migrated-chain", "Refuse to classify orphans against an unusable volume list"
    )
    that = task["ansible.builtin.assert"]["that"]
    pv = {"stdout": _CURRENT}
    assert (
        _test(
            that,
            prune_chain_current_pv=pv,
            prune_chain_live_volumes={"stdout_lines": []},
        )
        is False
    )
    assert (
        _test(
            that,
            prune_chain_current_pv=pv,
            prune_chain_live_volumes={"stdout_lines": [f"{_CURRENT}  "]},
        )
        is True
    )
    names = _names("migrated-chain")
    assert names.index(task["name"]) < names.index(
        "Narrow to Completed backups whose volume no longer exists"
    )


# --- seeds ------------------------------------------------------------------------------------


def _seed_is_superseded(completed: list[dict], seed: dict, floor=2) -> bool:
    when = _task("seeds", "Split the seeds into superseded and still-covering")["when"]
    return _test(
        when, prune_seed_completed=completed, item=seed, prune_seed_floor=floor
    )


def test_seeds_floor_refuses_a_volume_below_the_rotation_floor() -> None:
    seed = _backup("seed-pvc-a", "pvc-a", pvc="valheim-config")
    one_rotation = [seed, _backup("backup-1", "pvc-a", job="weekly-backup-d2")]
    assert _seed_is_superseded(one_rotation, seed) is False


def test_seeds_floor_allows_a_volume_at_the_rotation_floor() -> None:
    seed = _backup("seed-pvc-a", "pvc-a", pvc="valheim-config")
    rotation = [
        _backup(f"backup-{i}", "pvc-a", job="weekly-backup-d2") for i in range(2)
    ]
    assert _seed_is_superseded([seed, *rotation], seed) is True
    # An `-e prune_seed_floor=3` arrives as a string; the comparison must still be numeric.
    assert _seed_is_superseded([seed, *rotation], seed, floor="3") is False


def test_seeds_floor_defaults_to_the_shard_retain_and_precedes_the_delete() -> None:
    assert _play()["vars"]["prune_seed_floor"] == 2
    names = _names("seeds")
    assert names.index(
        "Split the seeds into superseded and still-covering"
    ) < names.index("Delete the superseded seeds")
    assert (
        "prune_seed_superseded" in _task("seeds", "Delete the superseded seeds")["loop"]
    )


def test_seeds_selection_requires_both_seed_markers_and_a_live_volume() -> None:
    fact = _task("seeds", "Narrow to seeds on volumes that still exist")[
        "ansible.builtin.set_fact"
    ]["prune_seed_candidates"]
    completed = [
        _backup("seed-pvc-a", "pvc-a"),
        _backup("seed-labelled", "pvc-a", job="weekly-backup-d2"),
        # The live store's wg-easy backup-7e481e73 has this shape: no job label, not a seed.
        _backup("backup-unlabelled", "pvc-a"),
        _backup("seed-pvc-gone", "pvc-gone"),
    ]
    got = render_expr(
        fact,
        prune_seed_completed=completed,
        prune_seed_live_volumes={"stdout_lines": ["pvc-a   "]},
    )
    assert [b["metadata"]["name"] for b in got] == ["seed-pvc-a"]


# --- b2-drain ---------------------------------------------------------------------------------


def _drain_argv(**overrides) -> list[str]:
    task = _task("b2-drain", "Drain the prefixes")
    context = {
        "prune_drain_argv": _play()["vars"]["prune_drain_argv"],
        "prune_drain_live_file": "/tmp/live.txt",
        "prune_volumes": "pvc-aaa",
        "prune_apply": False,
        **overrides,
    }
    return render_expr(task["ansible.builtin.command"]["argv"], **context)


def test_b2_drain_always_hands_the_script_the_live_volume_list() -> None:
    dry = _drain_argv()
    assert dry[:4] == ["uv", "run", "python", "scripts/backup/b2_drain.py"]
    assert dry[dry.index("--live-volumes-file") + 1] == "/tmp/live.txt"
    assert "--apply" not in dry
    assert "--apply" in _drain_argv(prune_apply="true")
    from_file = _drain_argv(prune_volumes_file="/tmp/vols.txt")
    assert "--live-volumes-file" in from_file and "--volumes-file" in from_file


def test_b2_drain_refuses_an_empty_volume_list_before_staging_it() -> None:
    task = _task("b2-drain", "Refuse to drain against an unusable volume list")
    that = task["ansible.builtin.assert"]["that"]
    assert _test(that, prune_drain_live={"stdout_lines": []}) is False
    assert _test(that, prune_drain_live={"stdout_lines": ["pvc-a"]}) is True
    names = _names("b2-drain")
    assert names.index(task["name"]) < names.index(
        "Stage the live-volume list for the script"
    )
