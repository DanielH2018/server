"""The operator-only runbooks say their steps in a safe order, one `RunbookRule` row each.

Every step these sections describe needs `sudo k3s kubectl` or the `DEPLOY_HOLD` variable, so
no test can run them, and each rule reads its section as text (D5 step 29, ADR-0020). Each
rule is a mistake that leaves the cluster worse than the failure the runbook answers. The
offline hold order in docs/break-glass.md's shed scenario is a pinned sentence with a case
table, which no row can hold; it stays in `tests/longhorn/test_volume_revert_runbook_text.py`.

Run: uv run pytest tests/repo/test_runbook_rules.py
"""

import re
from dataclasses import replace

import pytest
from _runbook_rule import (
    COMMANDS,
    ITEMS,
    LINES,
    SECTION,
    STEPS,
    Red,
    RunbookRule,
    check,
    edit,
    edit_words,
    move_line_after,
    move_step,
    proof_problems,
    replaced_by,
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
BOOTSTRAP_LINK = "flux-deploy-plane.md#bootstrapping-flux-on-a-rebuilt-cluster"

_SUSPEND = re.compile(r'\bpatch\b.*"suspend":\s*(true|false)')
_SERVER_SIDE_APPLY = re.compile(r"\bkubectl apply\b.*--server-side")
_HOLD_SET = re.compile(r"gh variable set DEPLOY_HOLD --body (?!none\b)")
_HAND_APPLY = re.compile(r"\bkubectl apply\b")
_ROOT_APPLY = re.compile(r"\bkubectl apply\b.*\broot\.yaml\b")
_KUBECTL_PATCH = re.compile(r"\bkubectl\b.*\bpatch\b")

# The forms R4's PR 7b refused anywhere in the bootstrap section, fenced lines included: a
# local deploy, an Ansible deploy of the flux tag, and a hand suspend of the root.
_BOOTSTRAP_FORBIDDEN = re.compile(
    r"\./scripts/deploy\.sh|ansible-playbook [^ ]*deploy\.yml|roles/k8s/flux"
    r"|^[ \t]*flux_sync_suspend: *true|flux_sync_suspend="
    r"|(patch|suspend) kustomizations?[^ ]*[ /]homelab( |$)"
)

# The interim step the flux PR wrote before bootstrap step 1, which D5's namespace PR deleted.
_FLUX_PR_INTERIM_STEP = """**Until D5's namespace PR lands**, run the deploy once first:

```bash
./scripts/deploy.sh
kubectl get namespace homelab
```

"""

# docs/break-glass.md's *daniel-box is dead* scenario as `git show 70591c307:docs/break-glass.md`
# prints it, every line below its heading.
_DEAD_BOX_AT_70591C307 = """
daniel-server keeps its own SOPS key, so decryption still works without the recovery key.

1. Follow `docs/longhorn-disaster-recovery.md` → *Procedure (fresh host, total loss)*,
   through *Cluster bring-up*: bring up a replacement daniel-box with `ansible/bootstrap.yml`, add its
   pubkey to `secrets/.sops.yaml`, `sops updatekeys` from daniel-server, commit, pull.
2. Decide whether cluster **objects** (Deployments, Secrets, PVC bindings) are also lost —
   if so, restore etcd first per `docs/k3s-etcd-restore.md` (needs item 2, the cluster
   token, if this is a genuinely new host rather than the same disk).
3. Restore Longhorn volumes per `docs/longhorn-disaster-recovery.md` (*Wait for the backupstore sync*, then
   *Restore volumes BEFORE any deploy*), **before** the deploy — deploying first provisions
   empty PVCs under the same names.
4. Deploy: `./scripts/deploy.sh`.
5. Verify with `probe.py targets` / `probe.py health <svc>` per that doc's *Verify* step.
"""

_HAND_PATCH_REASON = (
    "A hand suspend or resume under kubectl's default manager is taken back by "
    "kustomize-controller at the next reconcile of the Kustomization that applies the "
    f"suspended object. `{OPERATOR_MANAGER}` matches none of the managers it cleans."
)
_HOLD_FIRST_REASON = (
    "A hand-apply branch sets DEPLOY_HOLD before its apply, so a promotion cannot move `prod` "
    "past the reset while the operator works."
)
_COLD_START_REASON = (
    "No local script installs Flux or deploys a workload on the cluster, so a cold-start "
    "runbook sends the operator to the bootstrap section, which owns the bring-up order. "
    "`./scripts/deploy.sh` is gone, so a step that names it is wrong wherever the link is."
)


def _deploy_step_added(text: str) -> str:
    """The section with a step naming the retired script appended, its link left in place."""
    return text + "\n9. Deploy: `./scripts/deploy.sh`.\n"


def _cold_start(doc: str, heading: str, *red: Red) -> RunbookRule:
    return RunbookRule(
        name=f"cold-start-links-the-bootstrap:{doc}",
        reason=_COLD_START_REASON,
        doc=doc,
        heading=(heading,),
        unit=SECTION,
        require=(BOOTSTRAP_LINK,),
        forbid=re.compile(r"\./scripts/deploy\.sh"),
        red=(
            *(red or (edit(BOOTSTRAP_LINK, "flux-deploy-plane.md", every=True),)),
            _deploy_step_added,
        ),
    )


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
    RunbookRule(
        name="bootstrap-holds-no-deploy-or-root-suspend",
        reason=(
            "The flux tag is flux-owned: no local or Ansible deploy installs Flux, and the "
            "root is withheld until the restores, never applied and suspended. The renderer "
            "refuses `flux_sync_suspend: true`, so the section never shows it as a value."
        ),
        doc=FLUX,
        heading=(BOOTSTRAP,),
        unit=LINES,
        forbid=_BOOTSTRAP_FORBIDDEN,
        min_matches=20,
        red=(
            edit("1. Confirm", _FLUX_PR_INTERIM_STEP + "1. Confirm"),
            edit(
                "the renderer refuses a\nsuspended sync render,",
                "the renderer refuses\nflux_sync_suspend: true\nas a value,",
            ),
        ),
    ),
    _cold_start(
        BREAK_GLASS,
        "### Scenario: daniel-box is dead, daniel-server survives",
        replaced_by(_DEAD_BOX_AT_70591C307),
    ),
    _cold_start(
        "docs/longhorn-disaster-recovery.md", "## Procedure (fresh host, total loss)"
    ),
    _cold_start("docs/k3s-etcd-restore.md", "## After the restore"),
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
        name="branch-1-holds-no-revert-command",
        reason=(
            "Branch 1 runs the revert line of each role's block in the post, so it holds no "
            "one-role revert command of its own for an operator to copy."
        ),
        doc=SNAPSHOT_DRILLS,
        heading=(REVERT_RUNBOOK, BRANCH_1),
        unit=LINES,
        forbid=re.compile(r"volume-revert\.yml|volume_revert\.py|ansible-playbook"),
        red=(
            edit(
                "Run the revert line of each role's block in the",
                "Run `sudo .venv/bin/python -B scripts/backup/volume_revert.py --service <s> "
                "--sha <digest8> --apply`, then the revert line of each role's block in the",
            ),
            edit(
                "Run the revert line of each role's block in the",
                "Run `ansible-playbook revert.yml`, then the revert line of each role's "
                "block in the",
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
