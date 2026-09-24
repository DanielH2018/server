"""Skip results: what a skipped task leaves in its register, and who never sees one.

A SKIPPED Ansible task still sets the variable it registers to — a result dict carrying
`skipped: true` and no `stdout`/`stderr`/`rc` key. Two guards read this module, because both
are about a task meeting that dict where it expected a real result:
`deploy/test_retried_commands_survive_check_mode.py` (a retried command evaluates its
until-condition against the skip result, burns every retry and fails the play) and
`deploy/test_conditional_register_consumers.py` (a later task dereferences the skip result
and errors on the missing key).

Two halves. `SKIPPED_IN_CHECK_MODE`, `skips_in_check_mode`, `excludes_check_mode`,
`importer_guards` and `walk_with_inherited_when` answer "does a `--check` run reach this task at
all". `SKIP_MISSING`, `unguarded_deref` and `lazily_guarded` answer "does this expression survive
meeting a skip result". `POST_MODULE_KEYS` names the third question, "does Ansible evaluate this
expression at all on the run that produced the skip result". #2315.

Its own module rather than a section of `_helpers`: that file is at its length cap, and this
is a subject a reader looks up by name rather than a path or a YAML walk.
"""

import re
from pathlib import Path
from typing import Iterator

import yaml

from _helpers import load_yaml, walk_tasks

_NESTING_KEYS = ("block", "rescue", "always")

# The modules check mode cannot simulate, so it skips them outright. `uri`, `stat`, `slurp`
# and the file modules are deliberately absent: they implement check mode and return a real
# result, so both classes above already work under `--check`.
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

# The two keys Ansible evaluates only on a result a module returned. `TaskExecutor._execute`
# wraps both in `if 'skipped' not in result`, so a task check mode SKIPS never reaches either
# — while its module args and its `when:` are templated before the skip and are read on every
# run. `POST_MODULE_KEYS` is therefore text to drop when the CONSUMER is itself skipped under
# `--check`, and text to keep in every other case (#2375).
#
# `until` is NOT one of them, though #2375 asked for it. Its retry loop sits outside that
# guard and evaluates against the skip result, which is the premise
# `test_retried_commands_survive_check_mode.py` rests on: "a `--check` run burns every retry
# on a skip result and fails the play". A read there is reachable and stays judged.
POST_MODULE_KEYS = ("failed_when", "changed_when")


def skips_in_check_mode(task: dict) -> bool:
    """True when a `--check` run skips this task for its module alone.

    `check_mode: false` is the opt-out: the task runs for real under `--check`. A `when:` is
    not consulted here — a task whose `when:` is false is skipped for a different reason, and
    the two callers ask that question separately through `excludes_check_mode`.

    LIMIT: `check_mode: false` on an enclosing `block:` propagates to its children, and
    `walk_with_inherited_when` carries only `when:`, so a child under such a block reads as
    skipped here when it actually runs. No `block:` in the roles tree carries `check_mode:`,
    and `_check_mode_producers` has had the same blind spot since #2315.
    """
    if not SKIPPED_IN_CHECK_MODE & set(task):
        return False
    return task.get("check_mode") is not False


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
# because all.yml folds `ansible_check_mode` into it. That folding is pinned by
# `test_the_k8s_no_mutate_guard_still_covers_check_mode` in the retry guard, so redefining the
# var cannot silently unguard the tasks both callers read as clean.
_CHECK_MODE_FACTS = ("ansible_check_mode", "k8s_no_mutate", "k8s_dry_run")
_NEGATED_FACT = re.compile(
    r"\bnot\s+\(?\s*(?:" + "|".join(_CHECK_MODE_FACTS) + r")\b",
)


def when_clauses(when) -> list[str]:
    """`when:` as a flat list of strings, whatever shape it was written in."""
    if when is None:
        return []
    if isinstance(when, (list, tuple)):
        return [str(clause) for clause in when]
    return [str(when)]


def excludes_check_mode(when) -> bool:
    """True when `when:` cannot be true under `--check`.

    A clause holding `or` is rejected: `not ansible_check_mode or foo` widens the condition
    back out, and reading it as a guard would call an unguarded task clean.
    """
    return any(
        _NEGATED_FACT.search(clause) and " or " not in clause
        for clause in when_clauses(when)
    )


def importer_guards(tasks_dir: Path) -> dict[str, list]:
    """Per task-file basename, the `when:` of every task in the role that includes it.

    Both `import_tasks` and `include_tasks` count. The first propagates its `when` onto each
    imported task at parse time; the second evaluates it before including anything at all.
    Either way a false condition means the file's tasks never run.
    """
    guards: dict[str, list] = {}
    for path in sorted(tasks_dir.glob("*.yml")):
        for task in walk_tasks(load_yaml(path) or []):
            for key in _INCLUDE_KEYS & set(task):
                target = task[key]
                name = target.get("file") if isinstance(target, dict) else target
                if not isinstance(name, str):
                    continue
                guards.setdefault(name.rsplit("/", 1)[-1], []).append(task.get("when"))
    return guards


def walk_with_inherited_when(tasks, inherited=()) -> Iterator[tuple[dict, list]]:
    """Every task, paired with the `when:` of each enclosing `block:`.

    `walk_tasks` yields the wrapper and its children side by side, which loses the
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
        for key in _NESTING_KEYS:
            yield from walk_with_inherited_when(task.get(key), nested)


# Attributes a skip result does not carry. `results` is deliberately absent: a skipped
# looped task DOES carry `results`, which is why looping it is safe and reading `.stdout`
# off its entries is not.
#
# `stdout_lines`, `stderr_lines` and `delta` are listed separately rather than left to the
# bare names: `unguarded_deref` anchors on `\b<reg>.<attr>\b`, and `_` is a word character,
# so there is no boundary inside `stdout_lines` for the `stdout` entry to match (#2351).
SKIP_MISSING = ("stdout", "stdout_lines", "stderr", "stderr_lines", "rc", "delta")

SKIP_FILTERS = (
    "rejectattr('skipped'",
    'rejectattr("skipped"',
    "is not skipped",
    "selectattr",
)


def expressions(task: dict, drop: tuple[str, ...] = ()) -> str:
    """Every templated string in the task itself, so a reference anywhere is seen.

    `drop` names further keys to leave out. Its one caller passes `POST_MODULE_KEYS` for a
    consumer check mode skips: those conditions are evaluated on a result, and a skipped task
    produces none, so a read inside them cannot happen on the run whose skip result is at
    issue (#2375).

    `block:`, `rescue:` and `always:` are dropped. `walk_with_inherited_when` yields each
    child on its own, so a wrapper that kept them would show a child's expressions twice —
    once as the child, once as part of the wrapper — and report the child's read against the
    wrapper's name, pointing the reader at the wrong task. `initial_setup`'s
    "Keep the info-level forwarding out of /var/log/syslog" block was flagged that way for
    a `failed_when:` on its own child, which Ansible never evaluates on a skipped task
    (#2352).
    """
    skip = set(_NESTING_KEYS) | set(drop)
    body = {key: value for key, value in task.items() if key not in skip}
    return yaml.safe_dump(body, default_flow_style=False)


def unguarded_deref(body: str, reg: str, attr: str) -> bool:
    """True when `body` reads `<reg>.<attr>` (directly, or as `item.<attr>` over
    `<reg>.results`) without a `| default(...)` immediately absorbing it.

    Jinja resolves a missing key to Undefined rather than raising, so `x.stdout | default('')`
    is safe on a skip result and `x.stdout | trim` is not. Without this distinction the check
    flags every defensive consumer in the tree -- nut_host's `| default('') | trim` was the
    first false positive it produced.
    """
    guarded = r"\s*\|\s*default\("
    if re.search(rf"\b{re.escape(reg)}\.{attr}\b(?!{guarded})", body):
        return True
    if re.search(rf"\b{re.escape(reg)}\.results\b", body):
        return bool(re.search(rf"\bitem\.{attr}\b(?!{guarded})", body))
    return False


_GATE_VAR = re.compile(r"\b([a-z_][a-z0-9_]*)\b")
# Words that appear in every condition and so identify nothing.
_GATE_NOISE = frozenset(
    {"not", "and", "or", "bool", "default", "true", "false", "is", "in"}
)


def lazily_guarded(
    body: str, reg: str, attr: str, producer_conditions: list[str]
) -> bool:
    """True when the deref sits in the else-branch of a Jinja conditional testing the same
    variable the producer is gated on.

    `{{ false if (x | bool) else (reg.rc != 0) }}` never touches `reg.rc` when `x` is true,
    because Jinja's conditional expression is lazy — so a producer gated on `not (x | bool)`
    and this consumer are reachable under exactly the same condition.

    Without this, the check demands a `| default()` on the else branch. That is not a tidier
    spelling of the same thing: k8s/volume-claim measured it, and defaulting there renders the
    whole expression True and would tar a long-gone source over all 25 live volumes. The
    absence of a default in that branch is load-bearing, so the check has to understand the
    laziness rather than ask for it to be papered over.
    """
    gate_vars = {
        w for cond in producer_conditions for w in _GATE_VAR.findall(cond)
    } - _GATE_NOISE
    if not gate_vars:
        return False
    for match in re.finditer(r"\bif\b(.*?)\belse\b(.*)", body, re.DOTALL):
        test, else_branch = match.group(1), match.group(2)
        if not re.search(rf"\b{re.escape(reg)}\.{attr}\b", else_branch):
            continue
        if gate_vars & (set(_GATE_VAR.findall(test)) - _GATE_NOISE):
            return True
    return False
