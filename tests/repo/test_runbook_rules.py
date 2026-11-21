"""The operator-only runbooks say their steps in a safe order, one `RunbookRule` row each.

Every step these sections describe needs `sudo k3s kubectl` or the `DEPLOY_HOLD` variable, so
no test can run them, and each rule reads its section as text (D5 step 29, ADR-0020). Each
rule is a mistake that leaves the cluster worse than the failure the runbook answers.

Run: uv run pytest tests/repo/test_runbook_rules.py
"""

import re
from dataclasses import replace

import pytest
from _runbook_rule import (
    BLOCKS,
    COMMANDS,
    ITEMS,
    LINES,
    STEPS,
    RunbookRule,
    check,
    edit,
    edit_words,
    move_line_after,
    move_step,
    proof_problems,
)

FLUX = "docs/flux-deploy-plane.md"
SNAPSHOT_DRILLS = "docs/volume-snapshot-drills.md"
BREAK_GLASS = "docs/break-glass.md"

RECOVERY = "## Recovering Flux by hand"
UPGRADE = "### A broken controller upgrade"
SYNC = "### A broken sync object"
BOOTSTRAP = "## Bootstrapping Flux on a rebuilt cluster"
REVERT_RUNBOOK = "## The revert runbook"
BRANCH_1 = "### Branch 1: a snapshot role failed"
SHED = "### Scenario: one node is down and the survivor runs short of memory"

OPERATOR_MANAGER = "--field-manager=homelab-operator"
KUSTOMIZE_MANAGER = "--field-manager=kustomize-controller"

_SUSPEND = re.compile(r'\bpatch\b.*"suspend":\s*(true|false)')
_SERVER_SIDE_APPLY = re.compile(r"\bkubectl apply\b.*--server-side")
_HOLD_SET = re.compile(r"gh variable set DEPLOY_HOLD --body (?!none\b)")
_HAND_APPLY = re.compile(r"\bkubectl apply\b")
_ROOT_APPLY = re.compile(r"\bkubectl apply\b.*\broot\.yaml\b")
_KUBECTL_PATCH = re.compile(r"\bkubectl\b.*\bpatch\b")

_HAND_PATCH_REASON = (
    "A hand suspend or resume under kubectl's default manager is taken back by "
    "kustomize-controller at the next reconcile of the Kustomization that applies the "
    f"suspended object. `{OPERATOR_MANAGER}` matches none of the managers it cleans."
)
_HOLD_FIRST_REASON = (
    "A hand-apply branch sets DEPLOY_HOLD before its apply, so a promotion cannot move `prod` "
    "past the reset while the operator works."
)
# The shed scenario's one statement of the offline order, and the token `_offline_order`
# folds it to, so that `forbid` reads only the bullet's other sentences.
_OFFLINE_ORDER = (
    "Once the WAN returns, run this step's `gh variable set DEPLOY_HOLD` first, before any "
    "resume and before step 4, because a queued `promote` run can start as soon as GitHub "
    "answers."
)
_PINNED = "<the pinned offline order>"
# The last line of the bullet in the document, which the nested-item and continuation reds extend.
_OFFLINE_ORDER_LINE = "      before step 4, because a queued `promote` run can start as soon as GitHub answers."


def _offline_order(text: str) -> list[str]:
    """Each list item and what hangs from it, the pinned offline-order sentence folded to a token."""
    return [item.replace(_OFFLINE_ORDER, _PINNED) for item in BLOCKS(text)]


ROWS = (
    RunbookRule(
        name="recovery-hand-patch-names-the-operator-manager",
        reason=_HAND_PATCH_REASON,
        doc=FLUX,
        heading=(RECOVERY,),
        unit=LINES,
        select=_SUSPEND,
        require=(OPERATOR_MANAGER,),
        min_matches=6,
        red=(
            edit(
                f'{OPERATOR_MANAGER} -p \'{{"spec":{{"suspend":false}}}}\'',
                '-p \'{"spec":{"suspend":false}}\'',
            ),
        ),
    ),
    RunbookRule(
        name="recovery-server-side-apply-forces-conflicts",
        reason=(
            "The API server refuses a server-side apply on exactly the fields that broke, "
            "unless it carries `--force-conflicts`."
        ),
        doc=FLUX,
        heading=(RECOVERY,),
        unit=LINES,
        select=_SERVER_SIDE_APPLY,
        require=("--force-conflicts",),
        min_matches=2,
        red=(edit("apply --server-side --force-conflicts", "apply --server-side"),),
    ),
    *(
        RunbookRule(
            name=f"recovery-hold-precedes-the-apply:{branch}",
            reason=_HOLD_FIRST_REASON,
            doc=FLUX,
            heading=(RECOVERY, branch),
            unit=LINES,
            select=_SERVER_SIDE_APPLY,
            precede=_HOLD_SET,
            red=(move_line_after(_HOLD_SET, _SERVER_SIDE_APPLY),),
        )
        for branch in (UPGRADE, SYNC)
    ),
    RunbookRule(
        name="bootstrap-restore-precedes-the-root-apply",
        reason=(
            "The root Kustomization provisions every stateful tag's PVC. Applied before the "
            "Longhorn restore, it provisions empty volumes under the backed-up names."
        ),
        doc=FLUX,
        heading=(BOOTSTRAP,),
        unit=LINES,
        select=_ROOT_APPLY,
        precede=re.compile(r"runbook_gates\.py longhorn-dr"),
        red=(move_step(9, before=7),),
    ),
    RunbookRule(
        name="bootstrap-hand-apply-names-the-root-manager",
        reason=(
            f"`{KUSTOMIZE_MANAGER}` is the root's own manager. A hand apply under another "
            "conflicts with the root's first apply."
        ),
        doc=FLUX,
        heading=(BOOTSTRAP,),
        unit=LINES,
        select=_HAND_APPLY,
        require=(KUSTOMIZE_MANAGER,),
        min_matches=5,
        red=(edit(f"{KUSTOMIZE_MANAGER} -f <out>/root.yaml", "-f <out>/root.yaml"),),
    ),
    RunbookRule(
        name="bootstrap-reset-names-its-condition-and-exception",
        reason=(
            "A reset to `last-good` across a snapshot role or a forward-only tag applies an "
            "older image onto restored data, so a step naming the reset states that condition "
            "and the recorded judgment that excepts it."
        ),
        doc=FLUX,
        heading=(BOOTSTRAP,),
        unit=STEPS,
        select=re.compile(r"reset_to_last_good"),
        require=(
            "only when no tag changed between `last-good` and `prod` is a snapshot role or "
            "a forward-only tag",
            "The one exception",
            "recorded in the hold's thread before the dispatch",
        ),
        red=(
            edit_words(
                "only when no tag changed between `last-good` and `prod` is a snapshot role "
                "or a forward-only tag",
                "",
            ),
        ),
    ),
    *(
        RunbookRule(
            name=f"hand-patch-names-the-operator-manager:{doc}",
            reason=_HAND_PATCH_REASON,
            doc=doc,
            heading=(heading,),
            unit=COMMANDS,
            select=_KUBECTL_PATCH,
            require=(OPERATOR_MANAGER,),
            min_matches=2,
            red=(edit(f"\n      {OPERATOR_MANAGER} -p", "\n      -p"),),
        )
        for doc, heading in ((SNAPSHOT_DRILLS, REVERT_RUNBOOK), (BREAK_GLASS, SHED))
    ),
    RunbookRule(
        name="shed-clear-follows-a-read-of-the-hold",
        reason=(
            "The shed scenario clears DEPLOY_HOLD only after reading it, because a clear that "
            "does not first read the hold can erase a failure hold set meanwhile."
        ),
        doc=BREAK_GLASS,
        heading=(SHED,),
        unit=COMMANDS,
        select=re.compile(r"gh variable set DEPLOY_HOLD --body none"),
        precede=re.compile(r"gh variable get DEPLOY_HOLD"),
        red=(edit("`gh variable get DEPLOY_HOLD -R DanielH2018/server`", "nothing"),),
    ),
    RunbookRule(
        name="shed-offline-branch-sets-the-hold-before-any-resume",
        reason=(
            "When GitHub cannot be reached, the shed scenario sets DEPLOY_HOLD before any "
            "resume, because a queued `promote` run can start as soon as GitHub answers. A "
            "rule that parsed the ordering words opened a new bypass with each fix, so this "
            "row pins the one sentence. It refuses any second statement of the order beside "
            "it: a resume, a clear, another `gh variable set` or a mention of step 4. To "
            "reword the bullet, reword `_OFFLINE_ORDER` in the same commit."
        ),
        doc=BREAK_GLASS,
        heading=(SHED,),
        unit=_offline_order,
        select=re.compile(r"^- \*\*When GitHub cannot be reached\*\*"),
        require=(_PINNED,),
        forbid=re.compile(
            r'\bresum\w*|--body none|"suspend":false|\bgh variable set\b|\bstep 4\b'
            rf"|{re.escape(_PINNED)}.*{re.escape(_PINNED)}",
            re.IGNORECASE,
        ),
        red=tuple(
            edit_words(_OFFLINE_ORDER, sentence)
            for sentence in (
                "Once the WAN returns, before step 4 resume each suspended tag, then run "
                "this step's `gh variable set DEPLOY_HOLD`.",
                "Once the WAN returns, resume each suspended tag, then after that run "
                "`gh variable set DEPLOY_HOLD`, before step 4.",
                "Once the WAN returns, run this step's `gh variable set DEPLOY_HOLD`. "
                "Before that, resume each suspended tag.",
                "Once the WAN returns, resume each suspended tag first, then run this "
                "step's `gh variable set DEPLOY_HOLD`, before step 4, because a queued "
                "`promote` run can start as soon as GitHub answers.",
                _OFFLINE_ORDER.replace("DEPLOY_HOLD", "SOMETHING_ELSE"),
                _OFFLINE_ORDER.replace("DEPLOY_HOLD", "DEPLOY_HOLD --body none"),
                f"{_OFFLINE_ORDER} Before step 4 and before that set, resume each "
                "suspended tag.",
                f"Resume each suspended tag. {_OFFLINE_ORDER}",
                f"{_OFFLINE_ORDER} Then run `gh variable set DEPLOY_HOLD --body none`.",
                f"{_OFFLINE_ORDER} Then run `gh variable set SOMETHING_ELSE --body x`.",
                f"{_OFFLINE_ORDER} Then go to step 4.",
                f"{_OFFLINE_ORDER} {_OFFLINE_ORDER}",
            )
        )
        + (
            edit(
                _OFFLINE_ORDER_LINE,
                f"{_OFFLINE_ORDER_LINE}\n        - Resume each suspended tag before the hold.",
            ),
            edit(
                _OFFLINE_ORDER_LINE,
                f"{_OFFLINE_ORDER_LINE}\n\n      Resume each suspended tag first.",
            ),
        ),
    ),
    RunbookRule(
        name="classify-step-names-its-comparison-value",
        reason=(
            "A step that reads `lastAttemptedRevision` names the `prod@sha256:` value it is "
            "compared with, or the operator compares it with a SHA it never matches."
        ),
        doc=SNAPSHOT_DRILLS,
        heading=(REVERT_RUNBOOK,),
        unit=ITEMS,
        select=re.compile(r"lastAttemptedRevision"),
        require=("prod@sha256:",),
        red=(edit("`prod@sha256:<digest of sha-<R>>`", "R's SHA"),),
    ),
    RunbookRule(
        name="last-good-wait-reads-the-origin-key",
        reason=(
            "`.status.artifact.revision` is a digest, never a commit, so the wait for "
            "`last-good` reads the `org.opencontainers.image.revision` key."
        ),
        doc=SNAPSHOT_DRILLS,
        heading=(REVERT_RUNBOOK, BRANCH_1),
        unit=COMMANDS,
        select=re.compile(r"ocirepository homelab"),
        require=(r"org\.opencontainers\.image\.revision",),
        red=(
            edit(
                r"{.status.artifact.metadata.org\.opencontainers\.image\.revision}",
                "{.status.artifact.revision}",
            ),
        ),
    ),
)

_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_runbook_rule_holds_on_its_section(row: RunbookRule):
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_runbook_rule_flags_each_red_fixture(row: RunbookRule):
    problems = proof_problems(row)
    assert not problems, problems


_TOY = RunbookRule(
    name="toy",
    reason="toy",
    doc=FLUX,
    heading=(BOOTSTRAP,),
    unit=LINES,
    select=_HAND_APPLY,
    require=(KUSTOMIZE_MANAGER,),
    red=(edit(KUSTOMIZE_MANAGER, ""),),
)


def test_a_rule_whose_heading_is_gone_reports_its_subject_gone():
    [problem] = check(replace(_TOY, heading=(BOOTSTRAP, "### No such heading")))
    assert problem.startswith("toy: subject gone: delete this row"), problem


def test_commands_keeps_inline_spans_and_fenced_lines_in_document_order():
    text = "Read `gh variable get X`.\n\n```bash\nkubectl apply \\\n  -f a\n```\n\nThen `b`.\n"
    assert COMMANDS(text) == ["gh variable get X", "kubectl apply -f a", "b"]
