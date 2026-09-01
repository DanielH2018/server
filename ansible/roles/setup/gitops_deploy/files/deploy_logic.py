# ansible/roles/setup/gitops_deploy/files/deploy_logic.py
"""Pure decision logic for the GitOps deployer (no I/O — unit-tested).

`services_from_changed_paths` maps a git-diff file list to the set of active
container services to redeploy, or flags a "broad" change (shared template /
inventory) that the deployer must defer to a manual full deploy.

`next_action` decides what a poll tick should do given the local/origin HEADs
and any recorded known-bad (hold) SHA.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace

# A bind-mounted file under an active container role's templates/ or files/ dir — the
# docker-compose.yml.j2 OR any config template / files/ asset (e.g. prometheus.yml.j2,
# authelia configuration.yml.j2, monitor-bridge/files/check.py). A change here only reaches the
# container on its next deploy, so it maps to a scoped, health-gated redeploy — closing the GitOps
# loop instead of a silent ff-merge. tasks/ and the role CLAUDE.md are deliberately NOT matched
# (structural / docs — deploy those manually). The negative lookahead excludes archive/<svc>/...
_ACTIVE_CONFIG = re.compile(
    r"^ansible/roles/containers/(?!archive/)([^/]+)/(?:templates|files)/"
)
# A change under an active container role's tasks/ dir. tasks/ is deliberately NOT auto-deployed
# (structural — deploy manually), but unlike a CLAUDE.md/doc edit it DOES change what a deploy would
# do, so a tasks-only push must be flagged (defer-and-alert), not silently ff-merged and left
# unapplied with no signal — the same asymmetry the secrets / requirements.yml paths already close.
# Same archive/ exclusion; common/tasks is caught earlier by the _BROAD_PREFIXES check.
_ACTIVE_TASKS = re.compile(r"^ansible/roles/containers/(?!archive/)([^/]+)/tasks/")
# A change under an active container role's meta/ dir (meta/deps.yml). meta/ is NOT auto-deployed
# (structural, like tasks/), but unlike a doc edit it DOES change what a deploy does:
# `ansible/filter_plugins/toposort.py` reads meta/deps.yml to build the cross-service deploy ORDER
# and the dep CLOSURE a scoped `--tags` deploy expands. So a meta-only push must be flagged
# (defer-and-alert), not silently ff-merged as an invisible graph change — the same asymmetry the
# tasks / secrets / requirements paths already close. (The toposort LOGIC in filter_plugins/ is
# already _BROAD_PREFIXES; this is its DATA.) Same archive/ exclusion; common/meta is caught earlier
# by the _BROAD_PREFIXES check.
_ACTIVE_META = re.compile(r"^ansible/roles/containers/(?!archive/)([^/]+)/meta/")
# Catch-all for ANY other non-doc file under an active container role — `defaults/`, `vars/`,
# `handlers/`, or a future dir. Like tasks/ these change what a deploy of that service does but
# aren't auto-deployed, so a change here must defer-and-alert (via the tasks channel) rather than
# fall through to the silent docs-only ff-merge. Checked LAST, so templates/files (deploy), tasks/,
# and meta/ have already claimed their paths; only the structural remainder reaches it. CLAUDE.md /
# *.md are docs and keep the silent path (the caller excludes them). Same archive/ exclusion.
_ACTIVE_ROLE = re.compile(r"^ansible/roles/containers/(?!archive/)([^/]+)/")
# A change under a k8s-platform role's dir (ansible/roles/k8s/<role>/...). This deployer only ever
# auto-deploys DOCKER-platform services (deploy(cs.services) runs the same --tags path _ACTIVE_CONFIG
# feeds), so unlike _ACTIVE_TASKS/_ACTIVE_META there is no "rode the scoped redeploy" case to
# subtract — a k8s role change is NEVER applied by this pipeline and must always defer-and-alert.
# Before this, every path under ansible/roles/k8s/** matched NONE of the regexes above (they're all
# containers/-scoped) and fell through to services_from_changed_paths returning an EMPTY ChangeSet,
# which main()'s `if not cs.services:` branch takes as a plain docs-only ff-merge — silent, on EVERY
# host with has_gitops (daniel-box, all 47 services platform: k8s). Matches the WHOLE role dir (not
# split into templates/tasks/meta like containers/) since a k8s role has no separate auto-deploy path
# for any of its subdirs to be scoped against — the alert just needs to name the role. *.md (role
# CLAUDE.md) stays a silent ff-merge, same as the containers/ catch-all.
_ACTIVE_K8S = re.compile(r"^ansible/roles/k8s/([^/]+)/")

# Build roles that render no workload of their own, mapped to the roles that run what they
# build. Deploying the key WITHOUT the value builds a new image that nothing rolls onto,
# and reports green doing it.
#
# WHY A COUPLING EXISTS AT ALL. image-builder appends a rebuilt image to `k8s_rebuilt_images`,
# which roles/k8s/manifests turns into `manifests_image_changed` and rolls on. That fact is
# PLAY-SCOPED, so the build and the rollout must happen in one ansible-playbook run; two runs
# lose it and leave the old pods up. `roles/k8s/n8n/defaults/main.yml` records the incident
# (2026-08-08 `@n8n/di`) and prescribes `--tags "n8n-images,n8n"`. That prescription was prose,
# so every path→tag derivation missed it: a Renovate Dockerfile bump derives `n8n-images` alone.
#
# ONE ENTRY, NOT A MECHANISM. Six roles under roles/k8s/ include image-builder, and five of
# them (code-server, homelab-mcp, ical-proxy, nut, pi-peer-backup) render their own workload
# manifest, so a single tag already covers build and roll and the fact never crosses a role
# boundary. n8n-images is the only split one. test_build_roll_couplings.py re-derives that
# population from the role sources, so a second split build role fails the test rather than
# silently inheriting the bug.
#
# ONE-DIRECTIONAL. Editing roles/k8s/n8n's manifests needs no rebuild — a manifest change rolls
# on its own — so this must not be read as a symmetric pair.
_BUILD_ROLL_COUPLINGS = {"n8n-images": ("n8n",)}


def expand_build_couplings(tags):
    """`tags` plus the workload roles any build role among them requires.

    Called by the TAG derivations -- `deploy_tags.py changed` and land_tags.py -- and
    deliberately NOT by `services_from_changed_paths`, whose `cs.k8s` also feeds
    `split_k8s_auto_deploy`. A coupled role is added with no changed path of its own, so
    `image_only()` would diff an untouched defaults/main.yml and could read the empty result
    as vacuously image-only, promoting a role nothing asked for. n8n is denylisted today, so
    that is a latent hazard rather than a live one -- which is exactly the kind that should
    not be created. Widening a hand-scoped deploy is safe: the added role is one a correct
    deploy would have included anyway, and deploying a workload whose image did not move
    rolls nothing.
    """
    widened = set(tags)
    for build_role, needs in _BUILD_ROLL_COUPLINGS.items():
        if build_role in widened:
            widened.update(needs)
    return widened


# A `container_name:` line in a rendered docker-compose.yml.
_CONTAINER_NAME = re.compile(r'^\s*container_name:\s*["\']?([^\s"\']+)["\']?\s*$')
# Changes whose blast radius we don't try to scope automatically. Split by which manual playbook
# actually applies them, so the defer-and-alert can name the RIGHT one (2026-07-16 review M1):
# `deploy.yml` is a pure containers_list loop, so a setup-plane change deployed via deploy.yml is a
# silent no-op — it must be applied with `initial_setup.yml` instead.
_BROAD_DEPLOY_PREFIXES = (
    "ansible/templates/",  # shared macros (traefik/networks/resources/...)
    "ansible/inventory/",  # host_vars / group_vars
    "ansible/roles/containers/common/",  # shared deploy path
    "ansible/deploy.yml",
    # The task files deploy.yml imports. deploy.yml itself was already broad, but its three
    # sibling task dirs matched nothing: every _ACTIVE_* regex is anchored to ansible/roles/, so a
    # change to `pre_tasks/load_secrets.yml`, `tasks/k8s_batch.yml` (the k8s rollout-batch path) or
    # `post_tasks/k8s_stabilise_gate.yml` (the post-deploy stabilisation gate) returned a fully
    # EMPTY ChangeSet. main() has no catch-all branch — `if not cs.services:` ff-merges
    # unconditionally and both alert_deferred/alert_secrets_deferred no-op on empty fields — so
    # empty-because-unclassified was bit-for-bit indistinguishable from empty-because-docs: a
    # silent ff-merge, no alert, no deploy, on files that change what EVERY deploy does.
    # Deploy-plane rather than setup-plane because deploy.yml is the playbook this deployer runs;
    # `pre_tasks/load_secrets.yml` is also imported by initial_setup.yml, k3s-bringup.yml and
    # preflight.yml, but those only ever run by hand, so `ansible/deploy.yml` is the remediation
    # that actually applies the change on this host. Proportionate: these three dirs changed in 7
    # commits in the repo's whole history, so defer-and-alert here will rarely fire.
    "ansible/pre_tasks/",
    "ansible/tasks/",
    "ansible/post_tasks/",
    "ansible/filter_plugins/",  # toposort
    # ansible.cfg is a repo-root file read fresh by every ansible-playbook the deployer runs
    # (WorkingDirectory is the repo root, so ./ansible.cfg applies) but maps to no service — it sets
    # inventory/roles_path/collections_path/fact-caching, so a bad value mis-attributes a later
    # unrelated deploy's failure (2026-07-15 review M1). It changes rarely and operator-driven, so
    # broad (defer-and-alert) fits. pyproject.toml + uv.lock are deliberately NOT broad: they churn on
    # a predictable schedule (renovate.json lockFileMaintenance, daily + every dep-pin bump re-resolves
    # uv.lock), and the broad path never ff-merges — it parks local behind origin, and since broad is
    # checked before services, every later image bump (incl. CVE automerges) then piles up unapplied
    # behind the stuck lockfile until a manual full deploy (2026-07-15 review H1). A bad lockfile is
    # already caught pre-merge by CI `uv lock --check` and at deploy by the health-gate rollback, so
    # letting them take the silent ff-merge path (pre-2026-07-15 behavior) is the safer trade.
    "ansible.cfg",
)
# Broad changes applied by initial_setup.yml, NOT deploy.yml — deploy.yml renders NOTHING for these,
# so the defer-alert must point the operator at `initial_setup.yml --tags <role>`. Naming deploy.yml
# here is a no-op that leaves the change unapplied while a plain `git merge --ff-only` clears the
# divergence — worst case a fix to gitops_deploy.py itself ff-merges and the host keeps running the
# OLD code forever, with last_run still updating (old code writes it) so no monitor catches it.
_BROAD_SETUP_PREFIXES = (
    # Galaxy collections: installed by sops_setup — `initial_setup.yml --tags collections`.
    "ansible/requirements.yml",
    # Setup roles (gitops_deploy itself, renovate_notify, sops_setup, …): `--tags <role>`.
    "ansible/roles/setup/",
    # The bring-up playbooks — they only run by hand.
    "ansible/initial_setup.yml",
    "ansible/bootstrap.yml",
    "ansible/k3s-bringup.yml",
)
# Broad paths the deployer must NEVER apply itself, even though every other broad path now
# fast-forwards and applies.
#
# The bring-up playbooks are hand-run by construction (see broad_remediation), and
# initial_setup.yml unqualified is a whole-host reprovision rather than a scoped apply.
#
# These keep the OLD behaviour in full: defer, alert, and do not fast-forward. Staying
# parked is what keeps `behind_since` set, and that marker is the only durable signal that
# an unapplied plane exists.
#
# DECIDED: roles/setup/gitops_deploy/ — the deployer's own role — is NOT here, and applies
# itself like any other setup role. It sat here until 2026-09-01 on the claim that applying
# it "runs a playbook whose handler restarts the unit executing the tick", so the run would be
# SIGTERMed partway. The handler does not restart anything: `Run gitops-deploy once` is
# `ansible.builtin.systemd: state: started`, and Ansible's systemd module treats an
# `activating` unit as already running (`is_running_service` accepts `active` and
# `activating`), so from inside a tick it is a no-op. The only other handler is a
# daemon-reload, which a running oneshot unit survives. What the park DID do, three times on
# 2026-09-01 alone (#707, #712, #714): stop every other session's landing until an
# operator hand-ran `initial_setup.yml --tags gitops_deploy` in the primary checkout and
# ff-merged, because `deploy.sh` refuses a tree behind origin and the tick would not move it.
# A self-apply that fails takes the broad-apply failure path — hold_sha, hold_plane, alert —
# which is the same containment every other setup role gets, and the code it installs has
# passed master CI, which is the same gate every other role gets. The unit's own state
# (`config.env`, `/opt/gitops-deploy/*.py`) is read at the START of a tick and a mid-tick
# overwrite reaches only the next one, which is the tick that should run the new code anyway.
_BROAD_MANUAL_PREFIXES = (
    "ansible/bootstrap.yml",
    "ansible/k3s-bringup.yml",
    "ansible/initial_setup.yml",
)
# The SOPS-encrypted secrets file. A change here maps to no service template, but the new
# value only reaches a container on its next deploy — so a secrets-ONLY push must NOT be
# silently fast-forwarded; the deployer defers-and-alerts (see gitops_deploy.py). NOT in
# _BROAD_PREFIXES on purpose: the /add-secret flow ships secrets.yml WITH the consuming
# template, and that should stay a scoped single-service deploy, not a manual full deploy.
_SECRETS_FILE = "ansible/vars/secrets.yml"


def _is_test_only_path(path: str) -> bool:
    """Whether a changed path is test-suite material that no role ever ships to a host.

    Four shapes, and the first two are why this is a path check rather than a name check.
    `ansible/tests/` holds the repo-wide guards plus `_helpers.py`, which matches no name
    pattern at all. A role-local `tests/` directory (home-assistant's macro tests) is the same
    case. The other two are name-shaped: a `test_*.py` beside the module it covers, the layout
    every `roles/*/*/files/` suite uses, and the `conftest.py` that sits with it —
    monitor-bridge has one, and its ship list already excludes it by name.

    The invariant this rests on: nothing under `ansible/` copies a test file to a host.
    `ansible/tests/test_no_role_ships_a_test_file.py` is the tree-wide guard, so a role that
    starts shipping one fails there rather than silently widening this predicate into a hole.
    """
    if path.startswith("ansible/tests/"):
        return True
    parts = path.split("/")
    if "tests" in parts[:-1]:
        return True
    name = parts[-1]
    return name == "conftest.py" or (name.startswith("test_") and name.endswith(".py"))


@dataclass
class ChangeSet:
    services: set[str] = field(default_factory=set)
    broad: bool = False
    # Which manual playbook a broad change needs — deploy.yml's plane (shared templates/inventory/
    # common) vs initial_setup.yml's (roles/setup/, requirements.yml, bring-up playbooks). `broad`
    # stays the OR so the existing defer branch is unchanged; these drive the alert's remediation
    # command so a setup-plane change isn't sent to deploy.yml (a no-op). A push can set both.
    broad_deploy: bool = False
    broad_setup: bool = False
    # The `roles/setup/<name>` dirs this push touched, so the remediation can name the
    # playbook that actually includes each one rather than assuming initial_setup.yml.
    setup_roles: set[str] = field(default_factory=set)
    # A broad path the deployer must not apply itself (_BROAD_MANUAL_PREFIXES). ORed across
    # the push: ONE manual path makes the whole tick manual, because a half-applied broad
    # change is exactly the state the defer-and-alert arm exists to prevent.
    broad_manual: bool = False
    secrets: bool = False
    # `tasks` is the defer-and-alert channel for a service's structural, not-auto-deployed dirs:
    # tasks/ plus the _ACTIVE_ROLE catch-all (defaults/, vars/, handlers/, …). The alert names all
    # of them, so the field keeps its name for continuity even though it's no longer tasks/-only.
    tasks: set[str] = field(default_factory=set)
    meta: set[str] = field(default_factory=set)
    # k8s-platform role(s) that changed (ansible/roles/k8s/<role>/...). Distinct from `tasks`/`meta`:
    # this deployer has no mechanism that EVER applies a k8s role change (deploy(cs.services) only
    # ever tags Docker-platform roles matched by _ACTIVE_CONFIG), so it always defer-and-alerts —
    # there's no "rode a scoped redeploy of the same service" case to subtract deployed against.
    k8s: set[str] = field(default_factory=set)
    # k8s service(s) whose change is an image-pin bump ELIGIBLE for auto-deploy, split out of
    # `k8s` by split_k8s_auto_deploy. `k8s` keeps its "defer-and-alert, never applied" meaning,
    # so every existing consumer of that field is unchanged and this stays inert until a service
    # actually qualifies.
    k8s_deploy: set[str] = field(default_factory=set)
    # k8s roles that import a changed `files/*.py` owned by another role — see
    # shared_module_consumers. Kept separate from `k8s` so it stays inert for every consumer
    # that reads `k8s` directly; only k8s_remediation folds it in, and only after
    # intersecting with what this host declares.
    k8s_consumers: set[str] = field(default_factory=set)


def shared_module_consumers(paths, repo_root) -> set[str]:
    """k8s roles that import a changed `files/*.py` module owned by a DIFFERENT role.

    `_ACTIVE_K8S` maps a path to the role whose directory it sits in, which is right for a
    manifest and wrong for a shared library. `bridge_common.py` lives under monitor-bridge and
    is imported by autofix-bridge too, so the #407 five-module split made an edit there emit
    `--tags monitor-bridge` alone -- autofix-bridge's ConfigMap kept the old copy, and nothing
    reported it (2026-08-25 review M-2).

    Derived by reading the imports rather than listing the pair, because a hardcoded pair is
    the same guard-scope mistake one level up: it would go stale the first time a third role
    imported the module. Returns only the EXTRA roles; the owning role is already in `cs.k8s`.
    """
    from pathlib import Path

    k8s = Path(repo_root) / "ansible" / "roles" / "k8s"
    changed_modules = {
        Path(p).stem
        for p in paths
        if re.match(r"^ansible/roles/k8s/[^/]+/files/[^/]+\.py$", p)
    }
    if not changed_modules or not k8s.is_dir():
        return set()

    owners = {
        m.group(1)
        for p in paths
        if (m := re.match(r"^ansible/roles/k8s/([^/]+)/files/[^/]+\.py$", p))
    }
    consumers: set[str] = set()
    for role in k8s.iterdir():
        if not (role / "files").is_dir() or role.name in owners:
            continue
        for src in (role / "files").glob("*.py"):
            if src.name.startswith("test_"):
                continue
            try:
                text = src.read_text(errors="ignore")
            except OSError:
                continue
            if any(
                re.search(r"^\s*(?:import|from)\s+%s\b" % re.escape(mod), text, re.M)
                for mod in changed_modules
            ):
                consumers.add(role.name)
                break
    return consumers


def services_from_changed_paths(paths: list[str]) -> ChangeSet:
    cs = ChangeSet()
    for p in paths:
        # Test-suite files reach no host, so they must not drive a deploy decision. Checked
        # FIRST, before every prefix below, because the prefixes match on path alone: a test
        # under roles/setup/gitops_deploy/ read as broad_manual and parked the whole tick,
        # and one under roles/k8s/<svc>/files/ read as a k8s change and defer-alerted. PR #707
        # was three test files and did both, costing two hand-run commands to clear (2026-09-01).
        #
        # An empty ChangeSet is the right outcome, not a hole: gitops_deploy.py takes the
        # `if not cs.services` branch and fast-forwards, exactly as it does for a docs-only push.
        if _is_test_only_path(p):
            continue
        if p == _SECRETS_FILE:
            cs.secrets = True
            continue
        # Tested FIRST, and it does not `continue` past the plane flags below: the manual
        # set overlaps _BROAD_SETUP_PREFIXES (the bring-up playbooks sit in both), and the
        # alert still needs to name the right remediation playbook.
        if any(p.startswith(prefix) for prefix in _BROAD_MANUAL_PREFIXES):
            cs.broad = True
            cs.broad_manual = True
            cs.broad_setup = True
            _note_setup_role(cs, p)
            continue
        if any(p.startswith(prefix) for prefix in _BROAD_SETUP_PREFIXES):
            cs.broad = True
            cs.broad_setup = True
            _note_setup_role(cs, p)
            continue
        if any(p.startswith(prefix) for prefix in _BROAD_DEPLOY_PREFIXES):
            cs.broad = True
            cs.broad_deploy = True
            continue
        m = _ACTIVE_CONFIG.match(p)
        if m:
            cs.services.add(m.group(1))
            continue
        t = _ACTIVE_TASKS.match(p)
        if t:
            cs.tasks.add(t.group(1))
            continue
        mt = _ACTIVE_META.match(p)
        if mt:
            cs.meta.add(mt.group(1))
            continue
        k = _ACTIVE_K8S.match(p)
        if k and not p.endswith(".md"):
            cs.k8s.add(k.group(1))
            continue
        # Catch-all: any other non-doc file under an active container role (defaults/, vars/,
        # handlers/, …). Not auto-deployed but it changes what a deploy does — defer-and-alert
        # via the tasks channel instead of a silent ff-merge. *.md (CLAUDE.md, README) are docs
        # and keep the silent path.
        r = _ACTIVE_ROLE.match(p)
        if r and not p.endswith(".md"):
            cs.tasks.add(r.group(1))
    return cs


_SETUP_ROLE = re.compile(r"^ansible/roles/setup/([^/]+)/")


def _note_setup_role(cs: "ChangeSet", path: str) -> None:
    """Record which setup role a broad path belongs to, if any. A bring-up playbook has none."""
    m = _SETUP_ROLE.match(path)
    if m:
        cs.setup_roles.add(m.group(1))


# Setup roles `ansible/initial_setup.yml` does NOT include, mapped to the playbook that does.
# `None` means no playbook includes the role at all.
#
# THE BUG THIS EXISTS TO KILL. Both functions below used to assume every directory under
# `roles/setup/` was a tag in initial_setup.yml. It is not, and the failure is silent in the
# worst way: `--tags` matching nothing makes Ansible exit 0, so the deployer ff-merges, runs a
# playbook that does nothing, and records a successful apply. `setup_tags_for`'s own docstring
# names that outcome as the reason it returns an empty set rather than a guess — it was
# guessing anyway.
#
# Occurred 2026-09-01 with PR #702, a `roles/setup/k3s/` change installing a host DNS
# forwarder. The role appears only in `k3s-bringup.yml`, so the tick's `initial_setup.yml
# --tags k3s` matched no task; the forwarder had to be installed by hand afterwards, and
# nothing in the pipeline said it had not been.
#
# `common` is the sharper shape: no playbook includes it, and it is not dead code — two roles
# read its templates by absolute path, on two different hosts. A change to its shared
# resolv.conf.j2 has to be applied twice, via k3s-bringup.yml on daniel-box and via
# initial_setup.yml on daniel-pi, and neither is what the old code named.
_SETUP_ROLES_OUTSIDE_INITIAL_SETUP: dict[str, str | None] = {
    "k3s": "ansible/k3s-bringup.yml",
    "common": None,
}
# Setup roles whose `--tags` value is not their directory name. Same silent-exit-0 failure:
# `--tags chezmoi_setup` matches nothing, because the playbook tags that role `chezmoi`.
_SETUP_ROLE_TAG_OVERRIDES = {"chezmoi_setup": "chezmoi"}


def setup_role_playbook(role: str) -> str | None:
    """The playbook that applies a setup role, or None when no playbook includes it."""
    if role in _SETUP_ROLES_OUTSIDE_INITIAL_SETUP:
        return _SETUP_ROLES_OUTSIDE_INITIAL_SETUP[role]
    return "ansible/initial_setup.yml"


def setup_role_tag(role: str) -> str:
    """The `--tags` value that actually selects a setup role, which is not always its name."""
    return _SETUP_ROLE_TAG_OVERRIDES.get(role, role)


def setup_tags_for(paths) -> set[str]:
    """The `initial_setup.yml --tags` values a set of setup-plane paths needs.

    broad_remediation emits a literal `<role>` placeholder, which is fine for a human
    reading an alert and useless for a machine about to run the playbook. This derives the
    real tags, and the alert text uses it too so the two can never disagree.

    Returns an EMPTY set for anything it cannot resolve — a bring-up playbook, or any path
    in _BROAD_MANUAL_PREFIXES. Empty means "cannot be applied automatically", which the
    caller must treat as a deferral rather than as an unscoped run. Returning a wrong tag
    would be worse than returning none: `--tags` matching nothing makes Ansible exit 0, so
    the deployer would report a successful apply having changed nothing at all.
    """
    tags: set[str] = set()
    for p in paths:
        if any(p.startswith(prefix) for prefix in _BROAD_MANUAL_PREFIXES):
            continue
        if p == "ansible/requirements.yml":
            # Installed by sops_setup — see the comment on _BROAD_SETUP_PREFIXES.
            tags.add("collections")
            continue
        m = _SETUP_ROLE.match(p)
        if m:
            role = m.group(1)
            # A role initial_setup.yml does not include cannot be applied by the automatic
            # arm at all, so it must return NOTHING and route to defer-and-alert. Returning
            # the role name here is the guess this function's docstring forbids: the run
            # would exit 0 having matched no task.
            if setup_role_playbook(role) != "ansible/initial_setup.yml":
                continue
            tags.add(setup_role_tag(role))
    return tags


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
    unrelated feature. Pinned by test_deploy_logic.py::
    test_the_budget_predicate_tracks_the_units_real_timeout, which carries the re-derivation.
    """
    return flock_s + forward_s + rollback_s + BROAD_BUDGET_MARGIN_S <= timeout_s


def broad_remediation(
    broad_deploy: bool, broad_setup: bool, setup_roles: set[str] | None = None
) -> str:
    """The manual command(s) a broad (defer-and-alert) change needs, by which plane it hit.

    deploy.yml runs only container roles, so a setup-plane change (roles/setup/, requirements.yml,
    the bring-up playbooks) needs `initial_setup.yml --tags <role>`; naming deploy.yml there is a
    silent no-op that leaves the change unapplied while a plain ff-merge clears the divergence —
    worst case a fix to gitops_deploy.py itself (2026-07-16 review M1). A push hitting both planes
    names both.

    `setup_roles` narrows the setup half from that `<role>` placeholder to the real command,
    and exists because the placeholder's *playbook* was wrong for some roles rather than merely
    vague — see `_SETUP_ROLES_OUTSIDE_INITIAL_SETUP`. Omitting it keeps the old generic text,
    which is what every caller with no path list still gets.
    """
    cmds: list[str] = []
    if broad_deploy:
        cmds.append("`ansible-playbook ansible/deploy.yml`")
    if broad_setup:
        cmds.extend(_setup_commands(setup_roles))
    return " and ".join(cmds)


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

    Eight roles are in that position today (manifests, rollout-drain, seed-volume,
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


# ── CI gate ───────────────────────────────────────────────────────────────────────────────────
# A GitHub check-run conclusion that counts as "this commit is good". `skipped` and `neutral` are
# passes on purpose: ci.yml's renovate-config job runs unconditionally but gates its real work on a
# per-PR step condition, so it legitimately reports a non-`success` completion.
_CI_PASS_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
# Deliberately NOT failures — these mean "no verdict for this SHA", not "this SHA is bad".
# ci.yml sets `concurrency: cancel-in-progress` keyed on github.ref, so two pushes to master in
# quick succession CANCEL the first run. Mapping `cancelled` to a failure would page on an
# ordinary back-to-back push; the tip's own run is the one that supplies the verdict.
_CI_NO_VERDICT_CONCLUSIONS = frozenset(
    {"cancelled", "stale", "skipped_by_concurrency", None}
)


def github_token(environ: dict[str, str], run: Callable) -> str | None:
    """A GitHub token for the check-runs gate, or None to query anonymously.

    `GH_TOKEN` / `GITHUB_TOKEN` in the environment win, then `gh auth token` — the gh CLI on
    daniel-box is logged in as the repo owner, and the deployer runs as that same user. The
    lookup is best-effort: a missing gh, an expired login, or a slow keyring all return None,
    and the caller queries anonymously exactly as it did before this existed.

    Why authenticate a read of a public repo. The anonymous limit is 60 requests/hour PER
    SOURCE IP, and every GitHub call from this host shares it: the tick's gate, `await_ci.py`
    polling every 20s for up to 900s during a landing (45 requests per run), renovate_notify,
    the ruleset-drift cron. Two `land.sh` runs in an hour exhaust it, after which the tick's
    gate reads `HTTP Error 403: rate limit exceeded` and defers as `CI not finished` — which
    is correct fail-closed behaviour and also a deploy outage nobody asked for. Measured
    2026-09-01: two landings and a manual tick, three 403 deferrals. Authenticated, the
    limit is 5000/hour per token.
    """
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = environ.get(name, "").strip()
        if value:
            return value
    try:
        proc = run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — absent binary, timeout, anything: anonymous is fine
        return None
    if proc.returncode != 0:
        return None
    token = (proc.stdout or "").strip()
    return token or None


def github_auth_headers(token: str | None) -> dict[str, str]:
    """The `Authorization` header for `token`, or nothing for an anonymous request."""
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def ci_verdict(check_runs: list[dict], required: frozenset[str] | set[str]) -> str:
    """Reduce GitHub's check-runs for ONE commit to `pass` / `pending` / `fail`.

    `required` is the set of check-run NAMES that must be green — the same strings GitHub reports
    as branch-protection contexts (`prek (lint + validate + tests + secrets)`). An empty set means
    the gate is disarmed, which returns `pass` so a host that hasn't re-templated config.env keeps
    behaving exactly as it does today.

    A name can carry SEVERAL runs (a re-run, or the same workflow triggered by both `push` and
    `pull_request`), so each name is reduced over all of its runs and the worst outcome wins: any
    outright failure makes the whole verdict `fail`, and a name that is missing, still running, or
    has only no-verdict conclusions holds the verdict at `pending`. Pending is the safe direction —
    the caller defers the tick and retries in 30 minutes.
    """
    if not required:
        return "pass"
    states: dict[str, set[str]] = {}
    for run in check_runs:
        name = run.get("name")
        if name not in required:
            continue
        if run.get("status") != "completed":
            state = "pending"
        elif run.get("conclusion") in _CI_PASS_CONCLUSIONS:
            state = "pass"
        elif run.get("conclusion") in _CI_NO_VERDICT_CONCLUSIONS:
            state = "pending"
        else:
            state = "fail"
        states.setdefault(name, set()).add(state)
    if any("fail" in s for s in states.values()):
        return "fail"
    # A name with no runs at all is a freshly-pushed SHA whose workflow hasn't registered yet.
    if any("pending" in states.get(name, {"pending"}) for name in required):
        return "pending"
    return "pass"


def next_action(
    local_head: str,
    origin_head: str,
    hold_sha: str | None,
    dirty: bool = False,
    origin_ahead: bool = True,
    ci: str = "pass",
) -> str:
    # A dirty working tree (operator mid-edit) is a healthy skip, not an outage,
    # and must never be deployed from — so it short-circuits every other outcome.
    if dirty:
        return "dirty"
    if origin_head == local_head:
        return "noop"
    if hold_sha is not None and origin_head == hold_sha:
        return "skip_hold"
    # The deployer is pull-based and only fast-forwards, so it must act ONLY when
    # origin is strictly ahead of local. `origin_ahead=False` means origin is an
    # ancestor of local (the operator committed locally but hasn't pushed) or the
    # two diverged — either way there is nothing to fast-forward. Deploying here
    # would diff local..origin (the *reverse* of the un-pushed commits) and
    # mis-fire a redeploy + false rollback, so treat it as a no-op.
    if not origin_ahead:
        return "noop"
    # CI gate. `origin` is the tip we would `--ff-only` onto, and the tip is the SHA whose
    # check-runs decide the tick. A tick can span local..origin — several commits — and the
    # intermediate ones are NOT individually gated; that is intentional, because the tip is the
    # tree the host actually ends up running. Both outcomes return WITHOUT ff-merging, so the host
    # stays parked on `local` and `behind_marker` records it — a persistently red master therefore
    # pages through the existing behind-origin watchdog rather than needing its own escalation.
    if ci == "fail":
        return "ci_failed"
    if ci == "pending":
        return "ci_pending"
    return "deploy"


def is_diverged(
    origin_head: str, local_head: str, origin_ahead: bool, local_ahead: bool
) -> bool:
    """True when origin and local have DIVERGED — they differ yet neither is an ancestor of the
    other, so the deployer can neither fast-forward (`origin_ahead`) nor is this the healthy
    committed-but-unpushed local state (`local_ahead`, which secret-rotate owns and which stays a
    plain noop). A diverged tree noops forever while origin's new commits — a Renovate/security bump
    — never deploy, and both GitOps monitors stay green (last_run keeps ticking, no hold). The
    deployer records this so GitOps Status surfaces it instead of camouflaging it as a healthy noop
    (2026-07-15 review L3)."""
    return origin_head != local_head and not origin_ahead and not local_ahead


def behind_marker(
    behind: bool, origin_head: str, existing: str | None, now: float
) -> str | None:
    """Next value of the `behind_since` marker: "<origin_sha> <unix_ts_first_seen>", or None to
    clear it.

    `behind` means origin is strictly ahead of local at the END of a tick — we saw new commits and
    did not converge on them. Unlike is_diverged this is not a broken state on its own: a routine
    push is behind for exactly one tick, and the dirty-tree path is behind for as long as the
    operator is editing (deliberately healthy). What is NOT healthy is staying behind, which is why
    the marker carries a first-seen timestamp and monitor-bridge pages on AGE, not on presence.

    The timestamp survives across ticks and resets only when we converge — deliberately not per-SHA.
    Re-stamping on each new origin SHA would let a steady trickle of pushes to a permanently-stuck
    host restart the clock forever, which is the exact failure this is meant to catch. The SHA is
    refreshed each tick so the alert names where origin actually is.
    """
    if not behind:
        return None
    if existing:
        parts = existing.split()
        if len(parts) == 2:
            return f"{origin_head} {parts[1]}"
    return f"{origin_head} {now}"


def dirty_summary(porcelain: str, limit: int = 12) -> str:
    """The paths making the tree dirty, with their porcelain status codes, for one log line.

    Names the paths rather than reporting the state, because the state alone sends the reader
    to `git status` on a host they may not be on. `??` is the code worth seeing: it means an
    untracked file, and `git status --porcelain` counts those — so the tree can be dirty with
    nothing modified, which is the case that is genuinely surprising.

    Porcelain v1 is two status characters, a space, then the path, so the path starts at
    index 3. A rename arrives as `R  old -> new` and is left whole: both halves are the fact.
    """
    entries = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        code, path = line[:2].strip() or "??", line[3:].strip()
        entries.append(f"{code} {path}" if path else code)
    if not entries:
        return "(no entries — the tree changed under us)"
    if len(entries) > limit:
        extra = len(entries) - limit
        return ", ".join(entries[:limit]) + f", +{extra} more"
    return ", ".join(entries)


def dirty_alert_slot(now, morning_hour: int = 8, evening_hour: int = 20) -> str | None:
    """The dirty-alert time slot `now` falls in, or None before the morning slot.

    The day has two slots — morning (`morning_hour` <= h < `evening_hour`) and
    evening (h >= `evening_hour`) — so a persistently dirty tree pages at most
    twice per America/Chicago day (~08:00 and ~20:00 CT). Before `morning_hour`
    there is no slot, so an overnight-dirty tree stays quiet until the morning.
    Returned as `YYYY-MM-DD:am|pm` so the caller can store it as the throttle key
    and compare the next tick against it.
    """
    if now.hour < morning_hour:
        return None
    slot = "am" if now.hour < evening_hour else "pm"
    return f"{now.date().isoformat()}:{slot}"


def should_alert_dirty(
    now,
    last_alert_slot: str | None,
    morning_hour: int = 8,
    evening_hour: int = 20,
) -> bool:
    """Whether this tick should send the dirty-working-tree Discord alert.

    The deploy timer fires every 30 min, so an unthrottled dirty alert pages the
    webhook through every long edit session. This caps it to at most once per slot
    (see `dirty_alert_slot`) — once in the morning (~08:00 CT) and once at night
    (~20:00 CT) — and stays silent before the morning slot, so an overnight-dirty
    tree pages twice a day instead of all night.

    `now` is the current time already in the target timezone (America/Chicago);
    `last_alert_slot` is the slot key (`YYYY-MM-DD:am|pm`) we last alerted on, or
    None. The caller records `dirty_alert_slot(now, ...)` whenever this returns True.
    """
    slot = dirty_alert_slot(now, morning_hour, evening_hour)
    if slot is None:
        return False
    return last_alert_slot != slot


def container_names(compose_text: str) -> list[str]:
    """Every `container_name:` declared in a rendered docker-compose.yml, in order.

    The deployer health-gates these, not the role/service name: a single role
    often runs several containers and the Renovate-bumped image's container is
    usually NOT the role-named one (e.g. `cadvisor` lives in the `prometheus`
    role, `scrutiny-influxdb` in `scrutiny`).
    """
    out: list[str] = []
    for line in compose_text.splitlines():
        m = _CONTAINER_NAME.match(line)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def containers_to_gate(compose_text: str | None, service: str) -> list[str]:
    """Containers to health-gate for `service` after a deploy.

    `compose_text` is the service's rendered docker-compose.yml on THIS host, or
    None when that file doesn't exist — which means the service isn't deployed on
    this host (e.g. dozzle is daniel-pi-only, and the deployer doesn't run on the
    Pi at all — has_gitops: false there).
    A changed template for such a service renders nothing here, so we must gate
    nothing: returning [] makes the caller skip it instead of polling a phantom
    container until HEALTH_TIMEOUT_S and triggering a false rollback.

    A present compose that declares no `container_name` falls back to [service].
    """
    if compose_text is None:
        return []
    return container_names(compose_text) or [service]


def health_decision(
    health_status: str, running: bool, running_streak: int, settle_checks: int = 3
) -> tuple[str, int]:
    """Pure transition for ONE health poll of a just-deployed container.

    This is the pass-or-keep-waiting decision the deployer's poll loop (`health_ok`
    in gitops_deploy.py) makes on each sample, lifted out of the I/O so it can be
    unit-tested without Docker/sleep/wall-clock. Inputs:
      - health_status: docker `.State.Health.Status` — 'healthy' / 'starting' /
        'unhealthy', or '' for an image with NO HEALTHCHECK (also '' if the
        container is already gone).
      - running: docker `.State.Running` (only consulted in the no-healthcheck
        case; pass False otherwise).
      - running_streak: count of consecutive prior no-healthcheck 'running' samples.
    Returns (verdict, new_running_streak); verdict is 'healthy' (gate passes — stop
    polling) or 'wait' (keep polling until the deadline).

    The settle streak is the boot-then-crash guard: a no-healthcheck image must stay
    'running' across `settle_checks` consecutive polls before it counts as healthy,
    so a container that boots then crash-loops can't slip the gate the way a single
    'running' sample would.
    """
    if health_status == "healthy":
        return "healthy", running_streak
    if health_status == "":  # no healthcheck -> require sustained running
        new_streak = running_streak + 1 if running else 0
        if new_streak >= settle_checks:
            return "healthy", new_streak
        return "wait", new_streak
    # 'starting' / 'unhealthy' -> not yet; reset the streak and keep waiting.
    return "wait", 0


def health_settles(samples: list[tuple[str, bool]], settle_checks: int = 3) -> bool:
    """Fold `health_decision` over a sequence of (health_status, running) polls.

    True if the container would reach 'healthy' before the samples run out (the poll
    loop returns True and the deploy stands); False if it never settles within them
    (the loop hits HEALTH_TIMEOUT_S and the deployer rolls back to the prior HEAD).
    A pure mirror of `health_ok`'s loop with the I/O (docker inspect + sleep + the
    deadline) removed, so the streak/crash-loop logic is exercised in tests.
    """
    streak = 0
    for health_status, running in samples:
        verdict, streak = health_decision(health_status, running, streak, settle_checks)
        if verdict == "healthy":
            return True
    return False


def apply_send_result(
    pending: dict[str, str], key: str, content: str, delivered: bool
) -> dict[str, str]:
    """The pending-alert queue after attempting to send `content` under `key`.

    On a confirmed delivery the key is cleared; on a failure the content is (re)queued under it, so a
    transient webhook blip can't permanently drop a post-merge alert (the ff-merged secrets/tasks/meta
    paths never re-reach their alert code). Pure: the caller (`deliver` in gitops_deploy.py) does the
    discord() I/O and persists the result only when it differs from the input. Returns a NEW dict;
    `pending` is not mutated.
    """
    updated = dict(pending)
    if delivered:
        updated.pop(key, None)
    else:
        updated[key] = content
    return updated


# The pending-alert queue had no cap, no expiry and no timestamps. Nothing reads the file back
# except drain_pending(), so an entry only leaves on a confirmed 2xx — a webhook that stays broken
# (a revoked URL, a permanently wrong channel) grows it without bound on a 30-minute tick, and
# every tick then re-POSTs the whole backlog. 64 is the cap: alerts are Discord messages, so the
# file stays well under a megabyte, and a backlog deeper than 64 means the webhook has been broken
# for over a day — at which point the OLDEST alerts are the least worth keeping.
#
# Eviction is oldest-first with no timestamp needed: dicts preserve insertion order, and json.load
# preserves it on the way back in, so the queue's own order IS its age order.
PENDING_ALERTS_MAX = 64


def cap_pending(
    pending: dict[str, str], max_entries: int = PENDING_ALERTS_MAX
) -> tuple[dict[str, str], list[str]]:
    """The queue trimmed to `max_entries`, plus the keys dropped — oldest first.

    Returns the dropped keys rather than dropping them silently: an alert discarded without a
    trace is the failure this queue exists to prevent, one level up. Pure; the caller logs.
    """
    if max_entries <= 0 or len(pending) <= max_entries:
        return pending, []
    dropped = list(pending)[: len(pending) - max_entries]
    return {k: c for k, c in pending.items() if k not in set(dropped)}, dropped


def apply_drain_result(pending: dict[str, str], delivered: set[str]) -> dict[str, str]:
    """The queue after a drain pass in which the `delivered` keys were confirmed sent — every other
    entry is kept for the next tick. Pure; the caller (`drain_pending`) does the per-entry discord()
    I/O and persists only on a change.
    """
    return {k: c for k, c in pending.items() if k not in delivered}


def gate_services(services, health_fn, gate_deadline, now_fn) -> list[str]:
    """Health-gate `services` (sorted, deterministic) and return those that FAILED.

    Bounds the TOTAL wall-clock spent gating: once `now_fn()` reaches `gate_deadline`, it
    stops polling and marks the current service AND every still-ungated one as failed, so the
    gate + rollback (git reset + one redeploy) finishes inside the unit's TimeoutStartSec.
    Without this cap a multi-service batch with several containers each polling to
    HEALTH_TIMEOUT_S can overrun the timeout; systemd then SIGTERMs the deployer before the
    rollback + hold run and the bad commit is left live (next tick sees local==origin -> noop).

    `health_fn(service, gate_deadline)` returns True when healthy; it also receives the deadline
    so one slow container's own poll loop can't block past it. A service not deployed on this
    host is vacuously healthy (health_fn returns True — see containers_to_gate). `now_fn` is the
    injected clock (the deployer passes `time.time`; tests pass a fake) — keeps this module I/O-free.
    """
    failed: list[str] = []
    ordered = sorted(services)
    for i, service in enumerate(ordered):
        if now_fn() >= gate_deadline:
            failed.extend(ordered[i:])
            break
        if not health_fn(service, gate_deadline):
            failed.append(service)
    return failed


# One containers_list entry: the `- name:` line plus everything indented under it up to the next
# `- name:` at the same (2-space) indent, or EOF. Two-space list indent is the repo-wide inventory
# convention; matching on it (rather than YAML-parsing) keeps this module stdlib-only and immune to
# the Jinja expressions inventory values carry.
_DECLARED_ENTRY = re.compile(
    r"^  - name: (\S+)(.*?)(?=^  - name: |\Z)", re.MULTILINE | re.DOTALL
)
# `platform: <value>` at the sub-key indent (4 spaces) within one entry's block.
_ENTRY_PLATFORM = re.compile(r"^    platform:\s*(\S+)", re.MULTILINE)


def declared_services(hostvars_text: str) -> set[str]:
    """Docker-platform service names declared in a host's containers_list.

    `platform: k8s` entries (default `docker` when the key is absent — see
    `ansible/inventory/host_vars/_example.yml`) are deliberately excluded: this deployer's
    stale-compose watchdog (`stale_rendered_services`) diffs against `containers/<svc>/` dirs
    rendered by deploy.yml's DOCKER play only, so a platform: k8s entry counting as "declared"
    here would let a leftover rendered compose for a service that migrated to k8s (a real stale
    dir) hide behind it as phantom-declared, instead of being flagged."""
    out: set[str] = set()
    for m in _DECLARED_ENTRY.finditer(hostvars_text):
        name, block = m.group(1), m.group(2)
        pm = _ENTRY_PLATFORM.search(block)
        platform = pm.group(1) if pm else "docker"
        if platform == "docker":
            out.add(name)
    return out


def declared_k8s_services(hostvars_text: str) -> set[str]:
    """k8s-platform service names declared in a host's containers_list — the platform: k8s
    counterpart to declared_services(), used to catch a same-named Docker role that's actually
    k8s on THIS host (see reroute_k8s_services)."""
    out: set[str] = set()
    for m in _DECLARED_ENTRY.finditer(hostvars_text):
        name, block = m.group(1), m.group(2)
        pm = _ENTRY_PLATFORM.search(block)
        platform = pm.group(1) if pm else "docker"
        if platform == "k8s":
            out.add(name)
    return out


def reroute_k8s_services(cs: ChangeSet, k8s_services: set[str]) -> ChangeSet:
    """Move any `cs.services` entry that is platform: k8s on THIS host into `cs.k8s`.

    services_from_changed_paths maps ansible/roles/containers/<svc>/{templates,files}/ changes to
    <svc> by NAME ALONE, with no knowledge of which platform this host actually runs that service
    under (e.g. wg-easy is a Docker role used by daniel-pi, but platform: k8s on daniel-box).
    Deploying such a match with `--tags <svc>` resolves to deploy.yml's K8S play, not the Docker
    one _ACTIVE_CONFIG assumed — an idempotent no-op whose health gate is silently skipped
    (containers_for() finds no rendered docker-compose.yml for a k8s entry), instead of the
    defer-and-alert a k8s-platform change should get (same as ansible/roles/k8s/** changes)."""
    moved = cs.services & k8s_services
    if not moved:
        return cs
    return replace(cs, services=cs.services - moved, k8s=cs.k8s | moved)


# A changed line in a unified diff that assigns a container image var, e.g.
#   +speedtest_k8s_image: openspeedtest/latest:v2.0.5
# Anchored on the `_image:` var-name suffix so it matches the same population the
# renovate.json k8s-defaults customManager tracks. Leading whitespace is tolerated; a leading
# `#` is not — a commented-out pin is not an image assignment.
_DIFF_IMAGE_LINE = re.compile(r"^[+-][ \t]*[A-Za-z0-9_]+_image:[ \t]*\S")
# Unified-diff file headers. They begin with -/+ but are metadata, not content, so they must be
# skipped before classifying changed lines.
_DIFF_HEADER = ("--- ", "+++ ")
_K8S_DEFAULTS_PATH = "ansible/roles/k8s/{svc}/defaults/main.yml"


def is_image_only_diff(diff_text: str) -> bool:
    """True when every changed line in `diff_text` assigns an `*_image:` var.

    The diff-shape half of k8s auto-deploy eligibility. Gating on the service NAME alone is not
    enough: `_ACTIVE_K8S` matches the whole role dir, so a name-only gate would also auto-deploy
    configmap / tasks/ / template pushes — none of which carry a Renovate soak behind them.

    Fails closed: a diff with no changed lines (empty, unreadable, header-only) returns False, so
    an unexpected git-output shape defers rather than deploys.
    """
    seen_change = False
    for line in diff_text.splitlines():
        if line.startswith(_DIFF_HEADER):
            continue
        if not line.startswith(("+", "-")):
            continue
        seen_change = True
        if not _DIFF_IMAGE_LINE.match(line):
            return False
    return seen_change


# Roles under roles/k8s/ that deploy no service of their own — they are included by other roles
# and carry no defaults/main.yml, so they declare nothing. Mirrors SHARED_ROLES in
# ansible/filter_plugins/k8s_autodeploy.py; ansible/tests/test_denylist_parsers_agree.py asserts
# the two stay in step.
SHARED_K8S_ROLES = frozenset({"manifests", "rollout-drain"})

# A top-level `k8s_autodeploy:` assignment, with an optional trailing comment. Anchored at column
# zero deliberately: an indented key of the same name belongs to some other mapping and does not
# declare the role's stance. `[ \t]+` (not `*`) requires a space after the colon, so
# `k8s_autodeploy:true` — which is not a dict key YAML would resolve this way either — doesn't
# match and falls through to "no declaration found", i.e. denied.
_DECLARATION_RE = re.compile(
    r"^k8s_autodeploy:[ \t]+(?P<value>\S+)[ \t]*(?:#.*)?$", re.MULTILINE
)
# The spellings PyYAML resolves to boolean true. Everything else — including an unparseable or
# absent value — counts as denied, so this parser can never widen what may auto-deploy.
_TRUE_VALUES = frozenset(
    {"true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"}
)


def declared_denylist(sources: dict[str, str | None]) -> frozenset[str]:
    """Roles denied auto-deploy, read from each role's own `k8s_autodeploy` declaration.

    `sources` maps a role name to the text of its defaults/main.yml, or None when the role has
    no such file. Shared roles are skipped; every other role is denied unless EVERY line that
    looks like a top-level `k8s_autodeploy:` assignment reads as true, and at least one such
    line was found.

    That's unanimity, not "last match wins" — a role is permitted only if every candidate
    assignment agrees. YAML itself is last-key-wins for a genuine duplicate key, so unanimity is
    not what a real YAML parser would do; it's deliberately more conservative. This is a regex,
    not a YAML parser: it can't tell a real top-level `k8s_autodeploy:` key apart from the same
    text sitting inside a multi-line quoted scalar, so on adversarial or malformed input (a
    decoy `true` before the real `false`, a duplicate key) unanimity can only ever add denials
    it would otherwise have missed — never turn a role the last-wins reading would deny into one
    this reader calls permitted. On well-formed, single-declaration input the two rules agree.

    This exists ONLY to detect that /etc/gitops-deploy/config.env has gone stale against
    origin — it never decides eligibility. So it is deliberately biased: anything unclear reads
    as denied, which can make the comparison mismatch and disarm auto-deploy, but can never make
    a denied role look permitted. The authoritative derivation is the Ansible filter, which
    parses real YAML; ansible/tests/test_denylist_parsers_agree.py pins the two together.
    """
    denied = set()
    for role, text in sources.items():
        if role in SHARED_K8S_ROLES:
            continue
        if not text:
            denied.add(role)
            continue
        values = [m.group("value") for m in _DECLARATION_RE.finditer(text)]
        if not values or any(v not in _TRUE_VALUES for v in values):
            denied.add(role)
    return frozenset(denied)


# A top-level, single-line `k8s_autodeploy_snapshot_pvcs: [...]` list literal — every role that
# declares one today writes it this way (e.g. `[bazarr-config]`, `[tdarr-configs, tdarr-server]`).
# Anchored at column zero for the same reason as _DECLARATION_RE: an indented key belongs to some
# other mapping.
_SNAPSHOT_CLAIM_RE = re.compile(
    r"^k8s_autodeploy_snapshot_pvcs:[ \t]*\[(?P<items>[^\]]*)\]", re.MULTILINE
)


def declares_snapshot_claims(text: str | None) -> bool:
    """Whether a role's defaults/main.yml declares at least one PVC for k8s/volume-revert to
    consider.

    Two consumers, and they want opposite things from an unparseable input:

    * wording gitops-deploy's rollback alert (which services in the batch a volume revert can
      affect) — False under-claims, which is safe for a line that must never overclaim;
    * gating `split_k8s_auto_deploy`'s per-tick cap on claim-declaring services (2026-08-22
      review H2) — False reads as claim-free and lets the service batch, which is the overrun
      the cap exists to prevent.

    Absent, empty (`[]`), multi-line, or unparseable all read as False. What keeps the second
    consumer honest is not this function but
    test_deploy_logic.py::test_declares_snapshot_claims_agrees_with_yaml_for_every_k8s_role,
    which pins this regex against `yaml.safe_load` for every k8s role — so a reformat to block
    style fails CI instead of silently widening the cap. Neither consumer is the authority on
    the revert itself: roles/k8s/manifests decides that from real YAML.
    """
    if not text:
        return False
    m = _SNAPSHOT_CLAIM_RE.search(text)
    return bool(m and m.group("items").strip())


def rollback_volume_revert_note(
    services: set[str], reverting: set[str], rollback_failed: str | None
) -> str:
    """One line for gitops-deploy's rollback-failure Discord alert, stating plainly whether the
    volume revert to the pre-deploy snapshot actually ran and for which services — "was my data
    rolled back too" is the first question during an incident.

    `rollback_failed`, when given, is the rollback redeploy's own exception message: the
    redeploy raised before Ansible could reach the revert task (or during it), so the revert may
    never have completed, and the note must say that plainly rather than claim it ran.

    `reverting` is the subset of `services` whose role defaults declare
    `k8s_autodeploy_snapshot_pvcs` — volume-revert is a no-op for the rest even when the redeploy
    itself succeeds, so naming only `services` would overclaim which ones actually changed.
    """
    if rollback_failed is not None:
        return (
            f"**The rollback redeploy itself failed** (`{rollback_failed}`) — the volume revert "
            f"task may never have completed. Check whether `{', '.join(sorted(services))}` is "
            f"sitting at zero replicas with a volume attached in Longhorn maintenance mode.\n"
        )
    if not reverting:
        return (
            f"No service in `{', '.join(sorted(services))}` declares "
            f"`k8s_autodeploy_snapshot_pvcs` — no volume revert applies.\n"
        )
    no_claim = sorted(services - reverting)
    line = f"Volume revert to the pre-deploy snapshot targets `{', '.join(sorted(reverting))}`"
    if no_claim:
        line += (
            f" (`{', '.join(no_claim)}` declares no `k8s_autodeploy_snapshot_pvcs` and is "
            f"unaffected)"
        )
    return line + ".\n"


def k8s_role_paths(listing: str) -> dict[str, str | None]:
    """Map each k8s role to its defaults/main.yml path in a `git ls-tree -r --name-only` listing.

    A role with no defaults/main.yml at that ref maps to None, which declared_denylist() reads
    as denied. Paths look like `ansible/roles/k8s/<role>/<rest...>`, so the role name is the
    FOURTH segment — this indexing shipped an off-by-one once already.

    Pure string parsing, callable on its own so it's unit-testable without git; the I/O caller
    is gitops_deploy.k8s_declarations_at.
    """
    roles: dict[str, str | None] = {}
    for path in listing.splitlines():
        # "ansible/roles/k8s/<role>/<rest...>" — a real role always has a file at least one
        # directory deep, so anything shallower (a stray file directly under roles/k8s/) is
        # not a role and must not be recorded as one.
        parts = path.split("/")
        if len(parts) < 5:
            continue
        role = parts[3]
        if parts[4] == "defaults" and path.endswith("defaults/main.yml"):
            roles[role] = path
        else:
            # Still record the role so one with no defaults/ at all is visible as None. A
            # setdefault here never clobbers an already-found defaults/main.yml path, and
            # doesn't need to run before it either — the listing's order doesn't matter.
            roles.setdefault(role, None)
    return roles


def split_k8s_auto_deploy(
    cs: ChangeSet,
    paths: list[str],
    *,
    denylist: frozenset[str] | set[str],
    pilot: frozenset[str] | set[str],
    enabled: bool,
    image_only: Callable[[str], bool],
    max_per_tick: int = 0,
    declares_claims: Callable[[str], bool] | None = None,
    max_claim_services_per_tick: int = 0,
) -> ChangeSet:
    """Promote image-bump-only k8s changes from `cs.k8s` into `cs.k8s_deploy`.

    A service qualifies only when ALL of:
      * the feature is enabled;
      * it is not denylisted (platform / observability / migrating-state / dependency-edge /
        stateful / nothing-to-gate / probe-less / games — each entry's reason is in the role
        defaults and the design doc's denylist table);
      * `pilot` is empty, or the service is named there (the slice-1 pilot scope);
      * every path this push changed under that role dir is exactly its defaults/main.yml;
      * `image_only(svc)` — that file's diff touches only `*_image:` lines.

    The path check and the diff check are BOTH required: the path check alone would admit a push
    editing defaults/main.yml's non-image vars, and the diff check alone would admit a push that
    also edits tasks/.

    Two caps then bound what one tick takes on: `max_per_tick` on the batch as a whole, and
    `max_claim_services_per_tick` on services declaring `k8s_autodeploy_snapshot_pvcs` — whose
    snapshot+revert cost is additive across a batch while the rollback budget is derived for one.
    Surplus in either direction stays in `cs.k8s` and defer-and-alerts.

    Fail-closed by construction — anything not promoted stays in `cs.k8s`, which defer-and-alerts
    exactly as it does today.
    """
    if not enabled:
        return cs
    if cs.services:
        # A tick carrying Docker services too. The caller's k8s branch returns before reaching
        # the Docker deploy + health gate, so promoting here would silently skip them. No host
        # is mixed today (daniel-box is all-k8s and is the only has_gitops host), so defer the
        # k8s half rather than grow a two-plane tick ordering this deployer doesn't model.
        return cs
    promoted: set[str] = set()
    for svc in cs.k8s:
        if svc in denylist:
            continue
        if pilot and svc not in pilot:
            continue
        role_prefix = f"ansible/roles/k8s/{svc}/"
        changed_here = [p for p in paths if p.startswith(role_prefix)]
        if changed_here != [_K8S_DEFAULTS_PATH.format(svc=svc)]:
            continue
        if not image_only(svc):
            continue
        promoted.add(svc)
    if not promoted:
        return cs
    # Cap what one tick takes on. deploy_k8s joins every promoted service into a SINGLE
    # ansible-playbook invocation under one shared K8S_DEPLOY_TIMEOUT_S, and on timeout the
    # failure path is `git reset --hard local` across the whole merged range — so an overlong
    # batch discards the good bumps alongside the bad one. renovate.json states the intent
    # ("keeps each auto-deploy tick to a single service, which is what the deploy timeout is
    # sized for") but nothing enforced it: a tick diffs local..origin, spanning every commit
    # since the last one, and per-service k8s PRs share one daily window with platformAutomerge.
    #
    # The surplus stays in cs.k8s, which defer-and-alerts — the same fail-closed path as any
    # unpromotable change. It is NOT picked up on a later tick, and it is worth being exact
    # about that: the ff-merge runs before deploy_k8s, so once the tick succeeds local == origin
    # and next_action() returns "noop" from then on. renovate.json states this correctly for the
    # same mechanism. What actually carries the surplus is the Discord message alert_deferred()
    # posts, which names the services and the tags to deploy them by hand — so the surplus is
    # operator-visible, but only once, and unlike hold_sha/diverged_sha/behind_since it leaves no
    # state behind for a monitor to notice.
    #
    # Do NOT "fix" this by deferring the ff-merge: that strands the tree behind pods already
    # running the new images.
    #
    # SECOND cap, on claim-declaring services specifically (2026-08-22 review H2). The rollback
    # budget K8S_ROLLBACK_TIMEOUT_S is derived for the worst SINGLE promoted service that
    # declares k8s_autodeploy_snapshot_pvcs — but deploy_k8s joins the whole batch into one
    # playbook run, and each such service pays its own snapshot + revert phase serially inside
    # it (only the rollout WAIT is deduped, by k8s/rollout-drain). Measured from role sources:
    # one radarr/sonarr-shaped service is ~1200s against a 1320s budget, two co-batched ~1680s,
    # three ~2160s. Past the budget `run()`'s killpg fires MID-REVERT — after volume-revert has
    # scaled the workload to zero replicas and attached the volume with disableFrontend: true —
    # stranding a service at zero replicas with its volume in Longhorn maintenance mode.
    #
    # Not hypothetical: slice 7b co-promoted five lscr.io/linuxserver/* siblings (bazarr,
    # jellyfin, prowlarr, radarr, sonarr) that share one Renovate automerge window with no
    # stagger, so a 2-3 sibling tick is ordinary.
    #
    # Capping claim-declaring services rather than lowering max_per_tick is deliberate: claim-free
    # services cost nothing on the revert path and should still batch. Per-service ROLLBACK
    # invocation — the alternative the role CLAUDE.md proposes — does NOT fit: 3 x 1200 + 900
    # forward + 180 flock exceeds the unit's 2700s TimeoutStartSec.
    #
    # `declares_claims` is injected (like `image_only`) so this stays a pure function; the caller
    # reads each role's defaults at the PINNED origin SHA. It resolves False for a declaration the
    # regex cannot parse, which for ALERT WORDING was the safe direction and for GATING is not —
    # a multi-line declaration would read as claim-free and batch. What holds that closed is
    # test_deploy_logic.py::test_declares_snapshot_claims_agrees_with_yaml_for_every_k8s_role,
    # which pins the regex verdict against yaml.safe_load for every k8s role, so a reformat fails
    # CI rather than silently widening this cap.
    if declares_claims is not None and max_claim_services_per_tick > 0:
        ordered = sorted(promoted)
        claim_services = [svc for svc in ordered if declares_claims(svc)]
        claim_free = [svc for svc in ordered if svc not in set(claim_services)]
        kept = claim_services[:max_claim_services_per_tick]
        room = (
            len(claim_free) if max_per_tick <= 0 else max(0, max_per_tick - len(kept))
        )
        promoted = set(kept + claim_free[:room])
    elif max_per_tick > 0 and len(promoted) > max_per_tick:
        promoted = set(sorted(promoted)[:max_per_tick])
    # Both branches above already respect max_per_tick (the first via `room`), so there is no
    # third clamp here — one would be able to drop the claim-declaring service the first branch
    # deliberately kept.
    if not promoted:
        # Every promotable service was a surplus claim-declaring one. Returning `cs` unchanged
        # leaves them all in cs.k8s, which defer-and-alerts — the same fail-closed path as any
        # unpromotable change.
        return cs
    return replace(cs, k8s=cs.k8s - promoted, k8s_deploy=cs.k8s_deploy | promoted)


def staging_scope(services: set[str], subset: set[str]) -> tuple[set[str], set[str]]:
    """Split a deploy into the half staging can speak for and the half it cannot.

    Decision 3 of docs/staging-phase-c.md. Staging runs six services of roughly fifty-four, and
    **a gate over a subset gates only that subset** — the design's single most important
    limitation and the one most likely to be forgotten once the tile is green.

    Returns (gated, ungated). Both halves are returned rather than just the first because the
    caller has to SAY which services staging never saw: a silent skip and a silent pass look
    identical in a log afterwards, and that ambiguity is how a subset gate gets over-read as
    coverage of the whole fleet.

    An empty `gated` is the ordinary case, not an error — most deploys touch nothing staging
    runs.
    """
    return services & subset, services - subset


def staging_verdict_summary(
    gated: set[str], ungated: set[str], deploy_rc: int, expect_rc: int
) -> str:
    """One line an operator can act on, naming what was and was not covered.

    `deploy_rc` and `expect_rc` are staging_gate.py's and staging_expectations.py's verdicts:
    0 pass, 1 rejected/failed, 2 no verdict. They are kept apart because Decision 4 needs
    "staging rejected this change" told apart from "staging could not be asked" — a guest that
    will not boot and a genuinely bad manifest are the same message otherwise, and an operator
    who cannot tell them apart learns to override on reflex.
    """
    if not gated:
        return (
            f"staging: nothing to gate; {len(ungated)} service(s) unchecked by staging"
        )
    # Named on EVERY verdict, not just the pass. A rejection is the moment an operator is most
    # likely to read the line as a statement about the whole deploy, and Decision 3's point is
    # that a silent skip and a silent pass look identical afterwards — which is just as true of
    # a silent skip beside a rejection.
    unchecked = f"; {len(ungated)} unchecked" if ungated else ""
    # The deploy's verdict is read FIRST, and expect_rc only counts once the deploy passed.
    # The caller leaves expect_rc at 2 whenever it never ran the expectation check, so a real
    # staging deploy failure always arrives as (1, 2) — and a plain `or` on that pair reported
    # a genuinely bad manifest as "staging could not be asked, which is not a rejection".
    # Decision 4's whole point is keeping those two apart, and that ordering told the operator
    # to discount the one verdict worth acting on.
    if deploy_rc == 2 or (deploy_rc == 0 and expect_rc == 2):
        return (
            f"staging: NO VERDICT on {sorted(gated)} "
            f"(deploy={deploy_rc}, expect={expect_rc}) — staging could not be asked, "
            f"which is not a rejection{unchecked}"
        )
    if deploy_rc != 0:
        return f"staging: REJECTED {sorted(gated)} — the deploy failed on staging{unchecked}"
    if expect_rc != 0:
        return (
            f"staging: REJECTED {sorted(gated)} — deployed, but a service did not answer "
            f"as declared{unchecked}"
        )
    return f"staging: PASS on {sorted(gated)}{unchecked}"


def stale_rendered_services(rendered: list[str], declared: set[str]) -> list[str]:
    """Rendered compose dirs with no containers_list entry — the stale-compose trap.

    A service retired or migrated off this host leaves containers/<svc>/ behind unless the
    cutover cleans it up; the phantom compose then feeds containers_for(), the health gate
    polls a container that will never run again, and a healthy push rolls back with a hold
    (code-server 2026-08-10, then the kopia/terraria cutover the same day — the second
    occurrence is why this is now a machine check instead of an operator memory)."""
    return sorted(set(rendered) - declared)
