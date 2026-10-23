"""*Bootstrapping Flux on a rebuilt cluster* keeps its withheld-root order (D5 steps 28 and 29).

docs/flux-deploy-plane.md's bootstrap section is read alone, bounded by its heading. Three
mistakes would rebuild a cluster with empty volumes or with objects Flux cannot adopt:

- the root Kustomization's apply before the restore step, so every stateful tag provisions an
  empty PVC under a backed-up name;
- a hand apply without `--field-manager=kustomize-controller`, the root's own manager, so the
  root's first apply conflicts with the operator's fields;
- step 2 naming `reset_to_last_good` without its condition, that no tag changed between
  `last-good` and `prod` is a snapshot role or a forward-only tag, and without the recorded
  judgment that excepts it. A reset across such a tag applies an older image onto restored data.

R4's PR 7b `awk` program, carried verbatim, runs over the whole document and must exit 0 with
empty output. It refuses an Ansible deploy or a hand suspend of the root anywhere in the
section, fenced lines included. It joined this test in D5's namespace PR, which deleted the
interim `./scripts/deploy.sh` step that R4's red half relies on. The plan is
docs/target-architecture/deploy/d5-bulk-move.md, steps 29 and 30, and
docs/target-architecture/render/r4-delete-k8s-ansible-path.md, *PR 7b*.

Run: uv run pytest ansible/tests/repo/test_flux_bootstrap_section.py
"""

import re
import shutil

import pytest

from lib.proc_testing import run
from _markdown_section import command_lines, normalised, section, step_spans, steps
from lib.repo_paths import REPO

DOC = REPO / "docs" / "flux-deploy-plane.md"
HEADING = "## Bootstrapping Flux on a rebuilt cluster"
FIELD_MANAGER = "--field-manager=kustomize-controller"
RESTORE = "runbook_gates.py longhorn-dr"
CONDITION = (
    "only when no tag changed between `last-good` and `prod` is a snapshot role or a "
    "forward-only tag"
)
EXCEPTION = ("The one exception", "recorded in the hold's thread before the dispatch")

# R4's PR 7b program, byte for byte between its shell quotes. Its exit codes: 0, the section was
# read whole and holds no forbidden form; 1, a forbidden form, each line printed with its
# number; 2, no section was read; 3, fewer than 20 lines or no `runbook_gates.py longhorn-dr`.
R4_PR_7B_AWK = r"""
  /^[ \t]*(```|~~~)/ { fence = !fence }
  !fence && /^#+ / {
    if (f) { if (index($0, " ") <= l) exit }
    else if ($0 ~ /^#+ Bootstrapping Flux on a rebuilt cluster$/) { f = 1; l = index($0, " "); next }
  }
  f {
    n++
    if ($0 ~ /runbook_gates\.py longhorn-dr/) a = 1
    if ($0 ~ /\.\/scripts\/deploy\.sh|ansible-playbook [^ ]*deploy\.yml|roles\/k8s\/flux|^[ \t]*flux_sync_suspend: *true|flux_sync_suspend=|(patch|suspend) kustomizations?[^ ]*[ \/]homelab( |$)/) { h = 1; print FNR ": " $0 }
  }
  END {
    if (!f) { print "no section read" > "/dev/stderr"; exit 2 }
    if (n < 20 || !a) { print "section: " n " lines, DR gate anchor " (a ? "present" : "absent") > "/dev/stderr"; exit 3 }
    if (h) exit 1
  }"""
# The interim step the flux PR wrote before step 1, which the namespace PR deleted. Its
# `./scripts/deploy.sh` line is the one R4's red half expects the program to print.
FLUX_PR_INTERIM_STEP = """**Until D5's namespace PR lands**, the `homelab` Namespace and its LimitRange come only from
the Ansible k8s play's `pre_tasks`. So run the deploy once first. Every k8s service is
flux-owned, so its batches visit none. Then check that both objects exist:

```bash
./scripts/deploy.sh
kubectl get namespace homelab
kubectl -n homelab get limitrange
```

The namespace PR deletes this step, because the Kustomization `homelab-namespace` then
creates both objects.

"""
# The rationale's clause before D5-I1-03 reworded it, with the refused key and value as a YAML
# line: the form the program's `^[ \t]*flux_sync_suspend: *true` pattern refuses.
RATIONALE = "the renderer refuses a\nsuspended sync render,"
RATIONALE_NAMING_THE_KEY = "the renderer refuses\nflux_sync_suspend: true\nas a value,"

_HAND_APPLY = re.compile(r"\bkubectl apply\b")
_ROOT_APPLY = re.compile(r"\bkubectl apply\b.*\broot\.yaml\b")


def bootstrap_problems(text: str) -> list[str]:
    """One line per mistake in the bootstrap section's text."""
    problems = []
    lines = command_lines(text)
    root = next((i for i, line in enumerate(lines) if _ROOT_APPLY.search(line)), None)
    restore = next((i for i, line in enumerate(lines) if RESTORE in line), None)
    if root is None or restore is None:
        problems.append("no root apply or no restore step to order")
    elif root < restore:
        problems.append("the root's apply comes before the restore step")
    for line in lines:
        if _HAND_APPLY.search(line) and FIELD_MANAGER not in line:
            problems.append(f"a hand apply without {FIELD_MANAGER}: {line}")
    step_2 = normalised(steps(text).get(2, ""))
    if "reset_to_last_good" in step_2 and not (
        CONDITION in step_2 and all(part in step_2 for part in EXCEPTION)
    ):
        problems.append(
            "step 2 names reset_to_last_good without its condition and exception"
        )
    return problems


def _section() -> str:
    return section(DOC.read_text(), HEADING)


def test_bootstrap_problems_is_clean_on_the_section():
    text = _section()
    # Non-vacuity: nine steps, step 2 names the reset, and the hand applies are there.
    assert sorted(steps(text)) == list(range(1, 10))
    assert "reset_to_last_good" in steps(text)[2]
    assert sum(bool(_HAND_APPLY.search(line)) for line in command_lines(text)) >= 5
    assert bootstrap_problems(text) == []


def test_bootstrap_problems_is_flagged_on_step_9_moved_before_step_7():
    text = _section()
    lines = text.splitlines()
    spans = {n: (a, b) for n, a, b in step_spans(text)}
    block = {n: lines[a:b] for n, (a, b) in spans.items()}
    first, last = spans[1][0], spans[9][1]
    order = [1, 2, 3, 4, 5, 6, 9, 7, 8]
    moved = lines[:first] + [line for n in order for line in block[n]] + lines[last:]
    assert bootstrap_problems("\n".join(moved)) == [
        "the root's apply comes before the restore step"
    ]


def test_bootstrap_problems_is_flagged_on_step_2_without_its_condition():
    text = _section()
    words = r"\s+".join(re.escape(word) for word in CONDITION.split())
    fixture, count = re.subn(words, "", text)
    assert count == 1
    assert bootstrap_problems(fixture) == [
        "step 2 names reset_to_last_good without its condition and exception"
    ]


def test_bootstrap_problems_is_flagged_on_a_hand_apply_without_the_field_manager():
    text = _section()
    fixture = text.replace(f"{FIELD_MANAGER} -f <out>/root.yaml", "-f <out>/root.yaml")
    assert fixture != text
    [problem] = bootstrap_problems(fixture)
    assert problem.startswith(f"a hand apply without {FIELD_MANAGER}: ")


def r4_pr_7b(text: str) -> tuple[int, str]:
    """R4's PR 7b program's exit code and standard output over `text`, read from stdin."""
    done = run(["awk", R4_PR_7B_AWK], input=text, check=False)
    return done.returncode, done.stdout


_needs_awk = pytest.mark.skipif(
    shutil.which("awk") is None, reason="R4's PR 7b check is an awk program"
)


@_needs_awk
def test_r4_pr_7b_exits_0_with_no_output_on_the_document():
    assert r4_pr_7b(DOC.read_text()) == (0, "")


@_needs_awk
def test_r4_pr_7b_prints_the_interim_deploy_step_the_flux_pr_wrote():
    text = DOC.read_text()
    fixture = text.replace("1. Confirm", FLUX_PR_INTERIM_STEP + "1. Confirm", 1)
    assert fixture != text
    code, out = r4_pr_7b(fixture)
    (line,) = out.splitlines()
    assert code == 1
    assert line.endswith(": ./scripts/deploy.sh"), line


@_needs_awk
def test_r4_pr_7b_refuses_a_rationale_naming_the_refused_key_as_true():
    text = DOC.read_text()
    assert text.count(RATIONALE) == 1
    fixture = text.replace(RATIONALE, RATIONALE_NAMING_THE_KEY)
    code, out = r4_pr_7b(fixture)
    (line,) = out.splitlines()
    assert code == 1
    assert line.endswith(": flux_sync_suspend: true"), line
