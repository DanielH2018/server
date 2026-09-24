"""The skipped-register rule itself: who produces a skip result, and who trips over one.

Split from `test_conditional_register_consumers.py` on 2026-09-24, when the #2351/#2352/#2353
widenings took that module past its length cap. That module's docstring is the contract — the
incidents, the two rules and why a static check catches this class at all. This file is the
mechanics.
"""

import re
from pathlib import Path
from typing import NamedTuple

import yaml
from lib import yaml_fast

from _check_mode import (
    SKIP_FILTERS,
    SKIP_MISSING,
    SKIPPED_IN_CHECK_MODE,
    excludes_check_mode,
    expressions,
    importer_guards,
    lazily_guarded,
    unguarded_deref,
    walk_with_inherited_when,
)
from _helpers import REPO as _REPO_ROOT
from _helpers import ROLES as _ROLES

# The synthetic condition a check-mode producer is gated on. It carries no `when:`, but the
# effect is exactly `when: not ansible_check_mode` — so the lazy-guard exemption below, which
# keys on the variables the producer's conditions name, reads it the same way.
_CHECK_MODE_CONDITION = "not ansible_check_mode"


class Problem(NamedTuple):
    """One consumer that errors on a skip result, and why."""

    task: str
    message: str


def _task_files() -> list[Path]:
    return sorted(p for p in _ROLES.rglob("tasks/*.yml") if "archive" not in p.parts)


def _when_text(task: dict) -> str:
    when = task.get("when")
    if when is None:
        return ""
    if isinstance(when, list):
        return " and ".join(str(w) for w in when)
    return str(when)


def _conditions(task: dict) -> list[str]:
    when = task.get("when")
    if when is None:
        return []
    if isinstance(when, list):
        return [str(w).strip() for w in when]
    return [str(when).strip()]


def _producers(tasks: list[dict]) -> dict[str, list[str]]:
    """register name -> the producer conditions that can yield per-item SKIP results.

    A condition that references the producer's own `loop:` source is excluded: when it is
    false the loop is empty, so the register's `results` is an empty list and every consumer
    iterates zero times. That is safe, and it is how k8s/rollout-drain is written. Only a
    condition orthogonal to the loop (a `changed` check, `not ansible_check_mode`) leaves
    skip entries behind for a consumer to trip over.
    """
    found = {}
    for task in tasks:
        reg = task.get("register")
        if not reg:
            continue
        loop_src = str(task.get("loop", ""))
        risky = [
            cond
            for cond in _conditions(task)
            if not any(
                name and name in loop_src
                for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", cond)
            )
        ]
        if risky:
            found[reg] = risky
    return found


def _check_mode_producers(pairs) -> dict[str, str]:
    """register -> producer task name, for producers a `--check` run skips outright.

    `pairs` is `walk_with_inherited_when(tasks)`. A producer that also carries a `when:` is
    reported here as well as by the when-based rule, so a consumer is clean only when BOTH
    rules accept it. Skipping those registers was #2353: the when-based rule accepts a
    consumer that repeats the producer's condition, which is enough on a real run and not
    under `--check`, where the producer is skipped whatever its `when:` says. `hypervisor`'s
    "Stop a running guest whose live interface carries no egress fence" repeats the
    condition, reads `hypervisor_staging_vm_live_xml.stdout`, and errored under `--check`
    against a running guest while the guard called it clean.

    `check_mode: false` is the opt-out: the task runs for real under `--check` and its
    register carries a real result. A `when:` that excludes check mode — on the task or on an
    enclosing block — means the producer is skipped, but so is every consumer beside it.
    """
    found = {}
    for task, inherited in pairs:
        reg = task.get("register")
        if not reg:
            continue
        if not SKIPPED_IN_CHECK_MODE & set(task):
            continue
        if task.get("check_mode") is False:
            continue
        if excludes_check_mode(task.get("when")):
            continue
        if any(excludes_check_mode(when) for when in inherited):
            continue
        found[reg] = str(task.get("name", "<unnamed>"))
    return found


def _offenders(path: Path) -> list[Problem]:
    try:
        loaded = yaml_fast.safe_load(path.read_text())
    except yaml.YAMLError:
        return []  # the manifest/lint hooks own YAML validity; this check owns semantics
    if not isinstance(loaded, list):
        return []
    pairs = list(walk_with_inherited_when(loaded))
    tasks = [task for task, _ in pairs]
    conditional = _producers(tasks)
    # A file imported under a guard no `--check` run satisfies has no reachable task in it,
    # producer or consumer. The when-based rule above does not consult this: an importer
    # saying "not under check mode" proves nothing about some other producer's `when:`.
    importer = importer_guards(path.parent).get(path.name, [])
    skipped = (
        {}
        if any(excludes_check_mode(when) for when in importer)
        else _check_mode_producers(pairs)
    )
    if not conditional and not skipped:
        return []

    problems = _check_mode_offenders(path, pairs, skipped)
    for task in tasks:
        if task.get("register") in conditional:
            continue  # the producer itself
        body = expressions(task)
        consumer_when = _when_text(task)
        loop = str(task.get("loop", ""))
        for reg, producer_conditions in conditional.items():
            deref = [attr for attr in SKIP_MISSING if unguarded_deref(body, reg, attr)]
            if not deref:
                continue
            if all(cond in consumer_when for cond in producer_conditions):
                continue  # consumer repeats every risky guard the producer carries
            if any(f in loop for f in SKIP_FILTERS):
                continue  # consumer filters the skip results out
            if any(
                lazily_guarded(body, reg, attr, producer_conditions) for attr in deref
            ):
                continue  # deref sits behind a lazy `if <same gate> else` and is unreachable
            producer_when = " and ".join(producer_conditions)
            name = str(task.get("name", "<unnamed>"))
            problems.append(
                Problem(
                    name,
                    f"{_where(path)}: task {name!r} reads "
                    f"{reg}.{deref[0]} but {reg}'s producer is gated on "
                    f"`{producer_when.strip()}`. A skipped task still sets its register, and "
                    f"the skip result has no `{deref[0]}`. Either repeat the producer's "
                    "condition, or filter the loop with "
                    "`| rejectattr('skipped', 'defined') | list`.",
                )
            )
    return problems


def _where(path: Path) -> Path:
    try:
        return path.relative_to(_REPO_ROOT)
    except ValueError:
        return path  # a tmp_path fixture in this file's own tests


def _check_mode_offenders(path: Path, pairs, skipped) -> list[Problem]:
    """Consumers that dereference a register check mode turns into a skip result."""
    problems = []
    for task, inherited in pairs:
        if task.get("register") in skipped:
            continue  # the producer itself
        if excludes_check_mode(task.get("when")):
            continue
        if any(excludes_check_mode(when) for when in inherited):
            continue
        body = expressions(task)
        loop = str(task.get("loop", ""))
        if any(skip_filter in loop for skip_filter in SKIP_FILTERS):
            continue
        for reg, producer in skipped.items():
            deref = [attr for attr in SKIP_MISSING if unguarded_deref(body, reg, attr)]
            if not deref:
                continue
            if any(
                lazily_guarded(body, reg, attr, [_CHECK_MODE_CONDITION])
                for attr in deref
            ):
                continue
            name = str(task.get("name", "<unnamed>"))
            problems.append(
                Problem(
                    name,
                    f"{_where(path)}: task {name!r} reads {reg}.{deref[0]}, but {reg}'s "
                    f"producer {producer!r} is a module check mode SKIPS whatever its "
                    f"`when:` says. Under `--check` the register is a skip result with no "
                    f"`{deref[0]}`, so this errors with \"object of type 'dict' has no "
                    f"attribute '{deref[0]}'\". Give the producer `check_mode: false` + "
                    "`changed_when: false` if it reads state that exists independently of "
                    "this play, or give this consumer `when: not ansible_check_mode`.",
                )
            )
    return problems
