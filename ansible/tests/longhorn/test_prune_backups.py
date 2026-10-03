#!/usr/bin/env python3
"""Guards on prune_backups.yml, the playbook that deletes stranded B2 objects.

It deletes something irreversible, so its floor gets a test here that renders the play's own
Jinja and watches it refuse.

  DRY RUN BY DEFAULT. The delete is gated on `prune_apply`, which defaults to false.

  b2-drain: THE LIVE-VOLUME LIST. scripts/backup/b2_drain.py refuses a live volume and refuses
  everything on an empty list (its own tests cover that). The play's part is to always hand the
  script that list, and to refuse before staging an empty one.

  WHAT IS NO LONGER HERE. The migrated-chain and seeds modes deleted Longhorn Backup CRs by
  selecting in kubectl and Jinja; #3279 made them selectors in
  scripts/backup/longhorn_reap_orphan_backups.py, where each floor is provable against a
  fixture. Their tests moved with them, to `scripts/backup/tests/test_longhorn_reap_logic.py`
  and `scripts/backup/tests/test_longhorn_reap_backups_cli.py`. Two things stay here: the
  redirect, because an operator typing the old command must get the new command rather than a
  missing task file, and the census below that keeps Backup-CR deletion out of this playbook.

Run: uv run pytest ansible/tests/longhorn/test_prune_backups.py
"""

import re

from _helpers import ANSIBLE
from _helpers import load_yaml
from _helpers import render_expr


PLAY = ANSIBLE / "prune_backups.yml"
MODES = frozenset({"b2-drain"})
RETIRED_MODES = ("migrated-chain", "seeds")
REAPER = "longhorn_reap_orphan_backups.py"
# The Backup CR as kubectl takes it, anchored on the separator that follows the resource name —
# a `/` before the object name, or the `,` or whitespace that ends an argv element. A bare `in`
# over the dotted name reads to CodeQL as an unanchored hostname check
# (the `no-host-shaped-membership-literal` row of
# ansible/tests/repo/test_census_rows_python.py).
BACKUP_CR = re.compile(r"backups\.longhorn\.io[/,\s\"']")


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
    assert _test(that, prune_mode="b2-drain", prune_modes=modes) is True
    assert _test(that, prune_mode="drop-everything", prune_modes=modes) is False
    assert _test(that, prune_modes=modes) is False
    for mode in MODES:
        assert mode in guard["ansible.builtin.assert"]["fail_msg"]


def test_a_retired_mode_is_redirected_to_the_reaper_not_left_to_fail() -> None:
    """`-e prune_mode=seeds` is in an operator's shell history; the refusal owes them the move.

    The two modes became `--mode` selectors on the reaper in #3279. Without the names and the
    script in this message, the old command fails on a missing task file and says nothing about
    where the mode went.
    """
    guard = next(t for t in _play()["pre_tasks"] if "ansible.builtin.assert" in t)
    fail_msg = guard["ansible.builtin.assert"]["fail_msg"]
    for mode in RETIRED_MODES:
        assert mode in fail_msg, f"the refusal does not say where {mode} went"
    assert REAPER in fail_msg
    for mode in RETIRED_MODES:
        assert not _mode_file(mode).exists(), (
            f"{mode}.yml is back; its selection belongs in longhorn_reap_logic.py"
        )


def test_this_playbook_deletes_no_longhorn_backup_crs() -> None:
    """Every Backup CR deletion selects in longhorn_reap_logic.py, where the floors are tested.

    The retired modes deleted `backups.longhorn.io/<name>` from a Jinja-built list. A new mode
    reintroducing that here would reintroduce the untested selection with it, which is the whole
    point of #3279.
    """
    texts = {
        path.name: path.read_text()
        for path in sorted((ANSIBLE / "prune_backups" / "tasks").glob("*.yml"))
    }
    assert set(texts) == {f"{mode}.yml" for mode in MODES}, (
        "the task-file census no longer matches prune_modes, so it reads the wrong files"
    )
    # Control: the retired modes' own delete argv must match, or this census passes on nothing.
    assert BACKUP_CR.search(
        'argv: [k3s, kubectl, -n, longhorn-system, delete, "backups.longhorn.io/{{ item }}"]'
    )
    offenders = [name for name, text in texts.items() if BACKUP_CR.search(text)]
    assert offenders == [], (
        f"{offenders} delete or select Backup CRs; that path is {REAPER} --mode"
    )


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
    """Why this playbook exists beside the reaper: the B2 API path is the cheap one.

    A Longhorn deletion's cost is per stored block, so a short list of backups is not a cheap
    one, and that is the whole reason an operator reaches for a B2-API drain instead. It has to
    survive someone reading only the header.
    """
    text = PLAY.read_text()
    assert "Class C" in text and "1.28" in text, (
        "the per-block deletion cost is the reason this B2-API path exists beside the reaper, "
        "and it has to survive someone reading only the header"
    )


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
