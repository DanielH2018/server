"""Tree-wide guard for the skipped-register class.

WHY THIS EXISTS. A SKIPPED Ansible task still sets the variable it registers to. The value is
a result dict carrying `skipped: true` and no `stdout`/`rc` key at all. So a consumer that
dereferences `<reg>.stdout`, or loops `<reg>.results` and reads `item.stdout`, blows up with
"object of type 'dict' has no attribute 'stdout'" on exactly the runs where the producer's
`when:` was false — which are the runs nobody tests.

This has now bitten twice:

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
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _REPO_ROOT / "ansible" / "roles"

# Attributes a skip result does not carry. `results` is deliberately absent: a skipped
# looped task DOES carry `results`, which is why looping it is safe and reading `.stdout`
# off its entries is not.
_SKIP_MISSING = ("stdout", "stderr", "rc")

_SKIP_FILTERS = (
    "rejectattr('skipped'",
    'rejectattr("skipped"',
    "is not skipped",
    "selectattr",
)


def _task_files() -> list[Path]:
    return sorted(p for p in _ROLES.rglob("tasks/*.yml") if "archive" not in p.parts)


def _flatten(tasks) -> list[dict]:
    """Walk block/rescue/always so a nested task is not invisible to the check."""
    out: list[dict] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        out.append(task)
        for key in ("block", "rescue", "always"):
            if key in task:
                out.extend(_flatten(task[key]))
    return out


def _when_text(task: dict) -> str:
    when = task.get("when")
    if when is None:
        return ""
    if isinstance(when, list):
        return " and ".join(str(w) for w in when)
    return str(when)


def _expressions(task: dict) -> str:
    """Every templated string in the task, so a reference anywhere is seen."""
    return yaml.safe_dump(task, default_flow_style=False)


def _unguarded_deref(body: str, reg: str, attr: str) -> bool:
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


def _offenders(path: Path) -> list[str]:
    try:
        tasks = _flatten(yaml.safe_load(path.read_text()))
    except yaml.YAMLError:
        return []  # the manifest/lint hooks own YAML validity; this check owns semantics
    conditional = _producers(tasks)
    if not conditional:
        return []

    problems = []
    for task in tasks:
        if task.get("register") in conditional:
            continue  # the producer itself
        body = _expressions(task)
        consumer_when = _when_text(task)
        loop = str(task.get("loop", ""))
        for reg, producer_conditions in conditional.items():
            deref = [
                attr for attr in _SKIP_MISSING if _unguarded_deref(body, reg, attr)
            ]
            if not deref:
                continue
            if all(cond in consumer_when for cond in producer_conditions):
                continue  # consumer repeats every risky guard the producer carries
            if any(f in loop for f in _SKIP_FILTERS):
                continue  # consumer filters the skip results out
            producer_when = " and ".join(producer_conditions)
            try:
                where = path.relative_to(_REPO_ROOT)
            except ValueError:
                where = path  # a tmp_path fixture in this file's own tests
            problems.append(
                f"{where}: task {task.get('name', '<unnamed>')!r} reads "
                f"{reg}.{deref[0]} but {reg}'s producer is gated on `{producer_when.strip()}`. "
                "A skipped task still sets its register, and the skip result has no "
                f"`{deref[0]}`. Either repeat the producer's condition, or filter the loop "
                "with `| rejectattr('skipped', 'defined') | list`."
            )
    return problems


@pytest.mark.parametrize(
    "path", _task_files(), ids=lambda p: str(p.relative_to(_ROLES))
)
def test_no_unguarded_consumer_of_a_conditional_register(path: Path) -> None:
    problems = _offenders(path)
    assert not problems, "\n".join(problems)


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
    assert "restarts_before.stdout" in problems[0]


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
