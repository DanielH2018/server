"""No retried `command` in the role tree fails its own retry loop under `--check`.

WHY THIS EXISTS. Check mode SKIPS `ansible.builtin.command` — Ansible cannot simulate an
arbitrary binary, so it returns `skipped: true` with no `stdout`. A task that also retries
then evaluates its until-condition against that skip result, which never satisfies it, so the
task burns every retry and FAILS the play. The dry run reports a red that says nothing about
the tree, and `.claude/rules/ansible.md` asks for a `--check` before touching production
state — a tag that always fails under `--check` trains an operator to skip it.

Measured 2026-09-22 (#2293): `ansible-playbook ansible/k3s-bringup.yml --tags kubeconfig
--check --diff` failed at `Read the read-only ServiceAccount token` after 12 attempts and a
minute of `delay: 5`, while the real run of the same tag passed with the token read
succeeding first try.

THE SCOPE IS TREE-WIDE, AND WAS NOT WHEN THIS FILE WAS WRITTEN. The #2293 version checked
`setup/k3s/tasks/kubeconfig.yml` alone, because a census that read each task in isolation
reported 19 more offenders and the note here said most of them should not be changed. That
census was wrong, and #2305 is the correction: of the 20 retried command tasks outside
kubeconfig, 12 already exclude check mode at a level a per-task read cannot see.

  - Nine carry `when: not (k8s_no_mutate | bool)`, and
    `ansible/inventory/group_vars/all.yml:k8s_no_mutate` is defined as `ansible_check_mode or
    (k8s_dry_run | bool)`. Skipping under a dry run is the same skip.
  - Two are in `ansible/roles/k8s/crowdsec/tasks/verify.yml`, whose importer in
    `main.yml` gates the whole file on `not ansible_check_mode`. A `when` on an
    `import_tasks` propagates to every task in the imported file.
  - One, `Sync the live Grafana admin password with SOPS`, carries `not ansible_check_mode`
    inline alongside its own condition.

`_excludes_check_mode`, `_importer_guards` and `walk_with_inherited_when` below teach the
predicate to see all three, which is what makes a tree-wide census honest rather than a
demand for twelve wrong changes. The remaining eight are fixed in the same commit as this
widening.

TWO KINDS OF FIX, AND ONLY A HUMAN READING THE TASK SORTS THEM. `changed_when: false` does
not: 19 of the 21 carry it, the waits included.

  - A **read of state that exists independently of this play** takes `check_mode: false` +
    `changed_when: false`. It runs for real under `--check` and writes nothing. Forced when a
    LATER task reads the register — a skip result cascades into that consumer and errors
    there instead.
  - A **wait for state an earlier task in the same play creates**, or a task that MUTATES,
    takes `when: not ansible_check_mode`. Running it for real would either burn its retries
    on something the dry run deliberately did not create, or write to production from a
    `--check` — the one thing `--check` promises it will not do.

Run: uv run pytest ansible/tests/deploy/test_retried_commands_survive_check_mode.py
"""

import re

import pytest
from lib import yaml_fast

from _helpers import ALL_VARS, ROLES, walk_tasks

# The modules check mode cannot simulate, so it skips them outright. `uri`, `stat`, `slurp`
# and the file modules are deliberately absent: they implement check mode and return a real
# result, so a retry loop over one of them already works under `--check`.
SKIPPED_IN_CHECK_MODE = frozenset(
    {
        "command",
        "shell",
        "raw",
        "script",
        "ansible.builtin.command",
        "ansible.builtin.shell",
        "ansible.builtin.raw",
        "ansible.builtin.script",
    }
)

_INCLUDE_KEYS = frozenset(
    {
        "import_tasks",
        "include_tasks",
        "ansible.builtin.import_tasks",
        "ansible.builtin.include_tasks",
    }
)

# The three facts a `when:` can name to mean "not under a dry run". `k8s_dry_run` is listed
# for completeness; `k8s_no_mutate` is the form roles actually use, and it is sound here only
# because all.yml folds `ansible_check_mode` into it — asserted below, so redefining that var
# cannot silently unguard nine tasks while this file stays green.
_CHECK_MODE_FACTS = ("ansible_check_mode", "k8s_no_mutate", "k8s_dry_run")
_NEGATED_FACT = re.compile(
    r"\bnot\s+\(?\s*(?:" + "|".join(_CHECK_MODE_FACTS) + r")\b",
)

# Task files whose roles are retired. They deploy nothing, so a red here would be a demand to
# edit history.
_ARCHIVED = "/archive/"

# The retried tasks the census must still find, so a rename or a move cannot empty it and
# leave every assertion below passing over nothing. One member per fix shape, named rather
# than counted so the failure says which one went missing.
KNOWN_RETRIED_COMMAND_TASKS = frozenset(
    {
        # opted out with `check_mode: false` — reads of pre-existing state
        "Read the read-only ServiceAccount token",
        "Read the secrets-encryption status",
        "Read the guest's ssh host key",
        # skipped with `when: not ansible_check_mode` — waits and mutations
        "Wait for the re-encryption to finish",
        "Wait for the PVC to reach Bound",
        "Verify cluster DNS resolves through the configured upstream",
        # skipped by an ancestor guard the per-task read cannot see
        "Probe the VIP from the banned Pi — expect 403",
    }
)


# The one fix in #2305 that carries no `retries`/`until`, so the census above cannot see it.
# `Point cluster DNS at Pi-hole first` compares this register's `stdout` in its `when:`, and
# under `--check` that conditional errors before the retried probe further down coredns.yml is
# ever reached — the tag fails whatever the retried task carries. Pinned by name here so a
# revert reopens the tag loudly instead of silently. The general class is #2315.
NON_RETRIED_READS_THAT_MUST_OPT_OUT = (
    (ROLES / "setup" / "k3s" / "tasks" / "coredns.yml", "Read the live Corefile"),
)


def _when_clauses(when) -> list[str]:
    """`when:` as a flat list of strings, whatever shape it was written in."""
    if when is None:
        return []
    if isinstance(when, (list, tuple)):
        return [str(clause) for clause in when]
    return [str(when)]


def _excludes_check_mode(when) -> bool:
    """True when `when:` cannot be true under `--check`.

    A clause holding `or` is rejected: `not ansible_check_mode or foo` widens the condition
    back out, and reading it as a guard would call an unguarded task clean.
    """
    return any(
        _NEGATED_FACT.search(clause) and " or " not in clause
        for clause in _when_clauses(when)
    )


def check_mode_retry_problem(task: dict, inherited_when=()) -> str | None:
    """Why `task` fails its own retry loop under `--check`, or None when it is fine.

    A reason rather than a bool, so the failure names the defect instead of the file.
    `inherited_when` carries the `when:` of any importer that pulls in the task's file.
    """
    if not SKIPPED_IN_CHECK_MODE & set(task):
        return None
    if "retries" not in task and "until" not in task:
        return None
    # FIRST, because a task check mode never reaches is clean whatever else it carries. The
    # `changed_when` branch below demands `false`, and a guarded task is free to compute a
    # real changed status — `Sync the live Grafana admin password with SOPS` does exactly
    # that and would be flagged for the wrong reason if this branch ran second.
    if _excludes_check_mode(task.get("when")):
        return None
    if any(_excludes_check_mode(when) for when in inherited_when):
        return None
    if task.get("check_mode") is not False:
        return (
            "retries under a module check mode skips, without `check_mode: false` and "
            "without a `when:` that excludes check mode — a `--check` run burns every "
            "retry on a skip result and fails the play"
        )
    if task.get("changed_when") is not False:
        return (
            "`check_mode: false` without `changed_when: false` — the task opts out of the "
            "dry run without declaring itself a read, so `--check` would change state"
        )
    return None


def _task_files() -> list:
    return [
        path
        for path in sorted(ROLES.glob("*/*/tasks/*.yml"))
        if _ARCHIVED not in path.as_posix()
    ]


def _importer_guards(tasks_dir) -> dict[str, list]:
    """Per task-file basename, the `when:` of every task in the role that includes it.

    Both `import_tasks` and `include_tasks` count. The first propagates its `when` onto each
    imported task at parse time; the second evaluates it before including anything at all.
    Either way a false condition means the file's tasks never run.
    """
    guards: dict[str, list] = {}
    for path in sorted(tasks_dir.glob("*.yml")):
        for task in walk_tasks(yaml_fast.safe_load(path.read_text()) or []):
            for key in _INCLUDE_KEYS & set(task):
                target = task[key]
                name = target.get("file") if isinstance(target, dict) else target
                if not isinstance(name, str):
                    continue
                guards.setdefault(name.rsplit("/", 1)[-1], []).append(task.get("when"))
    return guards


def walk_with_inherited_when(tasks, inherited=()):
    """Every task, paired with the `when:` of each enclosing `block:`.

    `_helpers.walk_tasks` yields the wrapper and its children side by side, which loses the
    relationship between them — and a `when:` on a block is exactly a condition the children
    are governed by without carrying it. Ansible applies it to `rescue:` and `always:` too,
    so all three nest the same way here. `setup/k3s/tasks/storage_smoke.yml` is why: one
    guard on its block covers the create, the wait, the assert and both cleanups.
    """
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task, list(inherited)
        nested = [*inherited, task.get("when")]
        for key in ("block", "rescue", "always"):
            yield from walk_with_inherited_when(task.get(key), nested)


def _retried_command_tasks() -> list[tuple]:
    """Every retried `command` in the deployed role tree, with its inherited `when:`s."""
    found = []
    for path in _task_files():
        loaded = yaml_fast.safe_load(path.read_text())
        if not isinstance(loaded, list):
            continue
        from_importer = _importer_guards(path.parent).get(path.name, [])
        for task, from_blocks in walk_with_inherited_when(loaded):
            if SKIPPED_IN_CHECK_MODE & set(task) and (
                "retries" in task or "until" in task
            ):
                found.append((path, task, [*from_importer, *from_blocks]))
    return found


def test_the_k8s_no_mutate_guard_still_covers_check_mode():
    """Nine tasks are called clean because they guard on `k8s_no_mutate`.

    That is only sound while all.yml folds `ansible_check_mode` into the expression. Without
    this, redefining the var to mean `k8s_dry_run` alone would unguard all nine and every
    assertion here would stay green.
    """
    expression = str(yaml_fast.safe_load(ALL_VARS.read_text())["k8s_no_mutate"])
    assert "ansible_check_mode" in expression, (
        f"`k8s_no_mutate` is now {expression!r} and no longer implies check mode, so "
        "`when: not (k8s_no_mutate | bool)` has stopped being a check-mode guard — this "
        "file reads nine tasks as clean on the strength of it"
    )


def test_the_census_still_finds_the_tasks_it_knows_about():
    names = {str(task.get("name", "")) for _, task, _ in _retried_command_tasks()}
    missing = KNOWN_RETRIED_COMMAND_TASKS - names
    assert not missing, (
        f"the role tree no longer has a retried command task named {sorted(missing)} — it "
        "was renamed, moved or removed; update KNOWN_RETRIED_COMMAND_TASKS in the same "
        "commit, or this file checks a shrinking set and passes"
    )


@pytest.mark.parametrize(("path", "name"), NON_RETRIED_READS_THAT_MUST_OPT_OUT)
def test_a_read_a_later_conditional_depends_on_still_opts_out_of_check_mode(path, name):
    matches = [
        task
        for task, _ in walk_with_inherited_when(yaml_fast.safe_load(path.read_text()))
        if task.get("name") == name
    ]
    assert len(matches) == 1, (
        f"{path.relative_to(ROLES)} no longer has exactly one task named {name!r} — it was "
        "renamed or removed, and this assertion would otherwise check nothing"
    )
    # Routed through the same predicate the retried census uses, by lending the task the
    # `retries` it does not have. The consequence is identical — check mode skips `command`
    # either way — and reusing the predicate means this pin inherits the branch coverage
    # below rather than adding a second rule that only anyone ever sees pass.
    why = check_mode_retry_problem({**matches[0], "retries": 1})
    assert why is None, (
        f"{path.relative_to(ROLES)} :: {name} — {why}. A later task reads this register in "
        "its `when:`, so under `--check` that conditional errors on the skip result before "
        "any retried task in the file is reached"
    )


def test_every_retried_command_in_the_role_tree_runs_under_check_mode():
    offenders = [
        f"{path.relative_to(ROLES)} :: {task.get('name', '<unnamed>')} — {why}"
        for path, task, inherited in _retried_command_tasks()
        if (why := check_mode_retry_problem(task, inherited))
    ]
    assert not offenders, "a `--check` run fails on:\n" + "\n".join(offenders)


# ── the rule itself, every branch in both directions ──────────────────────────────────────
# A rule that fires on everything and one that fires on nothing are indistinguishable from
# the passing side alone, so each branch has an accepting and a rejecting input.


def _task(**kw):
    return {"name": "t", "ansible.builtin.command": {"cmd": "true"}, **kw}


@pytest.mark.parametrize(
    "task",
    [
        _task(retries=12, until="r.stdout | length > 0"),
        _task(until="r.stdout | length > 0"),
        _task(retries=3, check_mode=True),
    ],
)
def test_a_retried_command_without_the_opt_out_is_flagged(task):
    assert check_mode_retry_problem(task) is not None


def test_a_read_that_opts_out_of_check_mode_is_clean():
    task = _task(retries=12, until="r.stdout", check_mode=False, changed_when=False)
    assert check_mode_retry_problem(task) is None


def test_a_state_changing_task_may_not_opt_out_of_check_mode():
    task = _task(retries=12, until="r.rc == 0", check_mode=False)
    assert "changed_when" in (check_mode_retry_problem(task) or "")


@pytest.mark.parametrize(
    "when",
    [
        "not ansible_check_mode",
        "not (k8s_no_mutate | bool)",
        "not k8s_no_mutate",
        ["k3s_secrets_encryption | bool", "not ansible_check_mode"],
    ],
)
def test_a_task_a_check_run_never_reaches_is_clean(when):
    assert (
        check_mode_retry_problem(_task(retries=12, until="r.stdout", when=when)) is None
    )


@pytest.mark.parametrize(
    "when",
    [
        # Polarity: this one runs ONLY in check mode, so the skip it hits is the whole point
        # of flagging it.
        "ansible_check_mode",
        "k8s_no_mutate | bool",
        # `or` widens the condition back out, so the negation no longer bounds it.
        "not ansible_check_mode or k3s_force",
        ["k3s_secrets_encryption | bool"],
    ],
)
def test_a_when_that_does_not_exclude_check_mode_is_still_flagged(when):
    assert (
        check_mode_retry_problem(_task(retries=12, until="r.stdout", when=when))
        is not None
    )


def test_a_guard_on_the_importer_clears_the_tasks_it_imports():
    task = _task(retries=8, until='r.stdout == "403"', changed_when=False)
    assert check_mode_retry_problem(task) is not None
    assert check_mode_retry_problem(task, ["not ansible_check_mode"]) is None


def test_an_importer_guard_that_does_not_exclude_check_mode_clears_nothing():
    task = _task(retries=8, until='r.stdout == "403"', changed_when=False)
    assert check_mode_retry_problem(task, ["k8s_public_route | bool"]) is not None


def test_a_block_hands_its_when_down_to_the_tasks_inside_it():
    wait = _task(retries=10, until="r.stdout == 'Bound'", changed_when=False)
    guarded = [{"name": "b", "when": "not ansible_check_mode", "block": [wait]}]
    inherited = {id(task): when for task, when in walk_with_inherited_when(guarded)}[
        id(wait)
    ]
    assert check_mode_retry_problem(wait, inherited) is None


def test_a_block_with_no_check_mode_guard_hands_down_nothing_that_clears():
    wait = _task(retries=10, until="r.stdout == 'Bound'", changed_when=False)
    unguarded = [{"name": "b", "when": "k3s_storage_smoke | bool", "block": [wait]}]
    inherited = {id(task): when for task, when in walk_with_inherited_when(unguarded)}[
        id(wait)
    ]
    assert check_mode_retry_problem(wait, inherited) is not None


def test_a_guarded_task_may_compute_a_real_changed_status():
    """The `when:` branch runs before the `changed_when` one, and this is why.

    `Sync the live Grafana admin password with SOPS` guards itself on check mode and keys
    `changed_when` on its own rc. Checking `changed_when` first would flag it for a defect it
    does not have.
    """
    task = _task(
        retries=6,
        until="r.rc == 0",
        when="manifests_secret_render is changed and not ansible_check_mode",
        changed_when="(r.rc | default(1)) == 0",
    )
    assert check_mode_retry_problem(task) is None


@pytest.mark.parametrize(
    "task",
    [
        {
            "name": "t",
            "ansible.builtin.uri": {"url": "x"},
            "retries": 5,
            "until": "r.status == 200",
        },
        _task(),
    ],
)
def test_a_task_the_rule_does_not_govern_is_clean(task):
    assert check_mode_retry_problem(task) is None
