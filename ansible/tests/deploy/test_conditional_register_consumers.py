"""Tree-wide guard for the skipped-register class.

WHY THIS EXISTS. A SKIPPED Ansible task still sets the variable it registers to. The value is
a result dict carrying `skipped: true` and no `stdout`/`rc` key at all. So a consumer that
dereferences `<reg>.stdout`, or loops `<reg>.results` and reads `item.stdout`, blows up with
"object of type 'dict' has no attribute 'stdout'" on exactly the runs where the producer's
`when:` was false — which are the runs nobody tests.

The `when:` half of the rule has bitten twice:

  * 2026-08-21, k8s/volume-snapshot: a retake wait registered over the first wait's genuine
    result and failed the deploy over a healthy snapshot. `test_volume_snapshot_register.py`
    is the behavioural anchor from that one, and it covers that role only.
  * 2026-08-22, k8s/claude-otel: the restart-count snapshot is gated on the manifests
    changing, but its assert and its stabilise-gate hand-off were not. A dashboards-only
    deploy — manifests unchanged — failed the play after the dashboards had already applied.

The behavioural test in `test_volume_snapshot_register.py` says a rendered-expression test
cannot catch this class, and it is right about the general case: the bug is in *when* Ansible
assigns a register. But the specific structural shape above IS statically visible, and it is
the shape both incidents took. Catching it costs one parse; catching it behaviourally costs a
stubbed end-to-end run per role.

The rule: if a task carries `when:` and a `register:`, every task that dereferences that
register must either carry the producer's condition too, or filter the skip results out of
its loop (`rejectattr('skipped', 'defined')`).

A PRODUCER NEEDS NO `when:` TO BE CONDITIONAL. Check mode skips `ansible.builtin.command` and
its siblings whatever their `when:` says, so an UNGUARDED command that registers is a skip
result on every `--check` run — the same dict with no `stdout`, reached by a different route.
`setup/k3s/tasks/coredns.yml` was the third incident (2026-09-22, #2305): `Read the live
Corefile` carries no `when:`, and `Point cluster DNS at Pi-hole first` compares
`k3s_coredns_live.stdout` in its own `when:`. Under `--check` that conditional errored before
the play reached anything else in the file. The fix was one `check_mode: false`; the class was
unguarded until #2315 widened `_producers` with `_check_mode_producers` below.

A consumer of a check-mode producer is clean when a `--check` run never reaches it — its own
`when:`, an enclosing `block:`, or the `when:` on the task that imports its file. All three
are `_check_mode.excludes_check_mode` over `_check_mode.walk_with_inherited_when` and
`_check_mode.importer_guards`, shared with
`deploy/test_retried_commands_survive_check_mode.py`, which asks the same questions of
the same tasks for the retry-loop version of this bug.

`check_mode_skip_consumers.txt` beside this file holds the 32 instances that predate the
widening, and its header carries the rules for editing it. Draining that list is #2323.
"""

import re
from pathlib import Path
from typing import NamedTuple

import pytest
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


def _check_mode_producers(pairs, when_based) -> dict[str, str]:
    """register -> producer task name, for producers a `--check` run skips outright.

    `pairs` is `walk_with_inherited_when(tasks)`. A producer already in `when_based` is
    reported by the older rule and is not repeated here.

    `check_mode: false` is the opt-out: the task runs for real under `--check` and its
    register carries a real result. A `when:` that excludes check mode — on the task or on an
    enclosing block — means the producer is skipped, but so is every consumer beside it.
    """
    found = {}
    for task, inherited in pairs:
        reg = task.get("register")
        if not reg or reg in when_based:
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
        else _check_mode_producers(pairs, conditional)
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


_ALLOWLIST = Path(__file__).parent / "check_mode_skip_consumers.txt"


def _allowlisted() -> frozenset[tuple[str, str]]:
    """`(path under ansible/roles/, task name)` for every entry in the allowlist file.

    Kept as data rather than a literal: the list is long, it only ever shrinks, and a drain
    PR then reads as deleted lines rather than as a diff inside a test module. The file's own
    header carries the rules for editing it.
    """
    entries = set()
    for line in _ALLOWLIST.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        path, _, task = line.partition("\t")
        entries.add((path, task))
    return frozenset(entries)


_COREDNS = _ROLES / "setup" / "k3s" / "tasks" / "coredns.yml"


def _live_offenders(path: Path) -> list[Problem]:
    """`_offenders`, minus the entries _allowlisted() already owns."""
    rel = path.relative_to(_ROLES).as_posix()
    allowlisted = _allowlisted()
    return [
        problem
        for problem in _offenders(path)
        if (rel, problem.task) not in allowlisted
    ]


@pytest.mark.parametrize(
    "path", _task_files(), ids=lambda p: str(p.relative_to(_ROLES))
)
def test_no_unguarded_consumer_of_a_conditional_register(path: Path) -> None:
    problems = _live_offenders(path)
    assert not problems, "\n".join(problem.message for problem in problems)


def test_the_allowlist_names_only_live_offenders() -> None:
    """Every allowlisted pair is still an offender, so the list can only shrink.

    This is also the census's non-vacuity assertion: a rule that stopped matching, or a walk
    that started returning nothing, empties `found` and fails here by name. Without it the
    per-file test above would pass over zero offenders and say nothing.
    """
    found = {
        (path.relative_to(_ROLES).as_posix(), problem.task)
        for path in _task_files()
        for problem in _offenders(path)
    }
    stale = sorted(_allowlisted() - found)
    assert not stale, (
        f"check_mode_skip_consumers.txt still lists {stale}, which the rule no longer "
        "flags — the task was fixed, renamed or removed. Drop the entry in the same commit."
    )


def test_the_widened_rule_would_have_caught_the_coredns_read(tmp_path: Path) -> None:
    """#2315's anchor: coredns.yml with its `check_mode: false` taken back off.

    `Read the live Corefile` carries no `when:`, so the older rule reads it as an
    unconditional producer. Check mode skips `command` regardless, and `Point cluster DNS at
    Pi-hole first` compares `k3s_coredns_live.stdout` in its own `when:` — which is how the
    `coredns` tag failed under `--check` on 2026-09-22 (#2305).
    """
    tasks = yaml_fast.safe_load(_COREDNS.read_text())
    reads = [t for t in tasks if t.get("name") == "Read the live Corefile"]
    assert len(reads) == 1, (
        "setup/k3s/tasks/coredns.yml no longer has exactly one task named 'Read the live "
        "Corefile' — it was renamed or removed, and this anchor would check nothing"
    )
    assert reads[0].pop("check_mode", None) is False, (
        "'Read the live Corefile' no longer carries `check_mode: false`. The #2305 fix was "
        "reverted and the `coredns` tag fails under `--check` again"
    )
    reverted = tmp_path / "tasks"
    reverted.mkdir()
    (reverted / "coredns.yml").write_text(yaml.safe_dump(tasks))

    flagged = {
        problem.task: problem.message
        for problem in _offenders(reverted / "coredns.yml")
        if "k3s_coredns_live" in problem.message
    }
    assert "Point cluster DNS at Pi-hole first" in flagged, (
        "the widened rule does not name the consumer that #2305 fixed by hand, so it would "
        f"not have caught the class it was written for — it flagged {sorted(flagged)}"
    )
    # The message names the PRODUCER as well as the consumer: the task an operator has to
    # edit is the read, and the failure is reported on the task that trips over it.
    assert "Read the live Corefile" in flagged["Point cluster DNS at Pi-hole first"], (
        "the failure names the consumer but not the read whose opt-out is the fix"
    )


def test_the_live_coredns_read_is_clean() -> None:
    """The accepting half: with `check_mode: false` in place, the file reports nothing."""
    assert _offenders(_COREDNS) == []


def test_the_check_finds_the_claude_otel_shape(tmp_path: Path) -> None:
    """Anchor the detector against the 2026-08-22 bug as it was actually written."""
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    bug = tasks / "main.yml"
    bug.write_text(
        "- name: Snapshot restart counts\n"
        "  ansible.builtin.command: kubectl get pods\n"
        "  register: restarts_before\n"
        "  when:\n"
        "    - manifests_render is changed\n"
        "- name: Fail if a selector matched no pods\n"
        "  ansible.builtin.assert:\n"
        "    that: item.stdout | trim | length > 0\n"
        '  loop: "{{ restarts_before.results | default([]) }}"\n'
        "  when: not ansible_check_mode\n"
    )
    problems = _offenders(bug)
    assert len(problems) == 1
    assert "restarts_before.stdout" in problems[0].message


def test_the_check_accepts_a_lazy_conditional_guard(tmp_path: Path) -> None:
    """k8s/volume-claim's shape: the deref sits behind `if <same gate> else`.

    Jinja's conditional expression is lazy, so `seed_volume_marker.rc` is never evaluated on
    the run where its producer was skipped. Demanding a `| default()` here is not a tidier
    spelling — that role measured it, and the default renders the expression True and would
    tar a long-gone source over every live volume.
    """
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    ok = tasks / "seed.yml"
    ok.write_text(
        "- name: Check the seed marker\n"
        "  when: not (seed_volume_short_circuit | bool)\n"
        "  ansible.builtin.command: kubectl exec test -f .seeded\n"
        "  register: seed_volume_marker\n"
        "- name: Decide whether this run copies\n"
        "  ansible.builtin.set_fact:\n"
        "    seed_volume_copying: >-\n"
        "      {{ false if (seed_volume_short_circuit | bool)\n"
        "         else (seed_volume_marker.rc != 0) }}\n"
    )
    assert _offenders(ok) == []


def test_the_lazy_guard_still_catches_an_unrelated_gate(tmp_path: Path) -> None:
    """Control: the exemption must key on the producer's OWN gate, not on any `if/else`.

    A conditional testing some other variable proves nothing about whether the producer ran,
    so the deref is still reachable on a skip and must still be reported.
    """
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    bad = tasks / "seed.yml"
    bad.write_text(
        "- name: Check the seed marker\n"
        "  when: not (seed_volume_short_circuit | bool)\n"
        "  ansible.builtin.command: kubectl exec test -f .seeded\n"
        "  register: seed_volume_marker\n"
        "- name: Decide whether this run copies\n"
        "  ansible.builtin.set_fact:\n"
        "    seed_volume_copying: >-\n"
        "      {{ false if (some_other_flag | bool)\n"
        "         else (seed_volume_marker.rc != 0) }}\n"
    )
    problems = _offenders(bad)
    assert len(problems) == 1
    assert "seed_volume_marker.rc" in problems[0].message


def test_the_check_accepts_a_filtered_loop(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    fixed = tasks / "main.yml"
    fixed.write_text(
        "- name: Snapshot restart counts\n"
        "  ansible.builtin.command: kubectl get pods\n"
        "  register: restarts_before\n"
        "  when:\n"
        "    - manifests_render is changed\n"
        "- name: Fail if a selector matched no pods\n"
        "  ansible.builtin.assert:\n"
        "    that: item.stdout | trim | length > 0\n"
        '  loop: "{{ restarts_before.results | default([]) '
        "| rejectattr('skipped', 'defined') | list }}\"\n"
        "  when: not ansible_check_mode\n"
    )
    assert _offenders(fixed) == []
