#!/usr/bin/env python3
"""Guards the patch that keeps Log2Ram's rsync out of `--sparse`.

Log2Ram ships both of its syncs as::

    rsync -aXv --sparse --inplace --no-whole-file --delete-after ...

`--sparse` makes rsync seek over a run of NUL bytes in the source rather than write
it, and `--inplace` makes the destination the live file — so the previous file's
bytes survive at those offsets. On daniel-pi that clobbered the trailing NULs of the
gzip ISIZE field in 6 of 9 rotated /var/log/apt/history.log.*.gz (#2694): every CRC32
matched byte for byte, and only the last one or two bytes carried stale data, so
`gzip -t` reported "invalid compressed data--length error" over intact content.

`optimize_pi` patches `--sparse` out of both command lines. Two things about that
patch have to survive, and neither is visible from reading the task alone:

1. The patch has to exist at all — deleting it re-opens the corruption on the next
   `apt upgrade` of log2ram, which rewrites /usr/local/bin/log2ram.
2. `ansible.builtin.replace` reports **ok** when its regexp matches nothing, so the
   patch alone cannot tell a successful edit from an upstream reword. The paired
   post-condition task is the half that can go red.

Run: uv run pytest ansible/tests/setup/test_optimize_pi_log2ram_sparse_patch.py
"""

import re

from _helpers import SETUP_ROLES, load_tasks, walk_tasks

LOG2RAM_SCRIPT = "/usr/local/bin/log2ram"
TASKS = SETUP_ROLES / "optimize_pi" / "tasks" / "main.yml"

# Both syncs carry the same flags, so the patch must cover two invocations:
# sync_to_disk (tmpfs -> SD card) and sync_from_disk (SD card -> tmpfs).
EXPECTED_PATCHED_INVOCATIONS = 2


def _tasks():
    return list(walk_tasks(load_tasks(TASKS)))


def _replace_tasks_on_the_log2ram_script():
    return [
        spec
        for task in _tasks()
        if isinstance(spec := task.get("ansible.builtin.replace"), dict)
        and spec.get("path") == LOG2RAM_SCRIPT
    ]


def test_the_role_patches_sparse_out_of_the_log2ram_script():
    patches = _replace_tasks_on_the_log2ram_script()
    assert patches, (
        f"optimize_pi no longer patches {LOG2RAM_SCRIPT}. Log2Ram's own "
        "`rsync -aXv --sparse --inplace` corrupts the trailing NUL bytes of every file it "
        "syncs — it broke 6 of 9 rotated apt history logs on daniel-pi (#2694). An "
        "`apt upgrade` of log2ram rewrites this script, so the patch has to be reapplied by "
        "the role, not done once by hand."
    )
    dropped_sparse = [p for p in patches if "--sparse" in str(p.get("regexp", ""))]
    assert dropped_sparse, (
        f"a task patches {LOG2RAM_SCRIPT} but no longer matches `--sparse`. That flag is the "
        f"whole defect; the patches found were {[p.get('regexp') for p in patches]}."
    )
    for spec in dropped_sparse:
        assert "--sparse" not in str(spec.get("replace", "")), (
            "the patch puts `--sparse` back in its replacement text"
        )


def test_the_patch_is_paired_with_a_post_condition_that_can_go_red():
    # `replace` is silent on a non-match, so without this the role would report ok while
    # leaving the corruption in place after any upstream reword of the rsync line.
    checks = [
        task
        for task in _tasks()
        if LOG2RAM_SCRIPT in str(task.get("ansible.builtin.command", ""))
        and task.get("failed_when")
    ]
    assert checks, (
        f"nothing verifies that the {LOG2RAM_SCRIPT} patch actually applied. "
        "`ansible.builtin.replace` reports ok when its regexp matches nothing, so an "
        "upstream reword of Log2Ram's rsync line would silently restore the NUL-clobbering "
        "sync. Keep a task that reads the patched file back and fails when the expected "
        "invocations are absent."
    )
    # Match the comparison, not the bare digit: the register name contains "log2ram", so a
    # substring search for "2" passes on any threshold at all.
    holds_the_count = re.compile(rf"(?:!=|==)\s*{EXPECTED_PATCHED_INVOCATIONS}\b")
    assert any(
        holds_the_count.search(str(task.get("failed_when"))) for task in checks
    ), (
        "the post-condition does not hold the invocation count to "
        f"{EXPECTED_PATCHED_INVOCATIONS}. Log2Ram carries the same flags in sync_to_disk and "
        "sync_from_disk, and patching only one leaves the other direction corrupting the "
        f"tmpfs copy. Found: {[task.get('failed_when') for task in checks]}"
    )


def test_the_guard_reads_the_real_task_file():
    # A path typo would make both tests above fail for the wrong reason, or pass vacuously
    # if either assertion were ever relaxed to a truthiness check on an empty list.
    assert TASKS.is_file(), f"{TASKS} is missing — check SETUP_ROLES"
    assert len(_tasks()) >= 20, (
        f"only {len(_tasks())} tasks parsed from {TASKS}; the role is much larger, so the "
        "loader is not reading what it thinks it is"
    )
