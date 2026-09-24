# ansible/roles/setup/gitops_deploy/files/deploy_remediation.py
"""The text a deferred change's alert prescribes, and the budget check behind it.

Every message that tells an operator what to run by hand is built here — the broad-change
pair (`broad_remediation`), the k8s defer-and-alert (`k8s_remediation`) and the structural
follow-ups (`deferred_service_alerts`) — so the four callers that quote them cannot drift
apart in what order they name the ff-merge and the playbook.
"""

from __future__ import annotations

from deploy_changes import ChangeSet, setup_role_playbook, setup_role_tag
from gitops_markers import (  # noqa: F401
    CONTENTION_CLEAR_CMD,
    MANUAL_PLANE_CLEAR_CMD,
    MAXIMAL_ROLE_GATED_TAGS,
)

# The branch `broad_remediation` names when a caller does not say. gitops_deploy.py reads the
# real one from config.env and passes it; the repo-side callers (deploy_tags, land_tags) run
# against this repo, where it is master.
BRANCH_DEFAULT = "master"

# The two commands an operator runs against the markers — clearing one role's `manual_plane`
# line after applying it by hand, and ending a contention streak once the lock's holder is
# gone — live in `gitops_markers` beside the markers they act on, so monitor-bridge and the
# SessionStart banner print the same string from their own copies of that module. Imported
# above and re-exported because `deploy_logic` re-exports this module, and `land.sh` reads
# them through it.


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


def broad_park_reason(cs: ChangeSet) -> str:
    """Why a broad tick parked without fast-forwarding, in one journal-line clause.

    Two shapes still park, and this says which. A bring-up playbook
    (`_BROAD_MANUAL_PREFIXES`) runs by hand by construction. A setup-plane path that resolves
    to no ROLE has no hand command to print and no role to record, so parking is the only
    signal it has — `deploy_defer.parks_the_tick` is the predicate, and `_note_setup_role`
    matches `roles/setup/<name>/`, so a file directly under `roles/setup/` is broad,
    unroutable and nameless at once. That second branch can never name a role: any role this
    deployer cannot apply puts the range on `deploy_defer.record`'s path instead, which
    fast-forwards and writes the `manual_plane` marker, so the text says "no role" rather
    than interpolating a set that is empty by construction.

    The Discord alert beside it is throttled once per SHA, so it says nothing on the second
    and every later tick behind the same range. That left the journal with no deferral reason
    at all while daniel-box sat nine commits behind origin for twenty minutes on 2026-09-09
    (#1467), and the only per-tick line came from an unrelated comment-only path, which read
    as the cause and was not. The invisibility was the defect, not the park, so this text is
    logged EVERY tick and the throttle governs only the page. (That range was
    `roles/setup/k3s/`, which is now recorded rather than parked — the journal line it needed
    is what survives.)
    """
    if cs.broad_manual:
        return "a bring-up playbook changed, which runs by hand by construction"
    return (
        "a setup-plane path names no role — there is no tag to apply and no role to record, "
        "so nothing but staying behind origin says the change is unapplied"
    )


def broad_remediation(
    broad_deploy: bool,
    broad_setup: bool,
    setup_roles: set[str] | None = None,
    branch: str = BRANCH_DEFAULT,
    narrow_tags: dict[str, frozenset[str]] | None = None,
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

    `narrow_tags` narrows the `--tags` value one step further, from the whole-role tag to the
    tags of the tasks that read what changed. It is the deployer's `manual_plane_tags` marker,
    so a caller without that marker in reach omits it and gets the role tag.

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
        cmds.extend(_setup_commands(setup_roles, narrow_tags))
    return f"`git merge --ff-only origin/{branch}` FIRST, then " + " and ".join(cmds)


def manual_plane_remediation(
    setup_roles: set[str], narrow_tags: dict[str, frozenset[str]] | None = None
) -> str:
    """The commands that clear a `manual_plane` marker: apply each role, then clear its line.

    No `git merge --ff-only` preamble, which is the one way this differs from
    `broad_remediation`. The tick recording this marker has already fast-forwarded — that is
    the whole change the marker exists to make safe — so prescribing the merge again would
    describe a tree the operator is not in.

    The clear command is the reverse of the write, and it is part of the remediation rather
    than a note beside it: a role applied by hand with its line left behind pages GitOps
    Deploy — Status six hours later over work that is already live.
    """
    return (
        " and ".join(_setup_commands(setup_roles, narrow_tags))
        + f", then `{MANUAL_PLANE_CLEAR_CMD}`"
    )


# Setup roles whose role tag applies far more than any one change to them needs, and what
# running it actually does. A role in this map gets this warning where the printed command is
# the whole-role tag. Beside a narrowed `--tags` it would describe a run the operator is not
# being told to make, so `_setup_commands` swaps it for `_MAXIMAL_ROLE_GATED_WARNING` when
# the narrowed tags still reach the gated tasks, and drops it otherwise.
#
# THE NARROWER TAG IS DERIVED, AND THIS IS THE FALLBACK (#2307). `deploy_defer.record` asks
# `scripts/deploy_tools/narrow_setup.py` which of the role's own tags the changed paths reach,
# and stores the answer in the `manual_plane_tags` sidecar marker; every surface quoting this
# composer reads that marker, so all four print the same narrow tag. The derivation refuses on
# any doubt — an untagged task file, a variable nothing in the role reads, a `handlers/` change
# — and a refusal lands here, on the whole-role tag plus this warning. That is the safe
# direction: a `--tags` value matching nothing makes Ansible exit 0 having applied nothing,
# which is worse than a command that applies too much and says so.
#
# Keyed by ROLE NAME, which is what every caller passes — `setup_role_tag` maps that to the
# `--tags` value, and the two differ for `chezmoi_setup`.
# The narrower tags the k3s warning offers instead. A tuple rather than prose, so
# `test_every_narrower_tag_the_warning_names_exists_in_the_role` can check it against the
# role's own `tasks/` — a tag that matches nothing makes Ansible exit 0 having applied
# nothing, which is the silent-success failure `setup_tags_for` also guards against.
_K3S_NARROWER_TAGS = (
    "kubeconfig",
    "longhorn",
    "longhorn_backup",
    "coredns",
    "node-dns",
    "backup-health",
    "metallb",
)

# The gates that decide whether `--tags k3s` actually takes the control plane down. Each is a
# variable or a status string `roles/setup/k3s/tasks/server.yml` reads, pinned by
# `test_every_gate_the_warning_names_is_read_by_the_role` so the warning cannot outlive the
# task it describes.
_K3S_CONTROL_PLANE_GATES = ("k3s_server_args", "k3s_version", "reencrypt_finished")

_K3S_GATED_TASKS = (
    "three gated control-plane tasks: the "
    f"k3s installer, which restarts k3s when `{_K3S_CONTROL_PLANE_GATES[0]}` gained an "
    f"argument or `{_K3S_CONTROL_PLANE_GATES[1]}` moved; a systemd restart when the log "
    "drop-in changed; and `k3s secrets-encrypt rotate-keys`, which re-encrypts every "
    "Secret in etcd unless the cluster already reached "
    f"`{_K3S_CONTROL_PLANE_GATES[2]}`"
)

_MAXIMAL_ROLE_TAGS: dict[str, str] = {
    "k3s": (
        "that tag is the WHOLE role. It reapplies MetalLB, Longhorn, the backup targets, the "
        f"crons, CoreDNS and the node config, and arms {_K3S_GATED_TASKS}. Narrower tags, "
        "one per task file: "
        + ", ".join(f"`{t}`" for t in _K3S_NARROWER_TAGS)
        + "; `--list-tasks` shows what one selects"
    ),
}

# The narrower tags that still reach a role's gated tasks live in `gitops_markers`, imported
# above: the SessionStart banner needs the same set and cannot reach this module (#2345). What
# stays here is the long prose, which only the surfaces with room for it print.
_MAXIMAL_ROLE_GATED_WARNING: dict[str, str] = {
    "k3s": f"that tag list reaches tasks/server.yml, which arms {_K3S_GATED_TASKS}",
}


def maximal_tag_warning(role: str) -> str:
    """What running `role`'s whole-role tag does, or '' when its tag is not a blunt instrument.

    A three-line RBAC addition to `k3s_readonly_crd_api_groups` was answered with
    `ansible-playbook ansible/k3s-bringup.yml --tags k3s` on 2026-09-22 (#2294). That command
    restarts the control plane; the change needed `--tags kubeconfig`, which was applied
    instead with ok=15 changed=2. An operator following the printed command as written takes
    the restart, and a session on the default follow-through path takes it unattended.
    """
    return _MAXIMAL_ROLE_TAGS.get(role, "")


def _setup_commands(
    setup_roles: set[str] | None,
    narrow_tags: dict[str, frozenset[str]] | None = None,
) -> list[str]:
    """One command per setup role, or the generic placeholder when no roles are known.

    Args:
        setup_roles: the roles to name a command for.
        narrow_tags: role tag -> the narrower tags that role's own change needs, from the
            `manual_plane_tags` marker. A role absent from it, or present with an empty set,
            gets the whole-role tag.

    A role in `_MAXIMAL_ROLE_TAGS` gets its command annotated with what that command does, so
    every surface quoting this composer — land.sh's `needs-manual-apply` note, the deployer's
    journal, the `manual_plane` Discord alert — carries the warning from one place. The
    whole-role warning goes only on the whole-role tag. A narrowed `--tags` that still reaches
    the role's gated tasks (`_MAXIMAL_ROLE_GATED_TAGS`) carries the shorter gated warning.
    """
    if not setup_roles:
        return ["`ansible-playbook ansible/initial_setup.yml --tags <role>`"]
    narrow_tags = narrow_tags or {}
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
        role_tag = setup_role_tag(role)
        narrowed = narrow_tags.get(role_tag) or narrow_tags.get(role) or frozenset()
        tags = ",".join(sorted(narrowed)) or role_tag
        cmd = f"`ansible-playbook {playbook} --tags {tags}`"
        if not narrowed:
            warning = maximal_tag_warning(role)
        elif narrowed & MAXIMAL_ROLE_GATED_TAGS.get(role, frozenset()):
            warning = _MAXIMAL_ROLE_GATED_WARNING[role]
        else:
            warning = ""
        # Parenthesised, not appended after a dash: `manual_plane_remediation` adds ", then
        # <clear command>" after this list, and an unbracketed warning made that clause read
        # as a continuation of the warning's own last sentence.
        cmds.append(f"{cmd} (WARNING: {warning})" if warning else cmd)
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
    """Return the (tasks, meta) service sets that still need a defer-and-alert.

    Given a tick that redeployed `deployed` (empty on the docs-only branch — no service
    mapped). A `tasks/` or `meta/deps.yml` change is NOT auto-deployed, and unlike a doc edit it changes
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
