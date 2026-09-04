# ansible/roles/setup/gitops_deploy/files/deploy_failtext.py
"""Bounding a failed run's output: what a deploy failure's alert quotes, and how much.

A failed `ansible-playbook` run can print megabytes into the journal, so both halves of its
output are cut before they reach a Discord message. `failure_detail` is the stdout half and is
not a plain tail — profile_tasks prints its timing table after the PLAY RECAP, so it lifts the
failing task's own lines out first and spends what is left of the budget on the tail.

This is a leaf: it imports the standard library and nothing else. Callers reach these names
qualified — `deploy_failtext.failure_detail(...)`. `deploy_io` re-exports them for the suite,
which reads them through the module it has always read.

Stdlib only: the unit runs under `uv run --no-project` and the host is still on Python 3.12.
"""

import re


# ansible-playbook writes the failing TASK header, the `fatal:` line carrying its msg and the
# PLAY RECAP to STDOUT. stderr carries only warnings and deprecation notices, so an error string
# built from stderr alone says nothing about what broke — the 2026-09-02 broad apply that failed
# on `2d25ced3` left a deprecation warning's origin as the only surviving detail, and the broad
# arm is forward-only, so the operator had nothing to fix forward from. Both halves are bounded
# because a failed run can print megabytes into the journal.
#
# The stdout half is NOT a plain tail. The profile_tasks callback prints its timing table after
# the PLAY RECAP, and on a 1950-task deploy.yml that table plus the recap is more than the whole
# budget, so a positional tail held `ok=1950 failed=1` and nothing naming the task (issue #907,
# the 21:26 failure on `55c33965`). _failure_detail lifts the failing task's own lines out first
# and spends what is left of the budget on the tail.
RUN_ERROR_STDOUT_CHARS = 4000
RUN_ERROR_STDERR_TAIL = 4000

# The default stdout callback opens every section with one of these, and profile_tasks adds
# TASKS RECAP. A failure inside a section is a `fatal: [host]: ...` line (FAILED! or
# UNREACHABLE!) or a per-item `failed: [host] (item=...)` line; ignore_errors prints
# `...ignoring` on the line after each one.
_SECTION_HEADER = re.compile(
    r"^(TASK|RUNNING HANDLER|PLAY|PLAY RECAP|NO MORE HOSTS LEFT|TASKS RECAP)\b"
)
_TASK_HEADER = re.compile(r"^(TASK|RUNNING HANDLER) \[")
_FAILURE_LINE = re.compile(r"^(fatal|failed): \[")
TRUNCATED = "[...truncated...]"


def tail(text: str, limit: int) -> str:
    """Return at most the last ``limit`` characters of ``text``, cut at a line boundary.

    The right slice for stderr and for stdout with no `fatal:` line in it: what ansible prints
    last is the diagnostic part. It is the wrong slice once profile_tasks pads the end, which
    is why failure_detail exists.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    _, newline, rest = cut.partition("\n")
    return f"{TRUNCATED}\n" + (rest if newline else cut)


def head(text: str, limit: int) -> str:
    """Return at most the first ``limit`` characters of ``text``, cut at a line boundary."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    kept, newline, _ = cut.rpartition("\n")
    return (kept if newline else cut) + f"\n{TRUNCATED}"


def failing_task(text: str) -> tuple[str, str] | None:
    """Find the last task in ansible stdout whose failure was not ignored.

    Returns:
        The task's lines and everything printed after them, or None when no section of
        ``text`` carries an un-ignored `fatal:`/`failed:` line. The task's lines are its
        header plus its output from the first failure line to the end of the section: a loop
        task prints one line per ok item before the one that failed, and those are padding.
        A failure a `rescue:` block caught is still a candidate — nothing marks it as
        rescued at the point it prints — but a later un-ignored failure wins over it.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if _SECTION_HEADER.match(line)]
    for n in range(len(starts) - 1, -1, -1):
        start = starts[n]
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        if not _TASK_HEADER.match(lines[start]):
            continue
        section = lines[start:end]
        failed_at = next(
            (i for i, line in enumerate(section) if _FAILURE_LINE.match(line)), None
        )
        if failed_at is None or any(line.startswith("...ignoring") for line in section):
            continue
        task = "\n".join([section[0], *section[failed_at:]]).strip()
        return task, "\n".join(lines[end:])
    return None


def failure_detail(stdout: str, limit: int) -> str:
    """Bound ansible's stdout to ``limit`` characters, keeping the failing task's lines.

    The failing task comes first and gets the budget first; the tail of what follows it gets
    the remainder, minus profile_tasks' timing table, which is the padding that evicted the
    task in the first place and diagnoses nothing. Stdout with no failing task in it is a
    plain tail, as before.
    """
    found = failing_task(stdout)
    if found is None:
        return tail(stdout, limit)
    task, rest = found
    rest, _, _ = rest.partition("\nTASKS RECAP")
    task = head(task, limit)
    rest = tail(rest, limit - len(task))
    return f"{task}\n{rest}" if rest else task
