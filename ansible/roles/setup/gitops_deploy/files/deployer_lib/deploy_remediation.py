# ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_remediation.py
"""The text a deferred change's alert prescribes, and the budget check behind it.

Every message that tells an operator what to run by hand is built here, so the callers that
quote them cannot drift apart in what order they name the ff-merge and the playbook.
"""

from __future__ import annotations

from collections.abc import Iterable

from deployer_lib import deploy_cross_role
from deployer_lib.deploy_changes import (
    ChangeSet,
    is_routed,
    roles_outside_initial_setup_in,
    routing_failed,
    setup_role_host,
    setup_role_playbook,
    setup_role_tag,
)
from gitops_ledger import OWED_MANUAL_PLANE
from gitops_markers import (
    PLAYBOOK_CMD,
    owed_clear_cmd,
    on_hosts,
)

# The branch `broad_remediation` names when a caller does not say. gitops_deploy.py reads the
# real one from config.env and passes it; the repo-side callers (deploy_lib/tags.py, land_tags) run
# against this repo, where it is master.
BRANCH_DEFAULT = "master"

# The owed-ledger clear command (`owed_clear_cmd`) lives in `gitops_markers` beside the
# markers it acts on, so monitor-bridge and the SessionStart banner print the same string from
# their own copies of that module.


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

    # DECIDED: the arm stays forward-only whatever the ceiling reads.
    TimeoutStartSec was 60min and then 70min, at which the same numbers FIT (2904 against 3600),
    and the arm stayed forward-only. At 40min (2400s) they overrun again. This predicate has no
    production caller, so nothing changed behaviour; the arm is forward-only in gitops_deploy.py's
    code. Arming a broad rollback needs its own evidence — a re-measured deploy.yml on today's
    tree — and a ceiling raised to fund it. Pinned by
    test_deploy_remediation.py::test_the_budget_predicate_tracks_the_units_real_timeout,
    which carries the re-derivation.
    """
    return flock_s + forward_s + rollback_s + BROAD_BUDGET_MARGIN_S <= timeout_s


def broad_park_reason(cs: ChangeSet) -> str:
    """Why a broad tick parked without fast-forwarding, in one journal-line clause.

    Four shapes park, and this says which. A range whose secret-reader lookup failed parks
    until the next tick repeats it (#5436). A setup role parks while the routing derivation
    failed or timed out, so the next tick derives again (#4326). A bring-up playbook
    (`_BROAD_MANUAL_PREFIXES`) runs by hand by construction. A setup-plane path that resolves
    to no ROLE has no hand command to print and no role to record, so parking is the only
    signal it has — `deploy_defer.parks_the_tick` is the predicate, and `setup_roles_for`
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
    if cs.key_readers_failed:
        return (
            "the key-reader lookup failed (the journal line above says why), so this tick "
            "cannot tell which setup role reads the changed `secrets.yml` or inventory key, "
            "and the next tick retries it rather than merge past the change"
        )
    if cs.setup_roles and routing_failed():
        return (
            f"setup-role routing failed, so it could not place {', '.join(sorted(cs.setup_roles))} "
            "(the journal line above says why), and the next tick retries rather than guess "
            "a playbook or tag"
        )
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
    playbooks: Iterable[str] = (),
    key_readers_failed: bool = False,
) -> str:
    """The manual command(s) a broad (defer-and-alert) change needs, in the order they work.

    deploy.yml runs only container roles, so a setup-plane change (roles/setup/, requirements.yml,
    the bring-up playbooks) needs `initial_setup.yml --tags <role>`; naming deploy.yml there is a
    silent no-op that leaves the change unapplied while a plain ff-merge clears the divergence —
    worst case a fix to gitops_deploy.py itself (2026-07-16 review M1). A push hitting both planes
    names both.

    `setup_roles` narrows the setup half from that `<role>` placeholder to the real command,
    and exists because the placeholder's *playbook* was wrong for some roles rather than merely
    vague — see `setup_role_playbook`. Omitting it keeps the old generic text,
    which is what every caller with no path list still gets.

    `playbooks` is the changed bring-up playbooks, `ChangeSet.manual_playbooks`. A bring-up
    playbook sits under no role directory, so it adds nothing to `setup_roles`, and before
    #4282 a `k3s-bringup.yml`-only change printed `initial_setup.yml --tags <role>`: a
    placeholder that names the wrong playbook and matches no tag.

    `narrow_tags` narrows the `--tags` value one step further, from the whole-role tag to the
    tags of the tasks that read what changed. It is the `tags` key of the deployer's
    `manual_plane` ledger lines, so a caller without the ledger in reach omits it and gets the
    role tag.

    `key_readers_failed` (`ChangeSet.key_readers_failed`, #5436) replaces the whole
    answer with the retry. The range holds a `secrets.yml` or inventory change whose setup
    readers could not be looked up, so a hand merge would fast-forward past the one commit
    that carries the key and no later tick would apply the role. The text says the next tick
    retries and names the lookup to run by hand; it never prints the merge.

    THE FF-MERGE COMES FIRST, and that is the half this returned string exists to carry.
    Ansible renders from the working tree, so a playbook run before the merge copies the
    PRE-merge files and reports `changed=0` — a clean, idempotent-looking recap over the old
    code. This role's own CLAUDE.md has documented that as a trap since 2026-07; the callers
    went on printing the reverse order anyway, and on 2026-09-01 an operator following the
    printed order shipped the previous `deploy_logic.py` and only caught it by grepping
    /opt/gitops-deploy afterwards. Ordering the pair here rather than at each call site is
    what stops the four of them drifting apart again.
    """
    if key_readers_failed:
        return (
            "The next tick retries the key-reader lookup, so there is nothing to run yet, "
            "and merging past the changed key by hand would drop it for good. "
            "To run the lookup now, from the repo checkout: "
            f"`uv run --frozen python scripts/deploy_tools/secret_setup_readers.py HEAD origin/{branch}`"
        )
    cmds: list[str] = []
    if broad_deploy:
        cmds.append(f"`{PLAYBOOK_CMD} ansible/deploy.yml`")
    if broad_setup:
        cmds.extend(_setup_commands(setup_roles, narrow_tags, playbooks))
    return f"`git merge --ff-only origin/{branch}` FIRST, then " + " and ".join(cmds)


def manual_plane_remediation(
    setup_roles: set[str],
    narrow_tags: dict[str, frozenset[str]] | None = None,
    hosts: dict[str, tuple[str, ...]] | None = None,
) -> str:
    """The commands that clear a `manual_plane` marker: apply each role, then clear its line.

    No `git merge --ff-only` preamble, which is the one way this differs from
    `broad_remediation`. The tick recording this marker has already fast-forwarded — that is
    the whole change the marker exists to make safe — so prescribing the merge again would
    describe a tree the operator is not in.

    The clear command is the reverse of the write, and it is part of the remediation rather
    than a note beside it: a role applied by hand with its line left behind pages GitOps
    Deploy — Status six hours later over work that is already live.

    It names `--applied` whenever a narrowed apply was printed (#2349). A bare clear drops the
    role's whole line, and a second range can widen the row between this text being printed
    and the operator running it — so the bare form would clear a tag nobody applied. A role
    set prints one clear per role; `manual_plane_clear_for` says why.

    `hosts` maps a role tag to the `hosts` key of its ledger line: the hosts the tick's own run
    did not reach (#6510).
    """
    return (
        " and ".join(_setup_commands(setup_roles, narrow_tags, hosts=hosts))
        + f", then `{manual_plane_clear_for(setup_roles, narrow_tags)}`"
    )


def _narrowed_tags(role: str, narrow_tags: dict[str, frozenset[str]]) -> frozenset[str]:
    """The narrow tags recorded for `role`, by either key the marker can hold it under."""
    role_tag = setup_role_tag(role)
    return narrow_tags.get(role_tag) or narrow_tags.get(role) or frozenset()


def manual_plane_clear_for(
    setup_roles: set[str], narrow_tags: dict[str, frozenset[str]] | None = None
) -> str:
    """The clear command to print after applying `setup_roles` by hand.

    One clear per role, each naming its own tags where the apply beside it was narrowed,
    chained with `&&` so the text stays one pasteable command. A shared `<role> --applied
    <tags>` placeholder is what this printed before, and it stranded a line: substituted with
    `common`, whose row is always empty, `--applied` keeps the line by design, because an
    empty row means the whole role is pending. Naming each role also leaves no `<` for a
    shell to read as a redirect.
    """
    narrow_tags = narrow_tags or {}
    roles = sorted(setup_roles or ())
    if not roles:
        return owed_clear_cmd(OWED_MANUAL_PLANE, "<role>")
    return " && ".join(
        owed_clear_cmd(
            OWED_MANUAL_PLANE,
            setup_role_tag(role),
            _narrowed_tags(role, narrow_tags),
        )
        for role in roles
    )


def _setup_commands(
    setup_roles: set[str] | None,
    narrow_tags: dict[str, frozenset[str]] | None = None,
    playbooks: Iterable[str] = (),
    hosts: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    """One command per setup role, or the generic placeholder when no roles are known.

    Args:
        setup_roles: the roles to name a command for.
        narrow_tags: role tag -> the narrower tags that role's own change needs, from the
            `manual_plane` ledger class. A role absent from it, or present with an empty set,
            gets the whole-role tag.
        playbooks: changed bring-up playbooks. One that `setup_role_playbook` routes a role
            to is named by that role's command, so `k3s-bringup.yml` prints
            `--tags k3s`. Any other is named as a whole run:
            `bootstrap.yml` and `initial_setup.yml` have no single role a playbook-level edit
            maps to.
        hosts: role tag -> the hosts its command runs against, where those are not the
            host the routing gates it onto. A role absent from it takes the routing's host.
    """
    roles = set(setup_roles or ())
    cmds = []
    for playbook in sorted(playbooks):
        owners = roles_outside_initial_setup_in(playbook)
        if owners:
            roles |= owners
        else:
            cmds.append(f"`{PLAYBOOK_CMD} {playbook}`")
    if not roles and not cmds:
        return [f"`{PLAYBOOK_CMD} ansible/initial_setup.yml --tags <role>`"]
    narrow_tags = narrow_tags or {}
    for role in sorted(roles):
        if not is_routed(role):
            cmds.append(
                f"`{role}` could not be routed from the playbooks (the deployer's journal says "
                "why) — apply it with the playbook and `--tags` value that include it"
            )
            continue
        playbook = setup_role_playbook(role)
        if playbook is None:
            cmds.append(_consumers_command(role))
            continue
        cmds.append(_role_command(role, playbook, narrow_tags, hosts or {}))
    return cmds


def _role_command(
    role: str,
    playbook: str,
    narrow_tags: dict[str, frozenset[str]],
    hosts: dict[str, tuple[str, ...]],
) -> str:
    """The command that applies a routed setup role."""
    role_tag = setup_role_tag(role)
    narrowed = _narrowed_tags(role, narrow_tags)
    selected = narrowed or {role_tag}
    gated = setup_role_host(role)
    targets = hosts.get(role_tag) or ((gated,) if gated else ())
    apply = f"{PLAYBOOK_CMD} {playbook} --tags {','.join(sorted(selected))}"
    return f"`{on_hosts(apply, targets)}`"


def _consumers_command(role: str) -> str:
    """The commands for a role no playbook includes: one per role that renders its files.

    `common` is that role. Its consumers come from `deploy_cross_role.SETUP_FILES_ROUTED_TO_OWNER`,
    and each consumer's playbook, tag and host come from the routing (#4316). Before that the
    text named `k3s-bringup.yml`, `optimize_pi` and daniel-pi as literals, which went stale
    the moment a consumer moved host or a third role started rendering the file.

    Read qualified at call time, because `deploy_cross_role.use_tables` rebinds the table to
    origin's copy. A consumer the routing does not place keeps its name and says so.
    """
    consumers = deploy_cross_role.owner_routed_consumers(role)
    cmds = []
    for consumer in sorted(consumers):
        playbook = setup_role_playbook(consumer) if is_routed(consumer) else None
        if playbook is None:
            cmds.append(f"`{consumer}` (not routed to a playbook)")
        else:
            cmds.append(_role_command(consumer, playbook, {}, {}))
    each = (
        f"apply each consumer ({', '.join(cmds)})"
        if cmds
        else "apply each role that renders its files"
    )
    return f"`{role}` is read by other roles and applied by no playbook of its own — {each}"
