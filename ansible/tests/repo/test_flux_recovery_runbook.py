"""The recovery runbook for a broken Flux says its operator-only steps in a safe order (D5 step 29).

docs/flux-deploy-plane.md's *Recovering Flux by hand* is read alone, bounded by its heading,
because the bootstrap section's hand applies carry no `--force-conflicts`. Three mistakes
leave the cluster worse than the failure the runbook answers:

- a suspend or a resume without `--field-manager=homelab-operator`, which kustomize-controller
  takes back at the next reconcile of the Kustomization that applies the suspended object;
- a server-side apply without `--force-conflicts`, which the API server refuses on exactly the
  fields that broke;
- a hand-apply branch whose apply comes before its `DEPLOY_HOLD` step, so a promotion can move
  `prod` past the reset while the operator works.

The plan is docs/target-architecture/deploy/d5-bulk-move.md, step 29.

Run: uv run pytest ansible/tests/repo/test_flux_recovery_runbook.py
"""

import re

from _markdown_section import command_lines, section
from lib.repo_paths import REPO

DOC = REPO / "docs" / "flux-deploy-plane.md"
HEADING = "## Recovering Flux by hand"
UPGRADE = "### A broken controller upgrade"
SYNC = "### A broken sync object"
FIELD_MANAGER = "--field-manager=homelab-operator"

_SUSPEND = re.compile(r'\bpatch\b.*"suspend":\s*(true|false)')
_APPLY = re.compile(r"\bkubectl apply\b.*--server-side")
_HOLD = re.compile(r"gh variable set DEPLOY_HOLD --body (?!none\b)")


def _first(lines: list[str], pattern: re.Pattern) -> int | None:
    return next((i for i, line in enumerate(lines) if pattern.search(line)), None)


def recovery_problems(runbook: str) -> list[str]:
    """One line per mistake in the recovery runbook's text."""
    problems = []
    for line in command_lines(runbook):
        if _SUSPEND.search(line) and FIELD_MANAGER not in line:
            problems.append(f"a suspend or resume without {FIELD_MANAGER}: {line}")
        if _APPLY.search(line) and "--force-conflicts" not in line:
            problems.append(f"a server-side apply without --force-conflicts: {line}")
    for heading in (UPGRADE, SYNC):
        lines = command_lines(section(runbook, heading))
        hold, apply = _first(lines, _HOLD), _first(lines, _APPLY)
        if apply is None:
            problems.append(f"{heading}: no hand apply")
        elif hold is None or hold > apply:
            problems.append(f"{heading}: the apply precedes the DEPLOY_HOLD step")
    return problems


def _runbook() -> str:
    return section(DOC.read_text(), HEADING)


def _hold_after_apply(runbook: str, heading: str) -> str:
    """`runbook` with `heading`'s hold command moved to just after its hand apply."""
    branch = section(runbook, heading)
    lines = branch.splitlines()
    hold = next(i for i, line in enumerate(lines) if _HOLD.search(line))
    moved = lines.pop(hold)
    apply = next(i for i, line in enumerate(lines) if _APPLY.search(line))
    lines.insert(apply + 1, moved)
    return runbook.replace(branch, "\n".join(lines))


def test_recovery_problems_is_clean_on_the_runbook():
    runbook = _runbook()
    lines = command_lines(runbook)
    # Non-vacuity: three suspends, three resumes, and one hand apply in each apply branch.
    assert sum(bool(_SUSPEND.search(line)) for line in lines) >= 6
    assert sum(bool(_APPLY.search(line)) for line in lines) >= 2
    assert recovery_problems(runbook) == []


def test_recovery_problems_is_flagged_on_the_upgrade_branch_with_its_hold_after_the_apply():
    fixture = _hold_after_apply(_runbook(), UPGRADE)
    assert recovery_problems(fixture) == [
        f"{UPGRADE}: the apply precedes the DEPLOY_HOLD step"
    ]


def test_recovery_problems_is_flagged_on_the_sync_branch_with_its_hold_after_the_apply():
    fixture = _hold_after_apply(_runbook(), SYNC)
    assert recovery_problems(fixture) == [
        f"{SYNC}: the apply precedes the DEPLOY_HOLD step"
    ]


def test_recovery_problems_is_flagged_on_the_sync_branch_without_force_conflicts():
    runbook = _runbook()
    branch = section(runbook, SYNC)
    fixture = runbook.replace(branch, branch.replace("--force-conflicts ", ""))
    [problem] = recovery_problems(fixture)
    assert problem.startswith("a server-side apply without --force-conflicts: ")


def test_recovery_problems_is_flagged_on_a_resume_without_the_field_manager():
    runbook = _runbook()
    fixture = runbook.replace(
        f'{FIELD_MANAGER} -p \'{{"spec":{{"suspend":false}}}}\'',
        '-p \'{"spec":{"suspend":false}}\'',
        1,
    )
    assert fixture != runbook
    [problem] = recovery_problems(fixture)
    assert problem.startswith(f"a suspend or resume without {FIELD_MANAGER}: ")
