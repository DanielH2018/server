# ansible/roles/setup/gitops_deploy/files/deploy_remediation.py
"""The text a deferred change's alert prescribes, and the budget check behind it.

Every message that tells an operator what to run by hand is built here — the broad-change
pair (`broad_remediation`), the k8s defer-and-alert (`k8s_remediation`) and the structural
follow-ups (`deferred_service_alerts`) — so the four callers that quote them cannot drift
apart in what order they name the ff-merge and the playbook.
"""

from __future__ import annotations

from deploy_changes import ChangeSet, setup_role_playbook, setup_role_tag

# The branch `broad_remediation` names when a caller does not say. gitops_deploy.py reads the
# real one from config.env and passes it; the repo-side callers (deploy_tags, land_tags) run
# against this repo, where it is master.
BRANCH_DEFAULT = "master"


# A rollback re-run must fit inside the unit's TimeoutStartSec alongside the forward run and
# the worst-case flock wait. Below that margin systemd SIGTERMs mid-rollback, which strands
# the tree at the failed commit with live state half-applied — exactly what every
# hold-before-reset in gitops_deploy.py exists to prevent.
BROAD_BUDGET_MARGIN_S = 300


def broad_budget_ok(
    forward_s: int, rollback_s: int, flock_s: int, timeout_s: int
) -> bool:
    """Can this broad apply carry a rollback inside the unit's start timeout?

    Measured 2026-08-22, a full deploy.yml is 1212s. 180 + 1212 + 1212 = 2604 against
    TimeoutStartSec=2700 leaves 96s — 3.5%, so a run four percent slower than measured is
    killed mid-rollback. That is why the deploy-plane arm is forward-only, and this
    predicate is what makes the reasoning executable rather than a comment that rots.

    # DECIDED: the ceiling has since moved and the arm stays forward-only anyway.
    TimeoutStartSec went to 60min on 2026-08-29 to fund the staging gate, at which the same
    numbers FIT (2904 against 3600). This predicate has no production caller, so nothing changed
    behaviour; the arm is forward-only in gitops_deploy.py's code. Arming a broad rollback needs
    its own evidence — a re-measured deploy.yml on today's tree — not a ceiling raised for an
    unrelated feature. Pinned by
    test_deploy_remediation.py::test_the_budget_predicate_tracks_the_units_real_timeout,
    which carries the re-derivation.
    """
    return flock_s + forward_s + rollback_s + BROAD_BUDGET_MARGIN_S <= timeout_s


def broad_remediation(
    broad_deploy: bool,
    broad_setup: bool,
    setup_roles: set[str] | None = None,
    branch: str = BRANCH_DEFAULT,
) -> str:
    """The manual command(s) a broad (defer-and-alert) change needs, in the order they work.

    deploy.yml runs only container roles, so a setup-plane change (roles/setup/, requirements.yml,
    the bring-up playbooks) needs `initial_setup.yml --tags <role>`; naming deploy.yml there is a
    silent no-op that leaves the change unapplied while a plain ff-merge clears the divergence —
    worst case a fix to gitops_deploy.py itself (2026-07-16 review M1). A push hitting both planes
    names both.

    `setup_roles` narrows the setup half from that `<role>` placeholder to the real command,
    and exists because the placeholder's *playbook* was wrong for some roles rather than merely
    vague — see `_SETUP_ROLES_OUTSIDE_INITIAL_SETUP`. Omitting it keeps the old generic text,
    which is what every caller with no path list still gets.

    THE FF-MERGE COMES FIRST, and that is the half this returned string exists to carry.
    Ansible renders from the working tree, so a playbook run before the merge copies the
    PRE-merge files and reports `changed=0` — a clean, idempotent-looking recap over the old
    code. This role's own CLAUDE.md has documented that as a trap since 2026-07; the callers
    went on printing the reverse order anyway, and on 2026-09-01 an operator following the
    printed order shipped the previous `deploy_logic.py` and only caught it by grepping
    /opt/gitops-deploy afterwards. Ordering the pair here rather than at each call site is
    what stops the four of them drifting apart again.
    """
    cmds: list[str] = []
    if broad_deploy:
        cmds.append("`ansible-playbook ansible/deploy.yml`")
    if broad_setup:
        cmds.extend(_setup_commands(setup_roles))
    return f"`git merge --ff-only origin/{branch}` FIRST, then " + " and ".join(cmds)


def _setup_commands(setup_roles: set[str] | None) -> list[str]:
    """One command per setup role, or the generic placeholder when no roles are known."""
    if not setup_roles:
        return ["`ansible-playbook ansible/initial_setup.yml --tags <role>`"]
    cmds = []
    for role in sorted(setup_roles):
        playbook = setup_role_playbook(role)
        if playbook is None:
            # No playbook includes this role, so there is no single command to print. Naming
            # its consumers is the only actionable thing left, and it is genuinely two
            # commands on two hosts — see the `common` note on the mapping above.
            cmds.append(
                f"`{role}` is read by other roles and applied by no playbook of its own — "
                "apply each consumer (`ansible-playbook ansible/k3s-bringup.yml --tags "
                "<tag>` on daniel-box, `ansible-playbook ansible/initial_setup.yml --tags "
                "optimize_pi -e target=daniel-pi` on daniel-pi)"
            )
            continue
        cmds.append(f"`ansible-playbook {playbook} --tags {setup_role_tag(role)}`")
    return cmds


def k8s_remediation(
    roles: set[str], declared: set[str], extra_consumers: set[str] | None = None
) -> str:
    """The redeploy instruction for a set of changed k8s roles, given this host's declared set.

    `_ACTIVE_K8S` matches every `ansible/roles/k8s/<role>/` path, but only a role with a
    `containers_list` entry has a deploy tag. deploy.yml includes k8s roles per entry with
    `tags: [<entry name>]`, so `--tags <role>` for a role with no entry matches nothing and
    ANSIBLE EXITS 0 — the operator runs the prescribed command, sees green, and the change is
    never applied. `scripts/deploy_tools/deploy_tags.py` catches it downstream with exit 2, but the alert
    itself was pointing at a command that cannot work.

    Eight roles are in that position today (manifests, rollout-drain, volume-claim,
    volume-snapshot, volume-revert, image-builder, longhorn-api, cronjob-gate) and they are the
    shared plane: `manifests` is the apply+rollout path for EVERY workload and `volume-revert` is
    the auto-deploy rollback path. They are not rare, either — 46 commits since 2026-06-01 touch
    only roles in that set.

    DECIDED: name a full deploy for the shared roles instead of routing them to `cs.broad`.
    Broad routing was the review's proposed fix and it costs more than it fixes: main() returns on
    `cs.broad` WITHOUT fast-forwarding, so every such commit would park the whole local..origin
    range — holding back other sessions' commits and every k8s image-bump auto-deploy in the same
    range until an operator ran a full deploy by hand. This keeps the ff-merge and corrects only
    the instruction, which is where the defect actually was.
    """
    # Intersect with `declared` BEFORE the union. A consumer that is not in this host's
    # containers_list has no deploy tag here, so folding it in raw would land it in `shared`
    # and escalate the instruction from a scoped `--tags` to "run a full deploy" -- for a role
    # this host does not deploy at all. Inert today; live the first time a cross-role consumer
    # is Pi-only.
    roles = roles | ((extra_consumers or set()) & declared)
    shared = sorted(roles - declared)
    deployable = sorted(roles & declared)
    if not shared:
        return (
            "Redeploy by hand: `ansible-playbook ansible/deploy.yml --tags "
            f"{','.join(deployable)}`."
        )
    lead = (
        f"`{', '.join(shared)}` " + ("has" if len(shared) == 1 else "have") + " no "
        "`containers_list` entry, so **`--tags` matches nothing and Ansible exits 0** — a "
        "tag-scoped redeploy would report success having applied nothing. Run a full deploy: "
        "`ansible-playbook ansible/deploy.yml`."
    )
    if deployable:
        lead += (
            " The rest can be scoped: `ansible-playbook ansible/deploy.yml --tags "
            f"{','.join(deployable)}`."
        )
    return lead


def deferred_service_alerts(
    cs: ChangeSet, deployed: set[str]
) -> tuple[set[str], set[str]]:
    """The (tasks, meta) service sets that still need a defer-and-alert after a tick that
    redeployed `deployed` (empty on the docs-only branch — no service mapped).

    A `tasks/` or `meta/deps.yml` change is NOT auto-deployed, and unlike a doc edit it changes
    what a deploy DOES — so for a service that was not itself redeployed it must be flagged, not
    silently ff-merged. Subtracting `deployed` is the combined-push fix: a single push that
    deploys svcA (its template changed) while also carrying svcB's `meta/deps.yml` leaves svcB's
    deploy-graph change ff-merged but unapplied. The alert used to live only inside
    `if not cs.services:`, so ANY push that deployed something swallowed that remainder — the exact
    hole the meta/tasks defer-and-alert was added to close. A service whose own template changed is
    in `deployed`, so its bundled tasks/meta change rode the scoped `--tags` redeploy — no alert.

    Secrets are intentionally excluded here: the `/add-secret` flow ships `secrets.yml` WITH its
    consuming template (that consumer is in `deployed`), so keying a secrets alert on 'any deploy
    happened' would false-fire the happy path — the secrets alert stays on the no-services branch.
    """
    return cs.tasks - deployed, cs.meta - deployed
